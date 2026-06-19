"""步骤⑩ 步骤前就绪检查 —— 页面准备好了吗.

执行每个动作前:
1. 确定性恢复 (fill 重填 / radio 补选, 见 deterministic_recovery)
2. URL + 语义DOM + 用例上下文 + 下一步意图 → LLM 判断 ready
3. 规则过滤 recovery (提交保护 / 越权剔除 / 无关 fill 过滤)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..dom import extract_semantic_dom
from ..execution.deterministic_recovery import (
    detect_unfilled_required_fields,
    extract_label_from_intent,
    resolve_expected_radio_label,
)
from ..llm import LLMAdapter, PromptLoader
from ..planning import PlannedAction, coerce_action

_ALLOWED_RECOVERY = {"click", "hover", "fill", "goto", "wait"}
_READINESS_ACTION_TYPES = frozenset({"click", "hover", "fill", "upload", "goto"})
_SUBMIT_WORDS = ("提交", "保存", "确定", "确认", "登录", "下一步", "结算", "立即支付", "支付", "完成")

_DEFAULT_SYSTEM = """\
你是"步骤前就绪检查器". 判断当前页面是否已准备好执行"下一步动作".
- 有弹窗时只看弹窗内状态, 忽略背景页.
- 下一步是提交/保存类动作时, 若必填项未填或表单未打开 → 就绪=false.
- 下一步是断言(assert_text等): DOM 已可检查目标则 ready=true; 禁止为断言填表/登录.
- 菜单未展开时 recovery 须先 hover/click 展开, 再点菜单项.
未就绪时给 1~5 条恢复动作, type 只能是 click/hover/fill/goto/wait, 每条一个原子操作.
禁止在恢复动作里包含与下一步相同的提交类点击.
只输出 JSON:
{"ready": true/false, "recovery": [ {"type":"click","intent":"...","value":null}, ... ]}"""

_DEFAULT_USER = """\
当前URL: {{url}}
必填项: 共{{req_total}}个, 未填{{req_empty}}个
未填字段名: {{required_missing}}

下一步动作: type={{action_type}} intent={{intent}} value={{value}}

用例备注/业务提示:
{{case_notes}}

用例操作步骤 (原始):
{{case_steps}}

会话变量 (api_call / bind_session 等):
{{session_vars}}

本用例已规划/已执行的前序动作 (含恢复步):
{{prior_steps}}

当前页面DOM摘要(弹窗/表单优先):
{{dom}}

请输出 JSON。"""


@dataclass
class ReadinessCaseContext:
    """用例级静态上下文 (run_actions 开始时注入)."""

    notes: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    business_hints: list[str] = field(default_factory=list)


@dataclass
class ReadinessContext:
    """单次就绪检查的动态 + 静态上下文."""

    case: ReadinessCaseContext = field(default_factory=ReadinessCaseContext)
    prior_actions: list[PlannedAction] = field(default_factory=list)
    session_vars: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReadinessResult:
    """步骤前就绪检查结果, 包含是否放行以及可选恢复动作."""

    ready: bool
    recovery: list[PlannedAction] = field(default_factory=list)
    skip_main: bool = False
    note: str = ""


def is_advancing(action: PlannedAction) -> bool:
    """推进门控: 提交/保存/跳转类动作才需就绪检查."""
    if action.type == "goto":
        return True
    if action.type == "click":
        return any(w in action.intent for w in _SUBMIT_WORDS)
    return False


def is_submit(action: PlannedAction) -> bool:
    """判断动作是否属于提交/保存/支付等关键推进点击."""
    return action.type == "click" and any(w in action.intent for w in _SUBMIT_WORDS)


def should_run_readiness(action: PlannedAction) -> bool:
    """门控: 仅对可能发生页面状态偏差的 UI 动作做就绪检查 (参考 V3 白名单)."""
    if action.is_assert():
        return False
    if action.type in ("api_call", "bind_session", "wait", "press", "select"):
        return False
    return action.type in _READINESS_ACTION_TYPES


class ReadinessChecker:
    """在关键动作前判断页面是否处于可执行状态."""

    def __init__(self, llm: LLMAdapter, prompts: PromptLoader) -> None:
        self.llm = llm
        self.prompts = prompts
        self._case_context = ReadinessCaseContext()

    def set_case_context(self, ctx: ReadinessCaseContext | None) -> None:
        self._case_context = ctx or ReadinessCaseContext()

    def check(
        self,
        page: Any,
        action: PlannedAction,
        *,
        context: ReadinessContext | None = None,
    ) -> ReadinessResult:
        ctx = context or ReadinessContext(case=self._case_context)
        if ctx.case is None:
            ctx.case = self._case_context
        elif (
            not ctx.case.notes
            and not ctx.case.steps
            and not ctx.case.preconditions
            and not ctx.case.business_hints
        ):
            ctx.case = self._case_context

        required_missing = detect_unfilled_required_fields(page)
        req_total = len(required_missing)
        req_empty = len(required_missing)

        dom = extract_semantic_dom(page, dialog_first=True)
        if not dom.strip() and action.type != "goto":
            return ReadinessResult(
                ready=True,
                note="语义 DOM 为空, 跳过 LLM 就绪检查",
            )

        system = self.prompts.system("readiness", _DEFAULT_SYSTEM)
        user = self.prompts.user(
            "readiness", _DEFAULT_USER,
            url=_safe_url(page),
            req_total=req_total,
            req_empty=req_empty,
            required_missing=_format_lines(required_missing, "(无)"),
            action_type=action.type,
            intent=action.intent,
            value=action.value,
            dom=dom,
            case_notes=_format_case_notes(ctx.case),
            case_steps=_format_lines(ctx.case.steps, "(无)"),
            session_vars=_format_session_vars(ctx.session_vars),
            prior_steps=_format_prior_actions(ctx.prior_actions),
        )
        try:
            data = self.llm.complete_json("readiness", system, user).data
        except Exception:
            return ReadinessResult(ready=True, note="就绪检查调用失败, 默认放行")

        if not isinstance(data, dict):
            return ReadinessResult(ready=True, note="就绪检查返回非JSON")
        reason = str(data.get("reason") or "").strip()
        ready = bool(data.get("ready", True))
        if ready:
            return ReadinessResult(ready=True, note=reason or "")

        raw_list = data.get("recovery") or data.get("recovery_actions") or []
        recovery = self._parse_recovery(raw_list)

        if is_submit(action) and any(r.type == "fill" for r in recovery):
            return ReadinessResult(
                ready=False, recovery=[], skip_main=False,
                note="提交保护: 真实值已输入, 跳过假值补填",
            )

        if not required_missing and is_submit(action):
            recovery = _filter_unrelated_fill_recovery(recovery, action)

        if is_submit(action):
            recovery = [r for r in recovery if not is_submit(r)]

        expected_radio = resolve_expected_radio_label(
            last_click_label=None,
            api_context=ctx.session_vars,
            prior_actions=ctx.prior_actions,
            case_steps=ctx.case.steps,
            case_notes=ctx.case.notes,
        )
        if expected_radio:
            recovery = _filter_contradictory_radio_recovery(recovery, expected_radio)

        note = reason or "页面未就绪"
        return ReadinessResult(ready=False, recovery=recovery, note=note)

    @staticmethod
    def _parse_recovery(raw) -> list[PlannedAction]:
        out: list[PlannedAction] = []
        for item in raw or []:
            a = coerce_action(item)
            if a is not None and a.type in _ALLOWED_RECOVERY:
                a.is_recovery = True
                out.append(a)
            if len(out) >= 5:
                break
        return out


def _filter_contradictory_radio_recovery(
    recovery: list[PlannedAction],
    expected: str,
) -> list[PlannedAction]:
    """去掉与用例指定审核原因矛盾的 radio click recovery."""
    exp = expected.strip()
    if not exp:
        return recovery
    kept: list[PlannedAction] = []
    for r in recovery:
        if r.type != "click":
            kept.append(r)
            continue
        label = extract_label_from_intent(r.intent or "") or (r.value or "")
        if not label:
            kept.append(r)
            continue
        if label in exp or exp in label:
            kept.append(r)
            continue
        if any(h in (r.intent or "") for h in ("单选", "radio", "审核原因", "选项")):
            continue
        kept.append(r)
    return kept


def _filter_unrelated_fill_recovery(
    recovery: list[PlannedAction],
    action: PlannedAction,
) -> list[PlannedAction]:
    """提交步且必填无缺失: 去掉与提交 intent 无关的 fill recovery."""
    main_intent = (action.intent or "").lower()
    kept: list[PlannedAction] = []
    for r in recovery:
        if r.type != "fill":
            kept.append(r)
            continue
        fill_intent = (r.intent or "").lower()
        fill_value = (r.value or "").lower()
        if fill_intent and any(
            kw in fill_intent
            for kw in re.split(r"[\s,，。、]+", main_intent)
            if len(kw) >= 2
        ):
            kept.append(r)
        elif fill_value and fill_value in main_intent:
            kept.append(r)
    return kept


def _safe_url(page: Any) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _format_lines(lines: list[str], empty: str = "(无)") -> str:
    if not lines:
        return empty
    return "\n".join(f"- {s}" for s in lines if s)


def _format_case_notes(case: ReadinessCaseContext) -> str:
    parts: list[str] = []
    if case.notes:
        parts.append("备注:\n" + _format_lines(case.notes))
    if case.preconditions:
        parts.append("前置条件:\n" + _format_lines(case.preconditions))
    if case.business_hints:
        parts.append("业务知识:\n" + _format_lines(case.business_hints))
    return "\n\n".join(parts) if parts else "(无)"


def _format_session_vars(vars: dict[str, Any]) -> str:
    if not vars:
        return "(无)"
    skip = {"_last_click_label", "_last_click_text"}
    lines = []
    for k, v in vars.items():
        if k.startswith("_") or k in skip:
            continue
        lines.append(f"- {k} = {v}")
    return "\n".join(lines) if lines else "(无)"


def _format_prior_actions(actions: list[PlannedAction]) -> str:
    if not actions:
        return "(无, 当前为第一步或尚无已执行动作)"
    lines: list[str] = []
    for i, a in enumerate(actions, 1):
        tag = "[恢复] " if getattr(a, "is_recovery", False) else ""
        val = f" value={a.value}" if a.value else ""
        lines.append(f"{i}. {tag}{a.type} | {a.intent}{val}")
    return "\n".join(lines)

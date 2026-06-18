"""步骤⑫ 步骤后校验 —— 点了不等于点对了 (防假操作).

执行完动作后, 把 动作类型/意图/分发结果/当前DOM摘要 发给大模型判断"真成功"还是"执行了但结果不对".
关键: 分发消息里"实际点击目标"是否与意图一致 —— 点错必须判假.
输入/上传: 值不符合占位符/格式说明必须判假.
悬停: 菜单/下拉类悬停后检查 menuitem 是否仍全部 [hidden], 并校验是否悬停到触发器而非内层 span.
输入锚点窗口: 输入后校验时, 在DOM摘要里找与值匹配的输入框行, 取前后各60行.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..dom import extract_semantic_dom
from ..llm import LLMAdapter, PromptLoader
from ..planning import PlannedAction

# 需要后校验的动作类型 (断言/等待/跳转自身即结果, 不再二次校验)
_POST_CHECK_TYPES = {"click", "fill", "press", "upload", "hover"}

# 悬停类 intent 若含这些词, 认为目标是展开菜单/下拉, 需做可见性判断
_MENU_HOVER_MARKERS = ("菜单", "下拉", "用户", "悬浮", "悬停", "hover")

_DEFAULT_SYSTEM = """\
你是"步骤后校验器", 判断一个 UI 操作是否真的达成了意图 (而非"执行了但结果不对").

规则:
1. 分发成功=false → 倾向 step_ok=false.
2. 分发成功=true → 仍要判断是"真成功"还是"点错地方/输入了错值".
3. 关键: 分发消息中"实际目标"元素是否与意图一致 —— 点错(如想点确定却点了取消)必须 step_ok=false.
4. 输入/上传: 值不符合占位符或格式说明 → step_ok=false.
5. 悬停: 若意图是展开菜单/下拉, 悬停后相关 menuitem 仍全部 hidden → step_ok=false.
6. 失败时给出原因, 并给重试建议:
   retry_focus ∈ "值"|"选择器"|"两者"|"无"; suggested_value(改值时的新值); resolve_hint(换元素时的提示).
只输出 JSON:
{"step_ok": true/false, "reason": "...", "retry_focus": "无", "suggested_value": null, "resolve_hint": null}"""

_DEFAULT_USER = """\
动作类型: {{action_type}}
操作意图: {{intent}}
输入值: {{value}}
分发成功: {{ok}}
分发消息: {{message}}

当前页面DOM摘要:
{{dom}}

请输出 JSON。"""


@dataclass
class PostCheckResult:
    """后校验输出: 是否真成功, 以及失败时下一轮重试应该调整什么."""

    step_ok: bool
    reason: str = ""
    retry_focus: str = "无"          # 值 | 选择器 | 两者 | 无
    suggested_value: Optional[str] = None
    resolve_hint: Optional[str] = None


def should_post_check(action: PlannedAction) -> bool:
    """只对可能发生"执行成功但目标不对"的动作做后校验."""
    return action.type in _POST_CHECK_TYPES


class PostStepChecker:
    """调用 LLM 判断动作执行结果是否符合真实业务意图."""

    def __init__(self, llm: LLMAdapter, prompts: PromptLoader) -> None:
        self.llm = llm
        self.prompts = prompts

    def check(
        self,
        page: Any,
        action: PlannedAction,
        dispatch_ok: bool,
        dispatch_msg: str,
        next_action: Optional[Any] = None,
    ) -> PostCheckResult:
        # 后校验需要基于执行后的最新页面状态判断.
        dom = extract_semantic_dom(page, dialog_first=True)

        # 如果有下一步意图, 注入到 DOM 摘要中, 让 LLM 结合当前结果和下一步预期判断.
        if next_action:
            dom = dom + f"\n\n【下一步意图】type={next_action.type}, intent={next_action.intent}"

        if action.type == "hover":
            code_result = _check_hover_visibility(action.intent, dispatch_ok, dispatch_msg, dom)
            if code_result is not None:
                return code_result

        if action.type == "fill" and action.value:
            # 输入类动作通常只关心输入框附近 DOM, 截窗降低 prompt 噪声.
            dom = _input_anchor_window(dom, action.value)

        system = self.prompts.system("post_check", _DEFAULT_SYSTEM)
        user = self.prompts.user(
            "post_check", _DEFAULT_USER,
            action_type=action.type, intent=action.intent, value=action.value,
            ok=str(dispatch_ok).lower(), message=dispatch_msg, dom=dom,
        )
        try:
            data = self.llm.complete_json("post_check", system, user).data
        except Exception as e:  # noqa: BLE001
            # 校验器本身失败时, 退回到分发结果, 不误杀
            return PostCheckResult(step_ok=dispatch_ok, reason=f"后校验调用失败: {e}")

        if not isinstance(data, dict):
            return PostCheckResult(step_ok=dispatch_ok, reason="后校验返回非JSON")
        # 模型字段缺失时退回分发结果, 避免后校验不稳定导致误判失败.
        return PostCheckResult(
            step_ok=bool(data.get("step_ok", dispatch_ok)),
            reason=str(data.get("reason") or ""),
            retry_focus=str(data.get("retry_focus") or "无"),
            suggested_value=_opt_str(data.get("suggested_value")),
            resolve_hint=_opt_str(data.get("resolve_hint")),
        )


def _hover_needs_menu_open(intent: str) -> bool:
    """判断悬停意图是否要求展开菜单/下拉面板."""
    text = (intent or "").lower()
    return any(m.lower() in text for m in _MENU_HOVER_MARKERS)


def _scan_menu_visibility(dom: str) -> tuple[int, int, bool]:
    """统计 DOM 摘要中 menu/menuitem 的可见与 hidden 数量; 第三项表示是否出现过菜单相关行."""
    hidden = visible = 0
    found = False
    for line in dom.split("\n"):
        low = line.lower()
        if "menuitem" not in low and 'role="menu"' not in low and "role=menu" not in low:
            continue
        found = True
        if "[hidden]" in line:
            hidden += 1
        else:
            visible += 1
    return hidden, visible, found


def _hover_hit_inner_span(dispatch_msg: str) -> bool:
    """分发消息显示实际悬停在内层 span, 而非 button 触发器."""
    msg = (dispatch_msg or "").lower()
    return "<span" in msg and "role=\"button\"" not in msg and "role=button" not in msg


def _check_hover_visibility(
    intent: str,
    dispatch_ok: bool,
    dispatch_msg: str,
    dom: str,
) -> Optional[PostCheckResult]:
    """悬停后的代码级可见性判断; 明确失败/成功时直接返回, 否则交 LLM."""
    if not _hover_needs_menu_open(intent):
        return None
    if not dispatch_ok:
        return None

    hidden, visible, found = _scan_menu_visibility(dom)
    if not found:
        return None

    if visible > 0:
        return PostCheckResult(
            step_ok=True,
            reason=f"悬停后已有 {visible} 个可见菜单项, 下拉已展开",
            retry_focus="无",
        )

    if hidden > 0:
        hint = (
            "悬停 role=button 或 haspopup=menu 的下拉触发容器, "
            "不要只悬停内层 span 文本节点"
        )
        if _hover_hit_inner_span(dispatch_msg):
            hint = (
                "实际悬停到了内层 span, 应改为悬停外层 "
                "[role=button][haspopup=menu] 触发器"
            )
        return PostCheckResult(
            step_ok=False,
            reason=f"悬停后菜单项仍全部 hidden(共 {hidden} 个), 下拉未展开",
            retry_focus="选择器",
            resolve_hint=hint,
        )
    return None


def _input_anchor_window(dom: str, value: str, radius: int = 60) -> str:
    """在DOM摘要里找含 value 的行, 取前后各 radius 行作为锚点窗口."""
    lines = dom.split("\n")
    hit = next((i for i, ln in enumerate(lines) if value in ln), None)
    if hit is None:
        return dom
    start = max(0, hit - radius)
    end = min(len(lines), hit + radius + 1)
    return "\n".join(lines[start:end])


def _opt_str(v) -> Optional[str]:
    """把模型可选字段归一成 None 或非空字符串."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None

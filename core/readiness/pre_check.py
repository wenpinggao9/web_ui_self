"""步骤⑩ 步骤前就绪检查 —— 页面准备好了吗.

执行每个动作前, 把 URL + 语义DOM摘要(弹窗/表单优先) + 下一步类型/意图 发给大模型判断就绪=真/假.
未就绪时给 1~5 条恢复动作 (只允许 点击/悬停/输入/跳转/等待).

关键约束:
1. 禁止越权: 下一步是提交操作时, 恢复动作禁止含相同提交类点击.
2. 弹窗感知: 有弹窗时只看弹窗内状态.
3. 必填项检测: JS 检测 required/aria-required/Element Plus 红星, 下一步提交但必填未填 → 就绪=假.
4. 提交类动作保护: 下一步提交且恢复含输入 → 说明想用假值补填(真实值已输入, 丢失是重渲染导致) → 跳过恢复直接执行主动作.
推进门控: 上一步后校验成功时, 只在"推进类动作"才做就绪检查, 省调用.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..dom import extract_semantic_dom
from ..llm import LLMAdapter, PromptLoader
from ..planning import PlannedAction, coerce_action

_ALLOWED_RECOVERY = {"click", "hover", "fill", "goto", "wait"}
_SUBMIT_WORDS = ("提交", "保存", "确定", "确认", "登录", "下一步", "结算", "立即支付", "支付", "完成")

_REQUIRED_JS = r"""
() => {
  const sels = ['[required]','[aria-required="true"]','.el-form-item.is-required','.el-form-item__required'];
  let total = 0, empty = 0;
  document.querySelectorAll(sels.join(',')).forEach(node => {
    const input = node.matches('input,textarea,select') ? node : node.querySelector('input,textarea,select');
    if (!input) return;
    total += 1;
    if (!(input.value && input.value.trim())) empty += 1;
  });
  return { total: total, empty: empty };
}
"""

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

下一步动作: type={{action_type}} intent={{intent}} value={{value}}

当前页面DOM摘要(弹窗/表单优先):
{{dom}}

请输出 JSON。"""


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


class ReadinessChecker:
    """在关键动作前判断页面是否处于可执行状态."""

    def __init__(self, llm: LLMAdapter, prompts: PromptLoader) -> None:
        self.llm = llm
        self.prompts = prompts

    def check(self, page: Any, action: PlannedAction) -> ReadinessResult:
        # 必填项状态通过 JS 快速检测, 与语义 DOM 一起作为 LLM 判断依据.
        req = self._required_status(page)
        dom = extract_semantic_dom(page, dialog_first=True)
        system = self.prompts.system("readiness", _DEFAULT_SYSTEM)
        user = self.prompts.user(
            "readiness", _DEFAULT_USER,
            url=_safe_url(page), req_total=req["total"], req_empty=req["empty"],
            action_type=action.type, intent=action.intent, value=action.value, dom=dom,
        )
        try:
            data = self.llm.complete_json("readiness", system, user).data
        except Exception:
            # 就绪检查失败时默认放行, 避免辅助能力阻断主流程.
            return ReadinessResult(ready=True, note="就绪检查调用失败, 默认放行")

        if not isinstance(data, dict):
            return ReadinessResult(ready=True, note="就绪检查返回非JSON")
        ready = bool(data.get("ready", True))
        if ready:
            return ReadinessResult(ready=True)

        recovery = self._parse_recovery(data.get("recovery"))
        # 提交类保护: 下一步提交且恢复含输入 → 跳过恢复直接执行主动作
        if is_submit(action) and any(r.type == "fill" for r in recovery):
            return ReadinessResult(ready=False, recovery=[], skip_main=False,
                                   note="提交保护: 真实值已输入, 跳过假值补填")
        # 禁止越权: 下一步提交时剔除恢复里的同类提交点击
        if is_submit(action):
            recovery = [r for r in recovery if not is_submit(r)]
        return ReadinessResult(ready=False, recovery=recovery)

    def _required_status(self, page: Any) -> dict:
        """执行浏览器侧 JS, 统计当前页面必填项数量和未填数量."""
        try:
            return page.evaluate(_REQUIRED_JS) or {"total": 0, "empty": 0}
        except Exception:
            return {"total": 0, "empty": 0}

    @staticmethod
    def _parse_recovery(raw) -> list[PlannedAction]:
        """把模型返回的恢复动作转换成 PlannedAction, 并过滤不允许的类型."""
        out: list[PlannedAction] = []
        for item in raw or []:
            a = coerce_action(item)
            if a is not None and a.type in _ALLOWED_RECOVERY:
                a.is_recovery = True
                out.append(a)
            if len(out) >= 5:
                break
        return out


def _safe_url(page: Any) -> str:
    """安全读取页面 URL, 页面关闭等异常时返回空字符串."""
    try:
        return page.url or ""
    except Exception:
        return ""

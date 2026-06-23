"""后校验发现弹窗阻挡时, 先执行就绪恢复再重试原动作."""
from __future__ import annotations

import re
from typing import Any, Optional

from ..planning import PlannedAction
from .dispatcher import ActionDispatcher
from .post_check import PostCheckResult
from .trace import ExecutionTrace

_POPUP_BLOCK_MARKERS = (
    "弹窗", "对话框", "dialog", "modal", "红线", "checkbox", "复选框",
    "已知悉", "遮挡", "阻挡", "先处理", "先勾选", "关闭弹窗", "intercept",
)
_DISABLED_MARKERS = re.compile(r"disabled|not enabled|已禁用|不可点|不可用", re.I)
_POPUP_EVIDENCE = re.compile(r"弹窗|modal|dialog|遮挡|阻挡|intercept|pointer events", re.I)
_CONFIRM_BTN_NAMES = ("已知悉并确认", "已知悉", "我已知悉", "同意并继续", "确认")
_DIALOG_VISIBLE = '.ant-modal-wrap:visible, .el-dialog:visible, [role="dialog"]:visible'
_REDLINE_MARKERS = re.compile(r"红线|已知悉|阅读并同意|协议|checkbox|复选框|勾选", re.I)


def needs_popup_recovery(post: PostCheckResult, dispatch_ok: bool) -> bool:
    """后校验指出弹窗阻挡时, 应先关弹窗再重试 (含点击超时/失败)."""
    text = " ".join(filter(None, (post.reason, post.resolve_hint)))
    # 仅按钮 disabled / not enabled、无弹窗证据 → 不是弹窗问题 (常见: 已领取导致按钮灰掉)
    if _DISABLED_MARKERS.search(text) and not _POPUP_EVIDENCE.search(text):
        return False
    if not any(m in text for m in _POPUP_BLOCK_MARKERS):
        return False
    if dispatch_ok:
        return True
    return any(m in text for m in ("弹窗", "遮挡", "阻挡", "红线", "modal", "dialog", "已知悉", "checkbox", "复选框"))


def reset_action_for_popup_retry(action: PlannedAction) -> None:
    """关弹窗后清空排除/选择器, 用原 intent 重新定位."""
    action.selector = None
    action.exclude_selectors = []
    action.resolve_hint = None


def try_dismiss_blocking_dialog(page: Any, timeout: int = 10000) -> bool:
    """规则关弹窗: 红线/协议类 dialog — 若存在则勾选 checkbox 并点确认."""
    try:
        dialog = page.locator(_DIALOG_VISIBLE).first
        wrap = page.locator(_DIALOG_VISIBLE)
        if wrap.count() == 0:
            return False
        body = dialog.inner_text(timeout=min(2000, timeout))
        if not any(k in body for k in ("红线", "已知悉", "阅读并同意", "协议")):
            return False
        cb = dialog.locator('input[type="checkbox"]').first
        if cb.count():
            try:
                if not cb.is_checked():
                    cb.click(timeout=timeout)
            except Exception:
                label = dialog.locator("label").filter(has_text="阅读").first
                if label.count():
                    label.click(timeout=timeout)
        for name in _CONFIRM_BTN_NAMES:
            btn = dialog.get_by_role("button", name=name)
            if btn.count():
                btn.first.click(timeout=timeout)
                try:
                    page.wait_for_selector(_DIALOG_VISIBLE, state="hidden", timeout=timeout)
                except Exception:
                    pass
                return True
    except Exception:
        pass
    return False


def wait_and_dismiss_blocking_dialog(page: Any, timeout: int = 10000) -> None:
    """等待可能出现的阻断弹窗并尝试关闭 (异步弹窗)."""
    try:
        page.locator('.ant-modal-wrap:visible').first.wait_for(state='visible', timeout=3000)
    except Exception:
        pass
    for _ in range(3):
        if page.locator(_DIALOG_VISIBLE).count() == 0:
            return
        try_dismiss_blocking_dialog(page, timeout)


def is_redline_recovery_intent(intent: str) -> bool:
    return bool(_REDLINE_MARKERS.search(intent or ""))


def prepare_dialog_recovery_action(rec: PlannedAction) -> None:
    """弹窗/协议类 recovery: 跳过 L3/L4 启发式, L1/L2 仍可用."""
    if not is_redline_recovery_intent(rec.intent or ""):
        return
    rec.skip_heuristics = True
    intent = rec.intent or ""
    if re.search(r"勾选|复选框|checkbox", intent, re.I):
        rec.resolve_hint = rec.resolve_hint or '[role="dialog"] input[type="checkbox"]'
    elif re.search(r"已知悉|确认|关闭|同意", intent):
        rec.resolve_hint = rec.resolve_hint or '[role="dialog"] button:has-text("已知悉")'


def execute_readiness_recovery(
    dispatcher: ActionDispatcher,
    readiness_checker: Any,
    action: PlannedAction,
    case_id: str,
    *,
    console: Any = None,
    trace: Optional[ExecutionTrace] = None,
    readiness_context: Any = None,
    post_checker: Any = None,
) -> bool:
    """先规则关弹窗, 再 LLM 恢复; 记录供 codegen 生成条件式脚本."""
    from ..readiness import should_run_readiness
    from .deterministic_recovery import run_deterministic_pre_readiness
    from .post_check import should_post_check

    page = dispatcher.page
    timeout = getattr(dispatcher, "default_timeout", 10000)
    if try_dismiss_blocking_dialog(page, timeout):
        if console:
            console.print("  [cyan]弹窗阻挡, 规则恢复: 勾选协议并点击确认[/cyan]")
        if trace:
            trace.emit(
                "popup_recovery",
                ready=False,
                intent=action.intent,
                recovery=[{"type": "rule", "intent": "红线弹窗确认"}],
            )
        dispatcher.mark_popup_dismiss_used(before_intent=action.intent)
        return True

    if not should_run_readiness(action):
        return False

    if readiness_context is not None:
        case = getattr(readiness_context, "case", None)
        run_deterministic_pre_readiness(
            dispatcher,
            action,
            prior_actions=list(getattr(readiness_context, "prior_actions", []) or []),
            case_steps=list(getattr(case, "steps", []) or []) if case else [],
            case_notes=list(getattr(case, "notes", []) or []) if case else [],
        )

    rdy = readiness_checker.check(page, action, context=readiness_context)
    if trace:
        trace.emit(
            "popup_recovery",
            ready=rdy.ready,
            intent=action.intent,
            recovery=[
                {"type": r.type, "intent": r.intent, "value": r.value}
                for r in rdy.recovery
            ],
        )
    if rdy.ready or not rdy.recovery:
        return False
    if console:
        console.print(
            f"  [cyan]弹窗阻挡, 先执行 {len(rdy.recovery)} 条恢复动作再重试[/cyan]"
        )
    executed: list[PlannedAction] = []
    for rec in rdy.recovery:
        rec.is_recovery = True
        if console:
            console.print(f"  [magenta]↺ 弹窗恢复[/magenta] [{rec.type}] {rec.intent}")
        ok, msg = dispatcher.dispatch(rec, case_id=case_id)
        dispatcher.capture_page_state_if_needed(rec)
        if post_checker and should_post_check(rec):
            post = post_checker.check(
                page, rec, ok, msg,
                next_action=action,
                dom_summary=dispatcher.get_cached_dom_summary(),
                dispatch_meta=dispatcher.last_dispatch_meta or None,
                list_anchor=getattr(dispatcher, "_list_tab_anchor", None),
            )
            if trace:
                trace.emit(
                    "popup_recovery_post_check",
                    intent=rec.intent,
                    dispatch_ok=ok,
                    post_ok=post.step_ok,
                    reason=post.reason,
                )
            if ok and not post.step_ok and console:
                console.print(f"  [yellow]弹窗恢复后校验未过: {post.reason}[/yellow]")
        executed.append(rec)
    if executed:
        dispatcher.record_popup_recovery(executed)
        if action.intent and action.intent not in dispatcher.popup_dismiss_before_intents:
            dispatcher.popup_dismiss_before_intents.append(action.intent)
    return bool(executed)

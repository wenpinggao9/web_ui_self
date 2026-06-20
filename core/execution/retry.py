"""步骤⑬ 带后校验的重试 —— 失败了不能傻试.

最多 N 次. 第1次正常执行, 第2~N次按后校验给出的重试焦点调整:
  值     → 只改值 (同步改写意图中的值), 复用上次选择器
  选择器 → 只换元素 (清选择器, 传 resolve_hint + 排除列表)
  两者   → 两个都改
  无     → 放弃重试
弹窗阻挡: 分发已成功但后校验指出弹窗/checkbox 未处理 → 先就绪恢复关弹窗, 再重试原动作 (不排除已命中选择器).
失败连锁清理: 缓存清条目 + 记忆库扣分 + 结构学习记失败 (经 resolver 钩子).
排除列表: 每次失败把选择器加入排除; "只改值"或"弹窗恢复"时不排除当前选择器.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from rich.console import Console

from ..planning import PlannedAction
from ..planning.page_nav import is_sidebar_nav_intent
from .dispatcher import ActionDispatcher
from .deterministic_recovery import (
    _is_submit_action,
    attempt_submit_prerecovery,
)
from .popup_recovery import (
    execute_readiness_recovery,
    needs_popup_recovery,
    reset_action_for_popup_retry,
)
from .post_check import PostStepChecker, upgrade_submit_post_result
from .retry_hint import resolve_force_selector_from_hint
from .target_text import backfill_click_from_dispatch
from .trace import ExecutionTrace


@dataclass
class RetryOutcome:
    """一次带后校验重试流程的最终结果."""

    ok: bool
    post_ok: bool
    message: str
    attempts: int
    reason: str = ""
    selector: Optional[str] = None


class RetryController:
    """根据后校验建议在"改值/换元素/两者"之间选择重试策略."""

    def __init__(
        self,
        dispatcher: ActionDispatcher,
        post_checker: PostStepChecker,
        resolver,
        console: Optional[Console] = None,
        max_retries: int = 5,
        trace: Optional[ExecutionTrace] = None,
        readiness_checker: Optional[Any] = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.post_checker = post_checker
        self.resolver = resolver
        self.console = console or Console()
        self.max_retries = max_retries
        self.trace = trace
        self.readiness_checker = readiness_checker
        self._readiness_context_fn: Optional[Any] = None

    def set_readiness_context_fn(self, fn: Any) -> None:
        self._readiness_context_fn = fn

    def run(self, action: PlannedAction, case_id: str, next_action: Optional[Any] = None) -> RetryOutcome:
        """执行动作并循环后校验, 直到成功、放弃或达到重试上限."""
        exclude: list[str] = list(action.exclude_selectors)
        last_ok, last_msg, last_reason = False, "", ""
        last_selector: Optional[str] = None
        popup_recovery_tried = False

        for attempt in range(1, self.max_retries + 1):
            if self.trace and attempt > 1:
                self.trace.emit(
                    "retry",
                    attempt=attempt,
                    retry_focus=action.resolve_hint or "",
                    exclude=action.exclude_selectors,
                    force_selector=action.force_selector,
                )
            ok, msg = self.dispatcher.dispatch(action, case_id=case_id)
            last_ok, last_msg = ok, msg
            last_selector = action.selector

            # 按钮 disabled 且列表已有数据 → 幂等跳过 (如已领取后「领取题目」变灰)
            if (
                not ok
                and action.type == "click"
                and self.dispatcher.is_disabled_click_failure(msg)
                and self.dispatcher.click_goal_already_met()
            ):
                reason = "按钮不可点但列表已有数据, 视为目标已达成 (幂等跳过)"
                self.console.print(f"  [green]✓ {reason}[/green]")
                if self.trace:
                    self.trace.emit(
                        "post_check",
                        attempt=attempt,
                        dispatch_ok=False,
                        step_ok=True,
                        retry_focus="无",
                        reason=reason,
                        resolve_hint=None,
                    )
                self.dispatcher.mark_idempotent_skip(action.intent)
                self.dispatcher.capture_page_state_if_needed(action)
                return RetryOutcome(True, True, reason, attempt, reason, last_selector)

            # 误插的侧栏导航: 目标页已可操作 → 跳过本步
            if not ok and _spurious_nav_skip(self.dispatcher, action, msg):
                reason = "无需侧栏导航, 当前已在目标业务页 (跳过冗余导航)"
                self.console.print(f"  [green]✓ {reason}[/green]")
                self.dispatcher.mark_idempotent_skip(action.intent)
                self.dispatcher.capture_page_state_if_needed(action)
                if self.trace:
                    self.trace.emit(
                        "post_check",
                        attempt=attempt,
                        dispatch_ok=False,
                        step_ok=True,
                        retry_focus="无",
                        reason=reason,
                        resolve_hint=None,
                    )
                return RetryOutcome(True, True, reason, attempt, reason, last_selector)

            self.dispatcher.capture_page_state_if_needed(action)
            cached_dom = self.dispatcher.get_cached_dom_summary()
            post = self.post_checker.check(
                self.dispatcher.page, action, ok, msg, next_action,
                dom_summary=cached_dom,
                dispatch_meta=self.dispatcher.last_dispatch_meta or None,
            )
            if self.trace:
                self.trace.emit(
                    "post_check",
                    attempt=attempt,
                    dispatch_ok=ok,
                    step_ok=post.step_ok,
                    retry_focus=post.retry_focus,
                    reason=post.reason,
                    resolve_hint=post.resolve_hint,
                )
            if post.step_ok:
                if action.type == "click":
                    backfill_click_from_dispatch(action, msg, post.suggested_value)
                if attempt > 1:
                    self.console.print(f"  [green]✚ 第{attempt}次重试后校验通过[/green]")
                return RetryOutcome(True, True, msg, attempt, post.reason, last_selector)

            last_reason = post.reason
            self.console.print(f"  [yellow]后校验未过(第{attempt}次): {post.reason} → 焦点={post.retry_focus}[/yellow]")

            post = upgrade_submit_post_result(
                post, action.intent or "", self.dispatcher.last_dispatch_meta,
            )
            if post.retry_focus != "无" and post.reason != last_reason:
                self.console.print(
                    f"  [cyan]↺ 提交失败升级重试焦点 → {post.retry_focus}[/cyan]"
                )

            # 提交未生效: 先补选审核原因再重试提交 (不依赖 LLM readiness)
            if (
                _is_submit_action(action)
                and not post.step_ok
                and attempt < self.max_retries
            ):
                ctx_fn = self._readiness_context_fn
                rctx = ctx_fn(action) if ctx_fn else None
                prior = list(getattr(rctx, "prior_actions", None) or [])
                if attempt_submit_prerecovery(
                    self.dispatcher, action, self.console, prior_actions=prior,
                ):
                    action.force_selector = None
                    action.selector = None
                    action.resolve_hint = post.resolve_hint
                    action.skip_acceleration = True
                    exclude.clear()
                    self.console.print("  [cyan]↺ 已补选审核原因, 重试提交[/cyan]")
                    continue

            # 分发已成功但被弹窗挡住: 先关弹窗再重试, 不换选择器、不降权
            if (
                not popup_recovery_tried
                and self.readiness_checker
                and needs_popup_recovery(post, ok)
            ):
                popup_recovery_tried = True
                if execute_readiness_recovery(
                    self.dispatcher,
                    self.readiness_checker,
                    action,
                    case_id,
                    console=self.console,
                    trace=self.trace,
                    readiness_context=(
                        self._readiness_context_fn(action)
                        if self._readiness_context_fn
                        else None
                    ),
                    post_checker=self.post_checker,
                ):
                    reset_action_for_popup_retry(action)
                    exclude.clear()
                    self.console.print(
                        f"  [cyan]弹窗已处理, 重试原动作: {action.intent}[/cyan]"
                    )
                    continue

            # 失败连锁清理 (对齐 V3: post_verify 未过即清缓存/记忆降权)
            page = self.dispatcher.page
            self.resolver.evict(page, action.intent, action.type, last_selector)
            self.resolver.penalize(page, action.intent, action.type, last_selector)

            if post.retry_focus == "无" or attempt == self.max_retries:
                break

            # V3 双 LLM: 专用重试策略规划, 产出更可落地的 resolve_hint
            if post.retry_focus in ("选择器", "两者", "值"):
                plan = self.post_checker.plan_retry(
                    page, action, ok, msg, post.reason,
                    dom_summary=cached_dom,
                )
                if plan and (plan.resolve_hint or plan.suggested_value or plan.retry_focus != "无"):
                    post = _merge_retry_plan(post, plan)
                    if self.trace:
                        self.trace.emit(
                            "retry_plan",
                            retry_focus=post.retry_focus,
                            resolve_hint=post.resolve_hint,
                            suggested_value=post.suggested_value,
                            rationale=plan.reason,
                        )
                    if post.resolve_hint:
                        self.console.print(
                            f"  [dim]  ↳ 重试策略: {post.resolve_hint[:100]}[/dim]"
                        )

            if post.retry_focus != "值" and last_selector:
                # 换元素类重试要排除刚失败的 selector, 防止下一轮又选回来.
                exclude.append(last_selector)
            _apply_retry(
                action, post.retry_focus, post.suggested_value, post.resolve_hint,
                last_selector, exclude, dispatcher=self.dispatcher,
            )

        return RetryOutcome(last_ok, False, last_msg, self.max_retries, last_reason, last_selector)


def _spurious_nav_skip(dispatcher: ActionDispatcher, action: PlannedAction, msg: str) -> bool:
    """侧栏导航找不到元素, 但当前页已有可交互内容 → 视为冗余导航."""
    if action.type != "click" or not is_sidebar_nav_intent(action.intent or ""):
        return False
    low = (msg or "").lower()
    if "未找到" not in msg and "找不到" not in msg and "not found" not in low:
        return False
    page = dispatcher.page
    try:
        return page.locator(
            "form, [role='form'], input, textarea, select, button, [role='radio'], [role='checkbox']"
        ).count() > 0
    except Exception:
        return False


def _apply_retry(
    action: PlannedAction,
    focus: str,
    suggested_value: Optional[str],
    resolve_hint: Optional[str],
    last_selector: Optional[str],
    exclude: list[str],
    *,
    dispatcher: Optional[ActionDispatcher] = None,
) -> None:
    """根据后校验焦点就地改写 action, 供下一轮 retry 使用."""
    # 改值
    if focus in ("值", "两者") and suggested_value is not None:
        action.value = suggested_value
        action.intent = _rewrite_intent_value(action.intent, suggested_value)

    if focus == "值":
        # 值有问题但元素找对了 → 复用上次选择器
        action.force_selector = last_selector
        action.exclude_selectors = []
        action.resolve_hint = None
    else:  # 选择器 / 两者 → 换元素
        items = dispatcher.get_cached_semantic_items() if dispatcher else None
        forced = resolve_force_selector_from_hint(resolve_hint, semantic_items=items)
        action.force_selector = forced
        action.selector = None
        action.resolve_hint = resolve_hint if not forced else None
        action.exclude_selectors = list(exclude)
    action.skip_acceleration = True


def _merge_retry_plan(primary: "PostCheckResult", plan: "PostCheckResult") -> "PostCheckResult":
    """合并后校验与专用重试规划结果, 优先采用规划器的 hint/value."""
    from .post_check import PostCheckResult

    focus = plan.retry_focus if plan.retry_focus != "无" else primary.retry_focus
    hint = plan.resolve_hint or primary.resolve_hint
    value = plan.suggested_value if plan.suggested_value is not None else primary.suggested_value
    return PostCheckResult(
        step_ok=False,
        reason=primary.reason or plan.reason,
        retry_focus=focus,
        suggested_value=value,
        resolve_hint=hint,
    )


def _rewrite_intent_value(intent: str, new_value: str) -> str:
    """改值时同步改写意图: 优先替换最后一个引号内容, 其次替换"输入xxx"尾部."""
    # 最后一对引号 (中英文引号)
    m = list(re.finditer(r"[\"'“”‘’「」『』]([^\"'“”‘’「」『』]*)[\"'“”‘’「」『』]", intent))
    if m:
        last = m[-1]
        return intent[:last.start()] + f'"{new_value}"' + intent[last.end():]
    m2 = re.search(r"(输入|填写|输入框输入)\s*\S*$", intent)
    if m2:
        return intent[:m2.start()] + f"{m2.group(1)} {new_value}"
    return intent

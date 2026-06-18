"""步骤⑪ 动作分发器 —— 统一执行入口.

dispatch(action) → (成功, 消息). 对需定位的动作先走五级链解析选择器, 再用 Playwright 执行.
消息里带"实际操作目标"(解析到的元素 HTML 片段), 供步骤⑫ 后校验判断是否点对.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..dom import extract_semantic_dom
from ..locating import LocatorResolver
from ..locating.playwright_api import info_key, infer_from_selector, normalize_info, resolve_locator
from ..planning import PlannedAction
from ..variable_substitution import substitute_variables
from .trace import ExecutionTrace
from .assert_or import combined_or_intent, is_or_assert, try_or_branches, try_or_heuristic
from .assert_scope import (
    build_semantic_text_summary,
    extract_page_regions,
    format_scope_note_for_semantic,
    parse_assert_scope,
    should_disable_semantic_fallback,
    try_field_value_assert,
    try_scoped_literal,
)
from .script_helpers import (
    assert_all_table_rows_contain,
    assert_no_table_row_contains,
    count_real_table_rows,
    extract_url_query,
    recover_active_page,
    wait_after_detail_submit,
    wait_and_recover_active_page,
    wait_before_assert,
    wait_for_list_count_at_least,
    _page_usable,
)
from .assert_codegen import (
    record_assert_count,
    record_control_mode,
    record_literal,
    record_or_branch,
    record_or_heuristic,
    record_post_click_wait,
    record_semantic_pass,
    set_codegen_assert,
)
from .target_text import backfill_click_from_html, extract_target_text
from .session_ops import execute_bind_session


class ActionDispatcher:
    """把 PlannedAction 转成 Playwright 调用, 并屏蔽定位与执行细节."""

    def __init__(
        self,
        page: Any,
        resolver: LocatorResolver,
        default_timeout_ms: int = 10000,
        trace: Optional[ExecutionTrace] = None,
        api_runner: Optional[Any] = None,       # 可预注入; 否则按 api_profile 懒加载
        api_profile: Optional[Any] = None,      # SystemProfile, 供步骤内 api_call 按需创建 runner
        session_ops_cfg: Optional[dict[str, Any]] = None,
        page_capture: Optional[dict[str, Any]] = None,
        llm: Optional[Any] = None,              # LLM 实例, 用于语义断言
        console: Optional[Any] = None,
        prompts: Optional[Any] = None,          # PromptLoader
    ) -> None:
        self.page = page
        self.resolver = resolver
        self.default_timeout = default_timeout_ms
        self.trace = trace
        self.api_runner = api_runner
        self._api_profile = api_profile
        self._session_ops_cfg = session_ops_cfg
        self._page_capture = page_capture
        self.api_context: dict[str, Any] = {}    # api_call 返回的变量, 用于后续动作替换
        self._last_click_label: Optional[str] = None
        self._llm_instance = llm
        self.console = console
        self._prompts = prompts
        self._list_tab_anchor: Optional[Any] = None
        self.popup_recovery_steps: list[PlannedAction] = []
        self._popup_dismiss_used = False
        self.popup_dismiss_before_intents: list[str] = []
        self.idempotent_skip_intents: list[str] = []
        self._page_snapshot: Optional[dict[str, Any]] = None

    def _snapshot_key(self) -> str:
        try:
            return (self.page.url or "").lower()
        except Exception:
            return ""

    def invalidate_page_snapshot(self) -> None:
        self._page_snapshot = None

    def page_snapshot_valid(self) -> bool:
        key = self._snapshot_key()
        return bool(key and self._page_snapshot and self._page_snapshot.get("key") == key)

    def refresh_page_snapshot(self, *, dom_summary: Optional[str] = None) -> dict[str, Any]:
        """操作改页后抓取页面快照, 供后续断言复用 (不再逐步重读 DOM)."""
        self._ensure_list_anchor()
        self.page, _ = recover_active_page(self.page, prefer=self._list_tab_anchor)
        body_text = self._read_body_text()
        regions = extract_page_regions(self.page)
        key = self._snapshot_key()
        self._page_snapshot = {
            "key": key,
            "body_text": body_text,
            "regions": regions,
            "dom_summary": dom_summary,
        }
        if self.trace:
            self.trace.emit("page_snapshot", url=key, cached=False, reason="after_operation")
        return self._page_snapshot

    def get_page_snapshot(self, *, allow_capture: bool = False) -> Optional[dict[str, Any]]:
        """断言读取上一操作后的页面快照; 默认不触发新的 DOM 读取."""
        if self.page_snapshot_valid():
            snap = self._page_snapshot
            if self.trace:
                self.trace.emit("page_snapshot", url=snap.get("key", ""), cached=True, reason="assert_reuse")
            return snap
        if not allow_capture:
            return None
        self._prepare_page_for_assert()
        return self.refresh_page_snapshot()

    def record_popup_recovery(self, steps: list[PlannedAction]) -> None:
        """记录运行期弹窗恢复动作, 供 codegen 生成条件式脚本."""
        for s in steps:
            if not any(x.intent == s.intent and x.type == s.type for x in self.popup_recovery_steps):
                self.popup_recovery_steps.append(s)

    def mark_popup_dismiss_used(self, before_intent: str | None = None) -> None:
        self._popup_dismiss_used = True
        if before_intent and before_intent not in self.popup_dismiss_before_intents:
            self.popup_dismiss_before_intents.append(before_intent)

    def mark_idempotent_skip(self, intent: str | None) -> None:
        if intent and intent not in self.idempotent_skip_intents:
            self.idempotent_skip_intents.append(intent)

    def popup_dismiss_was_used(self) -> bool:
        return self._popup_dismiss_used

    def set_page(self, page: Any) -> None:
        """新标签页切换后更新页面对象."""
        self.page = page
        self.invalidate_page_snapshot()

    def _ensure_live_page(self, *, poll: bool = False) -> bool:
        """当前 page 已关闭时切到仍打开的 tab (多 tab / 提交关详情页)."""
        before = self.page
        if poll:
            self.page, recovered = wait_and_recover_active_page(
                self.page, prefer=self._list_tab_anchor,
            )
        else:
            self.page, recovered = recover_active_page(
                self.page, prefer=self._list_tab_anchor,
            )
        if recovered and self.trace:
            try:
                url = self.page.url or ""
            except Exception:
                url = ""
            self.trace.emit("page_recover", url=url)
        return recovered or self.page is not before

    def _prepare_page_for_assert(self) -> None:
        """断言前: 轮询恢复存活 tab 并等待 DOM 稳定."""
        self._ensure_list_anchor()
        before = self.page
        try:
            before_url = before.url or ""
        except Exception:
            before_url = ""
        self.page = wait_before_assert(
            self.page, timeout_ms=5000, list_anchor=self._list_tab_anchor,
        )
        if not _page_usable(self.page) and self._list_tab_anchor is not None:
            if _page_usable(self._list_tab_anchor):
                self.page = self._list_tab_anchor
        if not self.trace:
            return
        try:
            after_url = self.page.url or ""
        except Exception:
            after_url = ""
        if self.page is not before or after_url != before_url:
            self.trace.emit("page_recover", url=after_url, reason="before_assert")

    def _read_body_text(self) -> str:
        """读取页面正文; 遇 TargetClosedError 时先恢复 tab 再重试."""
        self.page, _ = wait_and_recover_active_page(
            self.page, max_polls=20, prefer=self._list_tab_anchor,
        )
        if not _page_usable(self.page):
            if self._list_tab_anchor is not None and _page_usable(self._list_tab_anchor):
                self.page = self._list_tab_anchor
            else:
                return ""
        try:
            return self.page.inner_text("body")
        except Exception:
            self.page = wait_before_assert(
                self.page, timeout_ms=5000, list_anchor=self._list_tab_anchor,
            )
            if not _page_usable(self.page):
                return ""
            try:
                return self.page.inner_text("body")
            except Exception:
                return ""

    def dispatch(self, action: PlannedAction, case_id: str = "") -> tuple[bool, str]:
        """执行单个动作, 返回是否成功以及供日志/后校验使用的说明."""
        # 执行前变量替换: 把 action.value 和 action.intent 中的 ${var} 替换为 api_context 中的值
        self._substitute_action(action)
        t = action.type
        if action.is_assert():
            # 断言不操作页面, 复用上一操作后的 page_snapshot, 此处不 prepare、不重读 DOM.
            self._ensure_live_page()
        else:
            self.invalidate_page_snapshot()
            self._ensure_live_page()
        try:
            # 不需要元素定位的动作先直接处理.
            if t == "goto":
                self.page.goto(action.value or "", timeout=self.default_timeout)
                ok, msg = True, f"跳转到 {action.value}"
            elif t == "wait":
                ok, msg = self._wait(action)
            elif t in ("assert_text", "asset"):
                ok, msg = self._assert_text(action)
            elif t == "assert_table":
                ok, msg = self._assert_table(action)
            elif t == "assert_count":
                ok, msg = self._assert_count(action)
            elif t == "api_call":
                ok, msg = self._api_call(action)
            elif t == "bind_session":
                ok, msg = self._bind_session(action, case_id)
            elif t == "scroll":
                ok, msg = self._scroll(action)
            elif action.needs_locating():
                loc, target_html = self._resolve(action)
                if loc is None:
                    if self.trace:
                        self.trace.emit(
                            "locate",
                            source="失败",
                            selector=None,
                            target_html=None,
                            hint=action.resolve_hint,
                            exclude=action.exclude_selectors,
                        )
                    ok, msg = False, f"找不到元素: {action.intent}"
                else:
                    ok, msg = self._run_located(t, loc, action, target_html)
            else:
                ok, msg = False, f"未支持的动作类型: {t}"
            if self.trace:
                self.trace.emit("dispatch", type=t, ok=ok, message=msg)
            return ok, msg
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            if self.trace:
                self.trace.emit("dispatch", type=t, ok=False, message=msg)
            return False, msg

    # ---------- 变量替换 ----------
    def _substitute_action(self, action: PlannedAction) -> None:
        """把 action 中的 ${var} 替换为 api_context 中的值."""
        if not self.api_context:
            return
        if action.value:
            action.value = substitute_variables(action.value, self.api_context)
        action.intent = substitute_variables(action.intent, self.api_context)
        # 替换 extras 中的字符串值
        if action.extras:
            for k, v in action.extras.items():
                if isinstance(v, str):
                    action.extras[k] = substitute_variables(v, self.api_context)

    # ---------- 定位 ----------
    def _resolve(self, action: PlannedAction):
        if action.force_selector:
            info = normalize_info(infer_from_selector(action.force_selector))
            info["_source"] = "强制复用"
            if self.trace:
                self.trace.emit(
                    "locate_chain",
                    intent=action.intent,
                    action_type=action.type,
                    hint=action.resolve_hint,
                    exclude=action.exclude_selectors,
                    steps=[{"level": "强制复用", "status": "命中", "selector": action.force_selector, "note": ""}],
                    llm_called=False,
                    hit_level="强制复用",
                    hit_selector=action.force_selector,
                )
        else:
            # 正常路径交给五级定位链; exclude/hint 来自后校验重试.
            info = self.resolver.resolve(
                self.page, action.intent, action.type,
                exclude=action.exclude_selectors, hint=action.resolve_hint,
                action_value=action.value or "",
            )
        if not info:
            return None, None
        info = normalize_info(info)
        loc = resolve_locator(self.page, info)
        action.locator_info = info
        action.selector = info_key(info)
        target_html = None
        try:
            target_html = loc.evaluate("el => el.outerHTML.slice(0, 200)")
        except Exception:
            target_html = None
        if self.trace:
            self.trace.emit(
                "locate",
                source=info.get("_source", "未知"),
                selector=info_key(info),
                method=info.get("method"),
                nth=info.get("nth", 0),
                target_html=target_html,
                hint=action.resolve_hint,
            )
        return loc, target_html

    _SELECT_ANCESTOR_XPATH = (
        "xpath=ancestor::*["
        "contains(@class,'ant-select') or contains(@class,'el-select') or "
        "contains(@class,'n-select') or contains(@class,'arco-select') or "
        "contains(@class,'v-select')"
        "][1]"
    )

    def _click_combobox_trigger(self, loc: Any, timeout: int) -> None:
        """在已定位 combobox 元素的祖先链上点击最近的 Select 容器."""
        try:
            loc.locator(self._SELECT_ANCESTOR_XPATH).click(timeout=timeout)
            try:
                self.page.wait_for_selector(
                    '[role="listbox"], .ant-select-dropdown, .el-select-dropdown',
                    timeout=2000,
                )
            except Exception:
                pass
        except Exception:
            loc.click(timeout=timeout, force=True)

    # ---------- 已定位动作执行 ----------
    def _run_located(self, t: str, loc, action: PlannedAction, target_html: Optional[str]) -> tuple[bool, str]:
        timeout = self.default_timeout
        suffix = f" | 实际目标: {target_html}" if target_html else ""
        if t == "click":
            try:
                url_before = self.page.url or ""
            except Exception:
                url_before = ""
            try:
                role = loc.evaluate("el => el.getAttribute('role')")
            except Exception:
                role = None
            if role == "combobox":
                # combobox 多为 Select 内层 input. 必须在「已定位元素」的祖先链上
                # 找最近的下拉容器并点击, 禁止 page.locator('.ant-select').first()
                # (会误点页面上第一个下拉, 如把「来源」点成「状态」).
                self._click_combobox_trigger(loc, timeout)
            elif self._should_follow_new_tab(action, loc):
                self._click_and_follow_navigation(loc, timeout, action)
            else:
                loc.click(timeout=timeout)
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
                except Exception:
                    pass
            self._ensure_live_page(poll=True)
            try:
                url_after = self.page.url or ""
            except Exception:
                url_after = ""
            backfill_click_from_html(action, target_html)
            self._remember_click_label(action, target_html)
            record_post_click_wait(action, url_before, url_after)
            if self._is_detail_submit_click(action, url_before):
                ok_submit, submit_msg = self._wait_after_detail_submit(url_before)
                if not ok_submit:
                    if self.trace:
                        self.trace.emit("page_switch", url=url_after, intent=action.intent)
                    return ok_submit, submit_msg
                suffix = f"{suffix} | {submit_msg}"
            if self.trace:
                self.trace.emit("page_switch", url=url_after, intent=action.intent)
            return True, f"点击 {action.intent}{suffix}"
        if t == "hover":
            loc.hover(timeout=timeout)
            return True, f"悬停 {action.intent}{suffix}"
        if t == "fill":
            loc.fill(action.value or "", timeout=timeout)
            return True, f"输入 {action.value!r}{suffix}"
        if t == "press":
            loc.press(action.value or "Enter", timeout=timeout)
            return True, f"按键 {action.value}{suffix}"
        if t == "upload":
            loc.set_input_files(action.value or "", timeout=timeout)
            return True, f"上传 {action.value}{suffix}"
        return False, f"未支持的已定位动作: {t}"

    _NEW_TAB_INTENT_RE = re.compile(r"查看|新标签|新窗口|新开")
    _SUBMIT_CLICK_RE = re.compile(r"提交")

    def _is_detail_submit_click(self, action: PlannedAction, url_before: str) -> bool:
        if action.type != "click" or not self._SUBMIT_CLICK_RE.search(action.intent or ""):
            return False
        if "/detail" in (url_before or "").lower():
            return True
        try:
            return "/detail" in (self.page.url or "").lower()
        except Exception:
            return False

    def _ensure_list_anchor(self) -> None:
        """从 context 中查找仍存活的列表 Tab 作为锚点 (未经过「查看」开 Tab 时兜底)."""
        if self._list_tab_anchor is not None and _page_usable(self._list_tab_anchor):
            return
        try:
            for p in self.page.context.pages:
                if _page_usable(p) and count_real_table_rows(p) > 0:
                    self._list_tab_anchor = p
                    return
        except Exception:
            pass

    def _wait_after_detail_submit(self, url_before: str) -> tuple[bool, str]:
        self._ensure_list_anchor()
        max_polls = max(25, self.default_timeout // 200)
        self.page, outcome, recovered = wait_after_detail_submit(
            self.page,
            list_anchor=self._list_tab_anchor,
            url_before=url_before,
            max_polls=max_polls,
        )
        if self.trace:
            try:
                url = self.page.url or ""
            except Exception:
                url = ""
            self.trace.emit(
                "detail_submit_wait",
                outcome=outcome,
                url=url,
                recovered=recovered,
            )
        if outcome == "submit_error":
            return False, "提交失败: 页面提示任务已处理或不可重复提交"
        labels = {
            "returned_to_list": "提交后已回到列表页",
            "next_detail": "提交后已加载下一任务详情",
            "settled": "提交后页面已稳定",
            "timeout": "提交后等待页面结局超时",
        }
        return True, labels.get(outcome, f"提交后页面: {outcome}")

    def _should_follow_new_tab(self, action: PlannedAction, loc: Any) -> bool:
        """仅「查看」等会新开标签页的点击才跟随 popup, 避免普通按钮误切 tab."""
        intent = action.intent or ""
        if self._NEW_TAB_INTENT_RE.search(intent):
            return True
        try:
            if loc.evaluate("el => el.getAttribute('target')") == "_blank":
                return True
        except Exception:
            pass
        return False

    def _close_stale_secondary_tabs(
        self, ctx: Any, keep: set[Any], anchor_url: str,
    ) -> None:
        """关闭非保留且 URL 与锚定列表页不同的旧 tab (避免多次「查看」类操作堆积)."""
        for p in list(ctx.pages):
            if p in keep:
                continue
            try:
                if (p.url or "") != anchor_url:
                    p.close()
            except Exception:
                pass

    def _click_and_follow_navigation(
        self, loc: Any, timeout: int, action: Optional[PlannedAction] = None,
    ) -> None:
        """点击后跟随新 Tab (如「查看」打开详情页)."""
        ctx = self.page.context
        list_page = self.page
        self._list_tab_anchor = list_page
        count_before = len(ctx.pages)

        # 1) 等待 popup/new tab (expect_page 内 click 已执行, 超时勿重复点)
        try:
            with ctx.expect_page(timeout=timeout) as page_info:
                loc.click(timeout=timeout)
            new_page = page_info.value
            new_page.wait_for_load_state("domcontentloaded", timeout=timeout)
            self._close_stale_secondary_tabs(ctx, {list_page, new_page}, list_page.url or "")
            self.page = new_page
            self._list_tab_anchor = list_page
            try:
                new_page.bring_to_front()
            except Exception:
                pass
            return
        except Exception:
            pass

        # 2) 新 Tab 较慢: 轮询 context.pages
        poll_ms = 200
        polls = max(1, timeout // poll_ms)
        for _ in range(polls):
            pages = ctx.pages
            if len(pages) > count_before:
                for p in reversed(pages):
                    if p is not self.page:
                        try:
                            p.wait_for_load_state("domcontentloaded", timeout=timeout)
                        except Exception:
                            pass
                        self._close_stale_secondary_tabs(ctx, {list_page, p}, list_page.url or "")
                        self.page = p
                        self._list_tab_anchor = list_page
                        try:
                            p.bring_to_front()
                        except Exception:
                            pass
                        return
            try:
                self.page.wait_for_timeout(poll_ms)
            except Exception:
                break

    # ---------- 滚动 ----------
    def _scroll(self, action: PlannedAction) -> tuple[bool, str]:
        """滚动页面或指定元素.

        value 格式: "向下500" / "向上300" / "向左200" / "向右400" / "到元素底部" / "到元素顶部"
        如果 action 需要定位, 则滚动该元素所在容器; 否则滚动页面.
        """
        import re
        val = (action.value or action.intent or "").strip()

        # 滚动到指定元素
        if action.needs_locating():
            loc, target_html = self._resolve(action)
            if loc is None:
                return False, f"滚动: 找不到目标元素: {action.intent}"
            # 滚动元素到可视区域
            loc.scroll_into_view_if_needed(timeout=self.default_timeout)
            suffix = f" | 实际目标: {target_html}" if target_html else ""
            return True, f"滚动元素到可视区域{suffix}"

        # 页面滚动: 解析 value
        m = re.match(r'(?:向)?([上下左右])\s*(\d+)', val)
        if m:
            direction = m.group(1)
            amount = int(m.group(2))
            axis = "y" if direction in ("上", "下") else "x"
            delta = amount if direction in ("下", "右") else -amount
            self.page.evaluate(f"window.scrollBy({{'{axis}': {delta}}})")
            return True, f"页面{direction}滚动 {amount}px"

        # 默认: 尝试将 value 作为滚动时长或文本
        self.page.evaluate("window.scrollBy(0, 300)")
        return True, "页面向下滚动 300px"

    # ---------- 等待 ----------
    def _wait(self, action: PlannedAction) -> tuple[bool, str]:
        val = (action.value or "").strip()
        num = _as_duration_ms(val)
        if num is not None:
            self.page.wait_for_timeout(num)
            return True, f"等待 {num}ms"
        # 文本等待
        # 没有明确时长时, 把 value/intent 当作需要出现的页面文本.
        text = val or action.intent
        try:
            self.page.wait_for_function(
                "t => document.body && document.body.innerText.includes(t)",
                arg=text, timeout=self.default_timeout,
            )
            return True, f"等待文本出现 {text!r}"
        except Exception as e:  # noqa: BLE001
            return False, f"等待文本超时 {text!r}: {e}"

    # ---------- 数量断言 ----------
    def _assert_count(self, action: PlannedAction) -> tuple[bool, str]:
        """统计页面列表/表格数量并与 value、extras.operator 比较."""
        op, threshold = _parse_count_spec(action)
        actual, source = self._measure_list_count()
        ok = _compare_count(actual, threshold, op)
        sym = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "="}.get(op, op)
        status = "通过" if ok else "未通过"
        if ok:
            record_assert_count(action, op, threshold, source)
        return ok, f"计数断言({source}): 实际{actual} {sym} {threshold} → {status}"

    def _measure_list_count(self) -> tuple[int, str]:
        """优先读「当前总数为:N」, 否则统计表格数据行 (排除「暂无数据」占位行)."""
        try:
            body = self._read_body_text()
        except Exception:
            self._prepare_page_for_assert()
            body = self.page.content()
        m = re.search(r"当前总数为[:：]\s*(\d+)", body)
        if m:
            return int(m.group(1)), "当前总数"
        n = count_real_table_rows(self.page)
        if n > 0:
            return n, "表格行数"
        return 0, "未识别到列表"

    def _count_real_table_rows(self) -> int:
        return count_real_table_rows(self.page)

    @staticmethod
    def is_disabled_click_failure(message: str) -> bool:
        """分发失败是否因目标不可点 (disabled / not enabled)."""
        msg = (message or "").lower()
        return "not enabled" in msg or "disabled" in msg

    def click_goal_already_met(self) -> bool:
        """列表/表格已有有效数据行, 常见于「领取/新建」按钮已灰但目标状态已达成."""
        return self._count_real_table_rows() > 0

    def _should_semantic_fallback(self, scope) -> bool:
        return not should_disable_semantic_fallback(scope)

    def _try_assert_list_rows(
        self, action: PlannedAction, target: str, scope,
    ) -> Optional[tuple[bool, str]]:
        """列表「所有行含/不含某文本」— 扫表格行, 不依赖整页 body 字面匹配."""
        intent = action.intent or ""
        if not target:
            return None
        if action.negate:
            if not scope.negate_table_rows:
                return None
            ok, msg, _ = assert_no_table_row_contains(self.page, target)
            return ok, msg
        if not scope.all_table_rows:
            return None
        try:
            wait_for_list_count_at_least(self.page, 1, timeout_ms=min(self.default_timeout, 8000))
        except Exception:
            pass
        ok, msg, _ = assert_all_table_rows_contain(self.page, target)
        return ok, msg

    # ---------- 文本断言 (含否定断言 + LLM 语义兜底) ----------
    def _assert_text(self, action: PlannedAction) -> tuple[bool, str]:
        target = (action.value or action.intent or "").strip()
        if not target and not is_or_assert(action):
            return False, "断言缺少目标文本"
        intent = action.intent or ""
        scope = parse_assert_scope(intent, value=target, negate=action.negate)
        snap = self.get_page_snapshot(allow_capture=True)
        if snap is None:
            return False, "断言缺少页面快照: 请先执行会改变页面的操作步骤"
        body_text = snap["body_text"]
        regions = snap["regions"]

        if is_or_assert(action) and not action.negate:
            branches = (action.extras or {}).get("branches") or []
            if branches:
                hit = try_or_branches(self.page, branches, body_text)
                if hit is not None:
                    if hit[0]:
                        record_or_branch(action, self.page, branches, body_text)
                    return hit
            else:
                hit = try_or_heuristic(self.page, combined_or_intent(action))
                if hit is not None:
                    if hit[0]:
                        record_or_heuristic(action, self.page, combined_or_intent(action))
                    return hit
            ok, msg = self._semantic_assert(
                action, body_text, scope=scope, regions=regions, any_of=True,
                dom_summary=snap.get("dom_summary"),
            )
            if ok:
                record_semantic_pass(action, self.page, body_text)
            return ok, msg

        if scope.field_hint and not action.negate:
            field_hit = try_field_value_assert(scope, regions, scope.field_hint, target)
            if field_hit is not None:
                if field_hit[0]:
                    record_literal(action, target)
                return field_hit

        if not action.negate:
            scoped_hit = try_scoped_literal(scope, regions, target)
            if scoped_hit is not None:
                if scoped_hit[0]:
                    record_literal(action, target)
                return scoped_hit

        present = target in body_text
        # negate=true 表示"页面不应包含该文本".
        if action.negate:
            if not present:
                record_literal(action, target, negate=True)
            return (not present), (f"否定断言: 页面{'仍包含' if present else '不包含'} {target!r}")
        if present:
            record_literal(action, target)
            return True, (f"断言: 页面{'包含' if present else '不包含'} {target!r}")
        row_hit = self._try_assert_list_rows(action, target, scope)
        if row_hit is not None:
            if row_hit[0]:
                record_literal(action, target)
            return row_hit
        control = self._try_assert_control_mode(action)
        if control is not None:
            if control[0]:
                stats = self._read_control_stats()
                if stats is not None:
                    want_single = bool(re.search(r"单选|不能多选|互斥", action.intent or ""))
                    record_control_mode(action, stats, want_single=want_single)
            return control
        if not self._should_semantic_fallback(scope):
            return False, f"断言未通过: {action.intent!r} (目标文本 {target!r})"
        ok, msg = self._semantic_assert(
            action, body_text, scope=scope, regions=regions,
            dom_summary=snap.get("dom_summary"),
        )
        if ok:
            record_semantic_pass(action, self.page, body_text)
        return ok, msg

    def _read_control_stats(self) -> dict[str, Any] | None:
        try:
            return self.page.evaluate(
                """() => {
                  const root = document.querySelector('form') || document.body;
                  const radios = root.querySelectorAll('input[type=radio], .ant-radio-input');
                  const checks = root.querySelectorAll(
                    'input[type=checkbox]:not(.ant-checkbox-input), .ant-checkbox-input'
                  );
                  return { radio: radios.length, checkbox: checks.length };
                }"""
            )
        except Exception:
            return None

    def _try_assert_control_mode(self, action: PlannedAction) -> Optional[tuple[bool, str]]:
        """单选/多选等交互模式: 用 DOM 控件类型判断, 不依赖页面出现「单选」二字."""
        intent = action.intent or ""
        if not re.search(r"单选|多选|radio|checkbox|互斥|不能多选", intent, re.I):
            return None
        stats = self._read_control_stats()
        if stats is None:
            return False, "控件断言: 无法读取控件"

        want_single = bool(re.search(r"单选|不能多选|互斥", intent))
        want_multi = "多选" in intent and not want_single
        r, c = int(stats.get("radio", 0)), int(stats.get("checkbox", 0))

        if want_single:
            ok = r >= 2 and c == 0
            return ok, f"控件断言(单选): radio={r} checkbox={c} → {'通过' if ok else '未通过'}"
        if want_multi:
            ok = c >= 2
            return ok, f"控件断言(多选): checkbox={c} radio={r} → {'通过' if ok else '未通过'}"
        return None

    # ---------- LLM 语义断言兜底 ----------
    def _semantic_assert(
        self,
        action: PlannedAction,
        body_text: str,
        *,
        scope=None,
        regions: Optional[dict[str, str]] = None,
        dom_summary: Optional[str] = None,
        any_of: bool = False,
    ) -> tuple[bool, str]:
        """精确匹配失败时, 让 LLM 根据页面状态判断断言意图是否满足."""
        or_mode = any_of or is_or_assert(action)
        if scope is None:
            target = (action.value or action.intent or "").strip()
            scope = parse_assert_scope(action.intent or "", value=target, negate=action.negate)
        if regions is None:
            snap = self.get_page_snapshot(allow_capture=True) or {}
            regions = snap.get("regions") or {}
            body_text = snap.get("body_text") or body_text
            dom_summary = dom_summary if dom_summary is not None else snap.get("dom_summary")

        default_system = """你是 UI 自动化断言校验助手. 根据页面状态判断一个断言是否满足.
只输出 JSON: {"ok": true/false, "reason": "简短说明"}."""
        system = default_system
        if self._prompts is not None:
            system = self._prompts.system("semantic_assert", default_system)
        if or_mode:
            system += """
- **或断言**: intent 描述多个可接受结果 (如「没有A则B，有的话C」「B或C」). 只要当前页面满足**任一分支**的语义, 必须判 ok=true.
- 禁止因仅不符合其中一支就判 false; value 若只写了其中一支的文案, 仍以 intent 全部分支为准."""

        if not dom_summary:
            dom_summary = extract_semantic_dom(self.page, dialog_first=False, stable=False)
            if self.page_snapshot_valid() and self._page_snapshot is not None:
                self._page_snapshot["dom_summary"] = dom_summary
        text_summary = build_semantic_text_summary(body_text, regions, scope)
        or_note = "是 (满足任一分支即可)" if or_mode else "否"
        intent_text = combined_or_intent(action) if or_mode else (action.intent or "")
        scope_note = format_scope_note_for_semantic(scope)

        if self.console:
            self.console.print(f"  [dim]① 提取页面 DOM 摘要 (含标签/class/控件type)[/dim]")
            self.console.print(f"  [dim]② 提取页面文本摘要[/dim]")
            self.console.print(f"  [dim]③ 拼装 prompt 调用 LLM 语义分析[/dim]")
            self.console.print(f"  [dim]   断言意图: {intent_text}[/dim]")

        default_user = f"""断言意图: {intent_text}
断言目标(value): {action.value or "-"}
或断言: {or_note}
当前页面 URL: {self.page.url}

{scope_note}

页面 DOM 摘要 (含控件 type):
{dom_summary}

页面文本摘要:
{text_summary}

请输出 {{"ok": true/false, "reason": "..."}} JSON."""
        if self._prompts is not None:
            user = self._prompts.user(
                "semantic_assert",
                default_user,
                intent=intent_text,
                value=action.value or "-",
                or_note=or_note,
                url=self.page.url,
                scope_note=scope_note,
                dom_summary=dom_summary,
                text_summary=text_summary,
            )
        else:
            user = default_user

        try:
            llm: LLMAdapter = self._llm
            data = llm.complete_json("semantic_assert", system, user).data
            ok = bool(data.get("ok")) if isinstance(data, dict) else False
            reason = str(data.get("reason", "")) if isinstance(data, dict) else ""
            if self.console:
                self.console.print(f"  [dim]④ LLM 返回: ok={ok}, reason={reason}[/dim]")
            return ok, f"语义断言: {'满足' if ok else '不满足'} ({reason})"
        except Exception:
            return False, f"语义断言 LLM 调用失败, 精确匹配也未找到 {action.intent!r}"

    @property
    def _llm(self):
        """获取 LLM 实例, 由外部注入."""
        return getattr(self, "_llm_instance", None)

    @_llm.setter
    def _llm(self, value):
        self._llm_instance = value

    def _assert_table(self, action: PlannedAction) -> tuple[bool, str]:
        """断言表格中某行某列的值 (行由 value / extras.row_key 标识)."""
        extras = action.extras or {}
        row_key = (action.value or extras.get("row_key") or "").strip()
        key_col = str(extras.get("row_key_column") or "工单ID").strip()
        target_col = str(extras.get("column") or "").strip()
        expected = str(extras.get("expected") or extras.get("cell_value") or "").strip()
        if not row_key:
            return False, "assert_table 缺少行标识 (value 或 extras.row_key)"
        if not target_col or not expected:
            return False, "assert_table 缺少 extras.column 或 extras.expected"

        tables = self.page.locator("table")
        n_tables = tables.count()
        for ti in range(n_tables):
            table = tables.nth(ti)
            headers = [h.strip() for h in table.locator("thead th, thead td").all_inner_texts()]
            if not headers:
                continue
            if key_col not in headers or target_col not in headers:
                continue
            key_idx = headers.index(key_col)
            col_idx = headers.index(target_col)
            body_rows = table.locator("tbody tr")
            for ri in range(body_rows.count()):
                cells = [c.strip() for c in body_rows.nth(ri).locator("td").all_inner_texts()]
                if key_idx >= len(cells):
                    continue
                if row_key not in cells[key_idx]:
                    continue
                actual = cells[col_idx] if col_idx < len(cells) else ""
                if expected in actual or actual == expected:
                    return True, (
                        f"表格断言: 行标识 {row_key!r} 列 {target_col!r} "
                        f"期望 {expected!r} 实际 {actual!r}"
                    )
                return False, (
                    f"表格断言: 行标识 {row_key!r} 列 {target_col!r} "
                    f"期望 {expected!r} 实际 {actual!r}"
                )
        return False, (
            f"表格断言: 未找到行标识 {row_key!r} (列 {key_col!r}) "
            f"或列 {target_col!r}"
        )

    def _ensure_api_runner(self) -> bool:
        """首次 api_call 时按业务 profile 懒加载 ApiRunner (与前置是否含 API 无关)."""
        if self.api_runner is not None:
            return True
        profile = self._api_profile
        if not profile or not getattr(profile, "apis", None):
            return False
        try:
            from ..api_client import APIClient
            from ..api_runner import ApiRunner

            self.api_runner = ApiRunner(APIClient(profile), profile)
            self.api_runner.context.update(self.api_context)
            return True
        except Exception:
            return False

    def _remember_click_label(self, action: PlannedAction, target_html: Optional[str]) -> None:
        label = (action.value or "").strip()
        if not label:
            label = (extract_target_text(target_html) or "").strip()
        if not label:
            m = re.search(r"[「']([^」']+)[」']", action.intent or "")
            if m:
                label = m.group(1).strip()
        if label:
            self._last_click_label = label

    # ---------- API 调用 ----------
    def _bind_session(self, action: PlannedAction, case_id: str) -> tuple[bool, str]:
        if not self._ensure_api_runner():
            return False, "bind_session 失败: API runner 未初始化"
        try:
            import sys
            sys.stdout.write(f"\n  📎 会话记录: {action.intent}\n")
            sys.stdout.flush()
            ok, msg, _entry = execute_bind_session(
                self.page,
                self.api_context,
                api_runner=self.api_runner,
                case_id=case_id,
                prev_click=self._last_click_label,
                intent=action.intent or "",
                extras=action.extras,
                session_ops_cfg=self._session_ops_cfg,
                page_capture=self._page_capture,
            )
            sys.stdout.write(f"  └─ {msg}\n" if ok else f"  └─ {msg}\n")
            sys.stdout.flush()
            return ok, msg
        except Exception as e:  # noqa: BLE001
            return False, f"bind_session 失败: {e}"

    def _api_call(self, action: PlannedAction) -> tuple[bool, str]:
        """执行 api_call 动作: 把 intent 描述交给 API runner 执行, 并回填变量到动作列表."""
        if not self._ensure_api_runner():
            return False, "API runner 未初始化, 无法执行 api_call (无业务 API 配置)"
        try:
            import sys
            self.api_runner.context.update(self.api_context)
            for k, v in extract_url_query(self.page, "uniqId", "workId", "orderId").items():
                self.api_runner.context.setdefault(k, v)
            sys.stdout.write(f"\n  📡 API 调用: {action.intent}\n")
            sys.stdout.write(f"  ├─ 上下文变量: {self._format_api_context()}\n")
            sys.stdout.flush()
            runner_before = dict(self.api_runner.context)
            context = self.api_runner.run_preconditions([action.intent])
            if context:
                self.api_context.update(context)
                delta = {
                    k: v for k, v in context.items()
                    if runner_before.get(k) != v
                    and k != "ops"
                }
                if delta:
                    vars_summary = ", ".join(f"{k}={v}" for k, v in delta.items())
                else:
                    key_field = (self._session_ops_cfg or {}).get("ops_key_field", "orderId")
                    one = context.get(key_field)
                    vars_summary = f"{key_field}={one}" if one is not None else "(无变更)"
                sys.stdout.write(f"  └─ 返回: {vars_summary}\n")
                sys.stdout.flush()
                return True, f"API 调用成功: {vars_summary}"
            sys.stdout.write(f"  └─ 未返回有效结果\n")
            sys.stdout.flush()
            return False, "API 调用未返回有效结果"
        except Exception as e:  # noqa: BLE001
            sys.stdout.write(f"  └─ 失败: {e}\n")
            sys.stdout.flush()
            return False, f"API 调用失败: {e}"

    def _format_api_context(self) -> str:
        ctx = getattr(self.api_runner, "context", None) or self.api_context
        if not ctx:
            return "(空)"
        skip = {"ops"}
        items = [f"{k}={v}" for k, v in ctx.items() if k not in skip]
        return ", ".join(items) if items else "(空)"


def _parse_count_spec(action: PlannedAction) -> tuple[str, int]:
    """从 value / extras / intent 解析比较符与阈值, 如 >1、>=10、数量大于3."""
    extras = action.extras or {}
    val = (action.value or "").strip()
    op = str(extras.get("operator") or "==")
    threshold = 0

    m = re.fullmatch(r"(>=|<=|>|<|==?)?\s*(\d+)", val)
    if m:
        if m.group(1):
            op = m.group(1)
            if op == "=":
                op = "=="
        threshold = int(m.group(2))
    elif val.isdigit():
        threshold = int(val)

    if threshold == 0 and not val:
        intent = action.intent or ""
        for pattern, sym in (
            (r"大于\s*(\d+)", ">"),
            (r"不少于\s*(\d+)", ">="),
            (r"小于\s*(\d+)", "<"),
            (r"等于\s*(\d+)", "=="),
            (r"数量为\s*(\d+)", "=="),
            (r"(\d+)\s*条", "=="),
        ):
            im = re.search(pattern, intent)
            if im:
                op, threshold = sym, int(im.group(1))
                break
    return op, threshold


def _compare_count(actual: int, threshold: int, op: str) -> bool:
    return {
        "==": actual == threshold,
        ">": actual > threshold,
        ">=": actual >= threshold,
        "<": actual < threshold,
        "<=": actual <= threshold,
    }.get(op, actual == threshold)


def _as_duration_ms(val: str) -> Optional[int]:
    """'2000' / '2秒' / '2s' / '500ms' → 毫秒; 非时长返回 None."""
    if not val:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|毫秒|s|秒|)?\s*", val)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or ""
    if unit in ("ms", "毫秒"):
        return int(n)
    if unit in ("s", "秒", ""):
        # 纯数字默认按秒? 设计文档时长模式; >100 视为毫秒, 否则秒
        return int(n * 1000) if (unit or n <= 60) else int(n)
    return int(n)

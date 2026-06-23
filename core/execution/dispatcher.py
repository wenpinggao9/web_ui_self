"""步骤⑪ 动作分发器 —— 统一执行入口.

dispatch(action) → (成功, 消息). 对需定位的动作先走五级链解析选择器, 再用 Playwright 执行.
消息里带"实际操作目标"(解析到的元素 HTML 片段), 供步骤⑫ 后校验判断是否点对.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..locating.resolver import LocatorResolver

from ..dom import extract_items, compact_dom_lines, wait_for_dom_stable
from ..locating.dialog_awareness import (
    intent_may_trigger_dialog,
    try_retrigger_dialog,
)
from ..locating.page_guard import PageNotReadyError, PageReadyGuard
from ..locating.intent_align import detect_feature_titles_menu_nav
from ..locating.playwright_api import info_key, infer_from_selector, normalize_info, resolve_locator
from ..planning import PlannedAction
from ..variable_substitution import substitute_variables
from .session_ops import (
    explicit_row_keys_from_action,
    resolve_assert_table_row_key,
)
from .trace import ExecutionTrace, print_captured_dom
from .assert_or import combined_or_intent, is_or_assert, try_or_branches, try_or_heuristic
from .entity_discover import discover_active_entity, discover_page_entity
from .post_submit_eval import build_live_submit_facts
from .submit_post_verify import submit_dispatch_should_succeed
from .assert_scope import (
    _text_contains,
    build_semantic_text_summary_from_items,
    format_scope_note_for_semantic,
    items_flat_text,
    parse_assert_scope,
    should_disable_semantic_fallback,
    try_field_value_assert_items,
    try_scoped_literal_items,
)
from .tab_follow import DEFAULT_SUBMIT_WAIT_MS, follow_active_tab
from .script_helpers import (
    assert_all_table_rows_contain,
    assert_no_table_row_contains,
    bring_page_to_front,
    count_real_table_rows,
    extract_url_query,
    locate_button_in_table_row,
    parse_table_row_click,
    FIRST_TABLE_ROW_KEY,
    recover_active_page,
    find_list_tab_anchor,
    find_newest_non_anchor_tab,
    pick_surviving_tab_after_detail_close,
    recover_after_submit_tab_close,
    wait_after_detail_submit,
    wait_and_recover_active_page,
    wait_before_assert,
    wait_for_list_count_at_least,
    find_sibling_tab_anchor,
    is_detail_submission_url,
    is_same_tab_detail_entity_nav,
    submit_left_detail_context,
    url_matches_anchor,
    _reload_list_page,
    _context_from_any,
    _page_alive,
    _page_usable,
    _button_label_variants,
    _url_query_id,
    _url_safe,
)
from .assert_codegen import (
    record_assert_count,
    record_button_state,
    record_control_mode,
    record_literal,
    record_or_branch,
    record_or_heuristic,
    record_post_click_wait,
    record_semantic_pass,
    set_codegen_assert,
)
from .page_session import PageSession
from .target_text import backfill_click_from_html, extract_target_text
from .deterministic_recovery import record_fill_history, remember_option_click
from .session_ops import (
    execute_bind_session,
    get_table_columns_cfg,
    resolve_click_row_candidates,
    resolve_table_row_key,
    table_row_key_matches,
    _ops_resolve_hint,
)

_DROPDOWN_VISIBLE_SEL = (
    ".ant-select-dropdown:visible, .el-select-dropdown:visible, [role='listbox']:visible"
)


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
        *,
        page_ready_guard: bool = True,
        dialog_retrigger: bool = True,
    ) -> None:
        self._session = PageSession(active=page)
        self.resolver = resolver
        self.default_timeout = default_timeout_ms
        self.trace = trace
        self.api_runner = api_runner
        self._api_profile = api_profile
        self._session_ops_cfg = session_ops_cfg
        self._page_capture = page_capture
        self.api_context: dict[str, Any] = {}    # api_call 返回的变量, 用于后续动作替换
        self._last_click_label: Optional[str] = None
        self._last_dialog_trigger: Optional[str] = None
        self._llm_instance = llm
        self.console = console
        self._prompts = prompts
        self.popup_recovery_steps: list[PlannedAction] = []
        self.feature_titles: list[str] = []
        self._popup_dismiss_used = False
        self.popup_dismiss_before_intents: list[str] = []
        self.idempotent_skip_intents: list[str] = []
        self._fill_history: list[dict[str, Any]] = []
        self._expected_radio_label: Optional[str] = None
        self.last_dispatch_meta: dict[str, Any] = {}
        self.page_ready_guard = page_ready_guard
        self.dialog_retrigger = dialog_retrigger

    @property
    def page(self) -> Any:
        return self._session.active

    @page.setter
    def page(self, value: Any) -> None:
        self._session.active = value

    @property
    def _list_tab_anchor(self) -> Any:
        return self._session.list_anchor

    @_list_tab_anchor.setter
    def _list_tab_anchor(self, value: Any) -> None:
        self._session.list_anchor = value

    @property
    def _page_state(self) -> Optional[dict[str, Any]]:
        return self._session.page_state

    @_page_state.setter
    def _page_state(self, value: Optional[dict[str, Any]]) -> None:
        self._session.page_state = value

    _SKIP_PAGE_STATE_CAPTURE = frozenset({
        "api_call",
        "assert_text", "asset", "assert_table", "assert_count",
    })

    @classmethod
    def should_capture_page_state(cls, action: PlannedAction) -> bool:
        """UI 类动作执行后抓 DOM (不论成败); 仅 api_call / 断言跳过."""
        return action.type not in cls._SKIP_PAGE_STATE_CAPTURE and not action.is_assert()

    def invalidate_page_state(self) -> None:
        self._session.invalidate_dom()

    def finish_submit_step_handoff(
        self,
        meta: Optional[dict[str, Any]] = None,
        recovered_page: Any = None,
        *,
        recapture: bool = True,
    ) -> None:
        """详情提交步结束: 强制 handoff 到存活 tab 并重抓 DOM (后校验 PASS 前调用)."""
        merged = self._session.finish_detail_submit_handoff(
            meta,
            recovered_page=recovered_page,
            recapture=recapture,
            capture_fn=self.capture_page_state_after_operation,
            should_reload_fn=self._should_reload_after_tab_handoff,
        )
        self.last_dispatch_meta = merged

    def sync_page_after_post_check(
        self,
        recovered_page: Any = None,
        meta: Optional[dict[str, Any]] = None,
        *,
        recapture: bool = True,
    ) -> None:
        """后校验切 tab 后同步 page/list_anchor, 并在存活 tab 上重抓 DOM 供下一步断言/定位."""
        merged = dict(self.last_dispatch_meta or {})
        if meta:
            merged.update(meta)

        if PageSession.needs_list_tab_handoff(merged):
            self.finish_submit_step_handoff(merged, recovered_page, recapture=recapture)
            return

        if (
            recapture
            and PageSession.is_same_tab_submit_nav(merged)
        ):
            if meta:
                self.last_dispatch_meta = dict(meta)
            url_before = str(merged.get("url_before") or "")
            outcome = str(merged.get("navigation_outcome") or "")
            self.page, url_after = self._prefer_submit_outcome_page(
                url_before,
                outcome,
                str(merged.get("url_after") or _url_safe(self.page)),
            )
            merged["url_after"] = url_after
            merged["left_detail_context"] = False
            self.last_dispatch_meta = merged
            if recovered_page is not None:
                self.page = recovered_page
                bring_page_to_front(recovered_page)
            if not self._session.cache_matches_active():
                self.invalidate_page_state()
                if _page_usable(self.page) or _url_safe(self.page):
                    nav = str(merged.get("navigation_outcome") or "")
                    self.capture_page_state_after_operation(nav_outcome=nav)
            self.page = self._session.active
            return

        if meta:
            self.last_dispatch_meta = dict(meta)
        if recovered_page is not None:
            self.page = recovered_page
            bring_page_to_front(recovered_page)
            if self._list_tab_anchor is None or not _page_usable(self._list_tab_anchor):
                self._list_tab_anchor = recovered_page
        if not self._should_recapture_dom_after_post_check(merged, recapture):
            return
        self.invalidate_page_state()
        if not _page_usable(self.page):
            self._ensure_list_anchor()
            self.page, _ = wait_and_recover_active_page(
                self.page, max_polls=30, prefer=self._list_tab_anchor,
            )
        if _page_usable(self.page):
            if self._should_reload_after_tab_handoff(merged, recovered_page):
                _reload_list_page(self.page)
            nav = str(merged.get("navigation_outcome") or "returned_to_list")
            self.capture_page_state_after_operation(nav_outcome=nav)

    def _should_recapture_dom_after_post_check(
        self,
        meta: dict[str, Any],
        recapture: bool,
    ) -> bool:
        """仅 tab 关闭/回列表或 URL 与缓存不一致时重抓 DOM (dispatch 已抓过则跳过)."""
        if not recapture:
            return False
        if meta.get("detail_tab_closed") or meta.get("left_detail_context"):
            return True
        cur_key = self._page_state_key()
        cached_key = str((self._page_state or {}).get("key") or "")
        if cur_key and cached_key and cur_key != cached_key:
            return True
        if self._has_cached_page_state() and cur_key and cached_key == cur_key:
            return False
        return not self._has_cached_page_state() and _page_usable(self.page)

    @staticmethod
    def _should_reload_after_tab_handoff(
        meta: dict[str, Any],
        recovered_page: Any,
    ) -> bool:
        """详情 tab 关闭并切到兄弟 tab 后刷新, 避免 stale DOM / 半加载状态."""
        if meta.get("detail_tab_closed") or meta.get("left_detail_context"):
            return True
        if recovered_page is None:
            return False
        url_before = str(meta.get("url_before") or "")
        if not is_detail_submission_url(url_before):
            return False
        url_after = str(meta.get("url_after") or "")
        outcome = str(meta.get("navigation_outcome") or "")
        if outcome in ("timeout", "settled", "returned_to_list"):
            return True
        if outcome in ("resource_id_changed", "route_changed"):
            return bool(url_after) and not is_detail_submission_url(url_after)
        return False

    def _page_state_key(self) -> str:
        try:
            return (self.page.url or "").lower()
        except Exception:
            return ""

    def capture_page_state_after_operation(
        self, *, nav_outcome: str = "",
    ) -> None:
        """UI 动作执行后抓取 semantic_items (post_verify profile), 供后校验/断言/下一步定位共用."""
        nav = nav_outcome or (self.last_dispatch_meta or {}).get("navigation_outcome")
        meta = self.last_dispatch_meta or {}
        url_before = str(meta.get("url_before") or "")
        outcome = str(nav or "")

        if outcome in ("resource_id_changed", "route_changed") and is_detail_submission_url(url_before):
            self.page, url_now = self._prefer_submit_outcome_page(
                url_before, outcome, _url_safe(self.page),
            )
            same_tab_entity = True
        else:
            url_now = _url_safe(self.page)
            same_tab_entity = is_same_tab_detail_entity_nav(
                url_before, url_now, outcome,
            )
            need_poll = (
                not same_tab_entity
                and outcome in (
                    "returned_to_list", "resource_id_changed", "route_changed",
                    "timeout", "settled",
                )
            )
            self._ensure_live_page(poll=need_poll)
            if not same_tab_entity:
                self.page, _ = recover_active_page(
                    self.page, prefer=self._list_tab_anchor,
                )

        bring_page_to_front(self.page)
        if not _page_alive(self.page):
            self._page_state = None
            return
        if same_tab_entity:
            wait_for_dom_stable(self.page, quiet_ms=150, timeout_ms=2000)
        elif outcome in ("returned_to_list", "resource_id_changed", "route_changed"):
            if outcome == "returned_to_list":
                self.page = wait_before_assert(
                    self.page, quiet_ms=300, timeout_ms=6000,
                    list_anchor=self._list_tab_anchor,
                )
            else:
                wait_for_dom_stable(self.page, quiet_ms=200, timeout_ms=4000)
        else:
            wait_for_dom_stable(self.page, quiet_ms=200, timeout_ms=4000)
        fw = self.resolver._framework_selectors if self.resolver else None
        items = extract_items(
            self.page, profile="post_verify", dialog_first=True,
            stable=False, selectors=fw,
        )
        dom_summary = compact_dom_lines(items)
        key = self._page_state_key()
        self._page_state = {
            "key": key,
            "semantic_items": items,
            "dom_summary": dom_summary,
        }
        print_captured_dom(
            self.console, items,
            label="抓取", source="操作后 post_verify",
        )
        if self.trace:
            self.trace.emit(
                "page_state_capture",
                url=key,
                count=len(items),
            )

    def get_semantic_items_for_resolve(self, intent: str = "") -> tuple[list[dict], str]:
        """定位时优先复用操作后 DOM; 无缓存则 PageReady + stable + 实时抽取."""
        self._ensure_live_page()
        bring_page_to_front(self.page)
        key = self._page_state_key()
        if self._page_state and PageSession.url_keys_equivalent(
            str(self._page_state.get("key") or ""), key,
        ):
            items = self._page_state.get("semantic_items") or []
            if items:
                return items, "操作后共用"
        return self._extract_fresh_semantic_items(intent)

    def _extract_fresh_semantic_items(self, intent: str = "") -> tuple[list[dict], str]:
        """PageReadyGuard + DOM 稳定 + 最多 3 次抽取."""
        if self.page_ready_guard:
            try:
                PageReadyGuard.ensure_ready(self.page, intent or "", max_wait_time=5000)
            except PageNotReadyError:
                try:
                    self.page.wait_for_function(
                        "() => !location.href.includes('/login') && !location.href.includes('/signin')",
                        timeout=10000,
                    )
                except Exception:
                    pass
            wait_for_dom_stable(self.page, quiet_ms=500, timeout_ms=3000)
        fw = self.resolver._framework_selectors if self.resolver else None
        items: list[dict] = []
        for attempt in range(3):
            items = extract_items(
                self.page, profile="locate", dialog_first=True,
                stable=True, selectors=fw,
            )
            if items:
                break
            if attempt < 2:
                self.page.wait_for_timeout(300)
        print_captured_dom(
            self.console, items,
            label="抓取", source="定位实时 locate",
        )
        return items, "定位实时"

    def _prepare_semantic_items_for_locate(
        self, action: PlannedAction,
    ) -> tuple[list[dict], str]:
        """共用 DOM 或实时抽取; click 弹窗感知重触发."""
        items, source = self.get_semantic_items_for_resolve(action.intent or "")
        if action.type != "click" or not items or not self.dialog_retrigger:
            return items, source

        def _reextract() -> list[dict]:
            fresh, _ = self._extract_fresh_semantic_items(action.intent or "")
            return fresh

        new_items, retriggered = try_retrigger_dialog(
            self.page,
            action.intent or "",
            items,
            self._last_dialog_trigger,
            extract_fn=_reextract,
        )
        if retriggered:
            print_captured_dom(
                self.console, new_items,
                label="抓取", source="弹窗重触发",
            )
            return new_items, "弹窗重触发"
        return items, source

    def _record_dialog_trigger(self, action: PlannedAction) -> None:
        sel = action.selector or info_key(action.locator_info or {})
        if sel and intent_may_trigger_dialog(action.intent or ""):
            self._last_dialog_trigger = sel

    def get_cached_semantic_items(self) -> Optional[list[dict]]:
        if not self._page_state:
            return None
        if not PageSession.url_keys_equivalent(
            str(self._page_state.get("key") or ""), self._page_state_key(),
        ):
            return None
        return self._page_state.get("semantic_items") or None

    def get_cached_dom_summary(self) -> Optional[str]:
        if not self._page_state:
            return None
        return str(self._page_state.get("dom_summary") or "") or None

    def capture_page_state_if_needed(self, action: PlannedAction) -> None:
        if self.should_capture_page_state(action):
            self.capture_page_state_after_operation()

    def _has_cached_page_state(self) -> bool:
        return self._session.has_dom_cache()

    def _can_fast_assert(self) -> bool:
        """同一 URL 下连续断言可复用操作后缓存, 不必重复等 DOM."""
        if not self._has_cached_page_state():
            print(f"  [yellow]FastAssert: 无缓存 → force_live[/yellow]")
            return False
        if not self._session.cache_matches_active():
            ck = ""
            try:
                ck = str((self._page_state or {}).get("key") or "")[:80]
            except Exception:
                ck = "<err>"
            ak = ""
            try:
                ak = self._session.page_key()[:80]
            except Exception:
                ak = "<err>"
            print(f"  [yellow]FastAssert: cache不匹配 cache_key={ck} active_key={ak} → force_live[/yellow]")
            return False
        # 只做轻量存活探测, 不用 evaluate (超时 ≠ 不可用).
        try:
            alive = self.page is not None and not self.page.is_closed()
            if not alive:
                print(f"  [yellow]FastAssert: page已死 → force_live[/yellow]")
            return alive
        except Exception:
            print(f"  [yellow]FastAssert: page探测异常 → force_live[/yellow]")
            return False

    def _cached_assert_url(self) -> str:
        """断言 URL: 始终读当前 tab 实时 URL."""
        return _url_safe(self.page)

    def _recover_page_pointer_best_effort(self, *, poll: bool = True) -> None:
        """尽量把 page 指到存活 tab, 供断言后后续 click 使用; 不 invalidate DOM 缓存."""
        if _page_usable(self.page, timeout_ms=800):
            bring_page_to_front(self.page)
            return
        self._ensure_list_anchor()
        polls = 30 if poll else 15
        self.page, _ = wait_and_recover_active_page(
            self.page, max_polls=polls, prefer=self._list_tab_anchor,
        )
        if not _page_usable(self.page, timeout_ms=800):
            ctx = _context_from_any(self.page, self._list_tab_anchor)
            if ctx is not None:
                for p in reversed(ctx.pages):
                    if _page_usable(p, timeout_ms=800):
                        self.page = p
                        break
        if _page_usable(self.page, timeout_ms=800):
            bring_page_to_front(self.page)

    def _assert_needs_live_page(self, action: PlannedAction) -> bool:
        """仅控件类断言需要 page.evaluate; 区域/字面断言可读缓存 DOM."""
        intent = action.intent or ""
        return bool(re.search(r"单选|多选|互斥|radio|checkbox", intent, re.I))

    def _prepare_page_for_assert(self, action: Optional[PlannedAction] = None) -> None:
        """断言前 tab 跟随 (anchor 模型)."""
        del action
        self._ensure_assert_tab(quick=False)
        self.page, _, _ = follow_active_tab(self.page, self._list_tab_anchor)

    def _ensure_assert_tab(self, *, quick: bool = False) -> bool:
        """断言前 tab 跟随 + 短探测 (不复用 DOM 缓存)."""
        del quick
        meta = self.last_dispatch_meta or {}
        outcome = str(meta.get("navigation_outcome") or "")
        url_before = str(meta.get("url_before") or "")
        if (
            outcome in ("resource_id_changed", "route_changed")
            and is_detail_submission_url(url_before)
        ):
            self.page, _ = self._prefer_submit_outcome_page(
                url_before, outcome, _url_safe(self.page),
            )
            if _page_usable(self.page, timeout_ms=1500) or _url_safe(self.page):
                bring_page_to_front(self.page)
                return True
        self._ensure_list_anchor()
        self.page, _, _ = follow_active_tab(self.page, self._list_tab_anchor)
        tout = 1500
        if not _page_usable(self.page, timeout_ms=tout):
            self.page, _ = wait_and_recover_active_page(
                self.page, max_polls=15,
                prefer=self._list_tab_anchor,
            )
        if _page_usable(self.page, timeout_ms=tout):
            self.page = wait_before_assert(
                self.page,
                quiet_ms=300,
                timeout_ms=3000,
                list_anchor=self._list_tab_anchor,
            )
            bring_page_to_front(self.page)
            return True
        return _page_alive(self.page)

    def _get_page_state_for_assert(
        self,
    ) -> tuple[bool, list[dict], str, str]:
        """断言: tab 跟随; 同 URL 连续断言复用操作后 DOM, 否则实时重抓."""
        return self._session.context_for_assert(
            capture_fn=self.capture_page_state_after_operation,
            ensure_tab_fn=self._ensure_assert_tab,
            trace=self.trace,
            force_live=not self._can_fast_assert(),
        )

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
        """新标签页/角色切换后更新页面对象, 并将窗口置于最前."""
        self.page = page
        self.invalidate_page_state()
        bring_page_to_front(page)

    def _ensure_live_page(self, *, poll: bool = False) -> bool:
        """当前 page 已关闭时切到 anchor; active 仍存活则不换 tab."""
        self._ensure_list_anchor()
        before = self.page
        child_open = (
            _page_alive(self.page)
            and self._list_tab_anchor is not None
            and self.page is not self._list_tab_anchor
        ) or find_newest_non_anchor_tab(
            _context_from_any(self.page, self._list_tab_anchor),
            self._list_tab_anchor,
            self.page,
        ) is not None
        self.page, switched, _ = follow_active_tab(self.page, self._list_tab_anchor)
        if _page_usable(self.page, timeout_ms=400):
            return switched or self.page is not before
        if poll:
            max_polls = 4 if child_open else 10
            prefer = self.page if _page_alive(self.page) else self._list_tab_anchor
            self.page, recovered = wait_and_recover_active_page(
                self.page, max_polls=max_polls, prefer=prefer,
            )
        else:
            self.page, recovered = recover_active_page(
                self.page,
                prefer=self.page if _page_alive(self.page) else self._list_tab_anchor,
            )
        if (recovered or switched) and self.trace:
            try:
                url = self.page.url or ""
            except Exception:
                url = ""
            self.trace.emit("page_recover", url=url)
        return recovered or switched or self.page is not before

    def dispatch(self, action: PlannedAction, case_id: str = "") -> tuple[bool, str]:
        """执行单个动作, 返回是否成功以及供日志/后校验使用的说明."""
        self.last_dispatch_meta = {}
        # 执行前变量替换: 把 action.value 和 action.intent 中的 ${var} 替换为 api_context 中的值
        self._substitute_action(action)
        t = action.type
        if action.is_assert():
            if self._can_fast_assert():
                bring_page_to_front(self.page)
            else:
                self._prepare_page_for_assert(action)
        else:
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
                    if (action.extras or {}).get("skip_navigation"):
                        ok, msg = True, f"已在目标页, 跳过: {action.intent}"
                    elif self.trace:
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
        # assert_table: 残留 ${} 或未解析索引语义 → 从 intent + ops 解析行主键
        if action.type == "assert_table":
            row_key = (action.value or (action.extras or {}).get("row_key") or "").strip()
            if not row_key.isdigit() or "${" in row_key:
                resolve_assert_table_row_key(
                    action, self.api_context, self._session_ops_cfg,
                )

    # ---------- 定位 ----------
    def _wait_for_dropdown_visible(self, timeout: Optional[int] = None) -> bool:
        """等待下拉面板可见 (Playwright 语义等待, 替代固定 sleep)."""
        ms = timeout if timeout is not None else self.default_timeout
        try:
            self.page.wait_for_selector(_DROPDOWN_VISIBLE_SEL, timeout=ms)
            return True
        except Exception:
            return False

    def _ensure_select_dropdown_open(self, intent: str) -> None:
        """选下拉选项前若面板未展开, 重新点开上一步操作过的筛选框."""
        from ..locating.intent_route import is_dropdown_option

        if not is_dropdown_option(intent):
            return
        page = self.page
        try:
            if page.locator(_DROPDOWN_VISIBLE_SEL).count():
                return
        except Exception:
            pass
        label = (self._last_click_label or "").strip()
        if not label:
            return
        try:
            field = page.locator(".ant-form-item").filter(
                has=page.get_by_text(label, exact=True)
            ).first
            trigger = field.locator(
                ".ant-select-selector, .ant-select .ant-select-selection-search-input, "
                ".el-select .el-select__wrapper"
            ).first
            if trigger.count():
                trigger.click(timeout=5000)
                self._wait_for_dropdown_visible(timeout=2000)
        except Exception:
            pass

    def _try_resolve_table_row_click(
        self, action: PlannedAction,
    ) -> tuple[Any, Optional[str], Optional[str]]:
        ex = action.extras or {}
        parsed = parse_table_row_click(action.intent or "", ex)
        if not parsed:
            return None, None, None
        button, row_hint, status_hint = parsed
        _, key_col, status_col = get_table_columns_cfg(self._session_ops_cfg)
        key_col = str(ex.get("row_key_column") or key_col).strip()
        status_col = str(ex.get("status_column") or status_col).strip()

        candidates = explicit_row_keys_from_action(
            ex, self.api_context, self._session_ops_cfg,
        )
        if not candidates:
            candidates = resolve_click_row_candidates(
                row_hint, self.api_context, self._session_ops_cfg,
                status_hint=status_hint or "",
            )
        use_first_row = any(c[0] == FIRST_TABLE_ROW_KEY for c in candidates)

        loc, detail = locate_button_in_table_row(
            self.page,
            button_label=button,
            row_keys=[c[0] for c in candidates],
            key_col=key_col,
            status_column=status_col,
            status_filter=status_hint,
        )
        if loc is None:
            return None, None, detail
        note = detail
        hints = [c[1] for c in candidates if c[1]]
        if hints:
            note = f"{detail} ({hints[0]})"
        target_html = None
        try:
            target_html = loc.evaluate("el => el.outerHTML.slice(0, 200)")
        except Exception:
            pass
        if self.trace:
            self.trace.emit(
                "locate", source="行内表格", selector=note,
                method="table_row", nth=0, target_html=target_html,
                hint=action.resolve_hint,
            )
        return loc, target_html, note

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
                    source="强制复用",
                    selector=info_key(info),
                    method=info.get("method"),
                    nth=info.get("nth", 0),
                    target_html=target_html,
                    hint=action.resolve_hint,
                )
            return loc, target_html

        if action.type == "click":
            self._ensure_select_dropdown_open(action.intent or "")
            loc, target_html, row_note = self._try_resolve_table_row_click(action)
            if loc is not None:
                action.locator_info = {"method": "table_row", "selector": row_note or ""}
                action.selector = row_note
                return loc, target_html
            # 行内表格已识别但未命中时保留 hint，fallback 到五级链
            ex = action.extras or {}
            if ex.get("row_key") or parse_table_row_click(action.intent or "", ex):
                if row_note:
                    action.resolve_hint = row_note
        # L1/L2 命中短路: 跳过 DOM 抽取 (对齐 V3)
        skip_accel = bool(getattr(action, "skip_acceleration", False))
        if not skip_accel:
            fast_info = self.resolver.try_acceleration_only(
                self.page, action.intent, action.type,
                exclude=action.exclude_selectors,
                skip_heuristics=bool(getattr(action, "skip_heuristics", False)),
            )
            if fast_info:
                return self._locate_from_info(action, fast_info)

        semantic_items, dom_source = self._prepare_semantic_items_for_locate(action)
        menu_nav = detect_feature_titles_menu_nav(
            action.intent or "",
            extras=action.extras,
            feature_titles=self.feature_titles,
        )
        info = self.resolver.resolve(
            self.page, action.intent, action.type,
            exclude=action.exclude_selectors, hint=action.resolve_hint,
            action_value=action.value or "",
            semantic_items=semantic_items,
            dom_source=dom_source,
            skip_acceleration=True,
            skip_heuristics=bool(getattr(action, "skip_heuristics", False)),
            acceleration_prefetched=not skip_accel,
            feature_titles_menu_nav=menu_nav,
            feature_titles=self.feature_titles,
        )
        if not info:
            return None, None
        return self._locate_from_info(action, info)

    def _locate_from_info(self, action: PlannedAction, info: dict):
        if info.get("_skip_navigation"):
            action.locator_info = normalize_info(info)
            action.selector = "__SKIP_NAV__"
            action.extras = {**(action.extras or {}), "skip_navigation": True}
            if self.trace:
                self.trace.emit(
                    "locate",
                    source=info.get("_source", "L5大模型"),
                    selector="__SKIP_NAV__",
                    target_html=None,
                    hint=action.resolve_hint,
                )
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
                self.page.wait_for_selector(_DROPDOWN_VISIBLE_SEL, timeout=2000)
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
            need_submit_wait = self._should_wait_post_submit_navigation(
                url_before, loc, action.intent or "",
            )
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
            self._record_dialog_trigger(action)
            record_post_click_wait(action, url_before, url_after)
            if need_submit_wait:
                ok_submit, submit_msg = self._wait_after_detail_submit(url_before)
                if not ok_submit:
                    if self.trace:
                        self.trace.emit("page_switch", url=url_after, intent=action.intent)
                    return ok_submit, submit_msg
                nav_outcome = str(
                    (self.last_dispatch_meta or {}).get("navigation_outcome") or "",
                )
                if nav_outcome == "returned_to_list":
                    self.page = wait_before_assert(
                        self.page, quiet_ms=300, timeout_ms=3000,
                        list_anchor=self._list_tab_anchor,
                    )
                elif nav_outcome in ("resource_id_changed", "route_changed"):
                    try:
                        self.page.wait_for_load_state(
                            "domcontentloaded", timeout=2000,
                        )
                    except Exception:
                        pass
                suffix = f"{suffix} | {submit_msg}"
            if self.trace:
                self.trace.emit("page_switch", url=url_after, intent=action.intent)
            return True, f"点击 {action.intent}{suffix}"
        if t == "hover":
            loc.hover(timeout=timeout)
            return True, f"悬停 {action.intent}{suffix}"
        if t == "fill":
            loc.fill(action.value or "", timeout=timeout)
            try:
                meta = loc.evaluate("""el => ({
                    placeholder: el.getAttribute('placeholder') || '',
                    name: el.getAttribute('name') || '',
                    id: el.id || '',
                    ariaLabel: el.getAttribute('aria-label') || ''
                })""")
                record_fill_history(self, meta or {}, action.value or "")
            except Exception:
                pass
            return True, f"输入 {action.value!r}{suffix}"
        if t == "press":
            loc.press(action.value or "Enter", timeout=timeout)
            return True, f"按键 {action.value}{suffix}"
        if t == "upload":
            loc.set_input_files(action.value or "", timeout=timeout)
            return True, f"上传 {action.value}{suffix}"
        return False, f"未支持的已定位动作: {t}"

    _NEW_TAB_INTENT_RE = re.compile(r"查看|新标签|新窗口|新开")

    def _should_wait_post_submit_navigation(
        self, url_before: str, loc: Any, intent: str = "",
    ) -> bool:
        """type=submit 且 URL 带资源 ID 时, 等待提交后的页面结局."""
        if not _url_query_id(url_before):
            return False
        # intent 含「提交」时直接等待; 点击后 tab 可能已关闭导致 loc.evaluate 失败
        if "提交" in (intent or ""):
            return True
        try:
            el_type = loc.evaluate(
                "el => (el.getAttribute('type') || el.type || '').toLowerCase()"
            )
            return el_type == "submit"
        except Exception:
            return False

    def _ensure_list_anchor(self) -> None:
        """记录兄弟 tab 锚点: 多 tab 时取非当前详情页的那个, 纯运行时、无业务 URL 配置."""
        anchor = find_list_tab_anchor(self.page, self._list_tab_anchor)
        if anchor is not None:
            self._list_tab_anchor = anchor

    def _prefer_submit_outcome_page(
        self,
        url_before: str,
        outcome: str,
        url_after_hint: str = "",
    ) -> tuple[Any, str]:
        """提交后同 tab URL/实体变化: 保持当前 active (不切换 anchor)."""
        if outcome not in ("resource_id_changed", "route_changed"):
            return self.page, url_after_hint or _url_safe(self.page)
        if not is_detail_submission_url(url_before):
            return self.page, url_after_hint or _url_safe(self.page)

        target = self.page if _page_alive(self.page) else find_newest_non_anchor_tab(
            _context_from_any(self.page, self._list_tab_anchor),
            self._list_tab_anchor,
            self.page,
        )
        if target is None:
            return self.page, url_after_hint or _url_safe(self.page)

        self.page = target
        bring_page_to_front(target)
        url = _url_safe(target) or url_after_hint
        if not _page_usable(target, timeout_ms=500) and _page_alive(target):
            self.page, _ = wait_and_recover_active_page(
                target, max_polls=4, prefer=target,
            )
            url = _url_safe(self.page) or url
        if self.trace:
            self.trace.emit("page_recover", url=url)
        return self.page, url

    def _finalize_detail_submit_dom(
        self,
        url_before: str,
        outcome: str,
        recovered: bool,
        left_detail: bool,
        url_after: str,
        *,
        skip_list_reload: bool = False,
    ) -> tuple[str, bool, str, bool]:
        """详情提交后: 切存活 tab 并在其上抓 DOM, 供下一步断言/定位."""
        if not is_detail_submission_url(url_before):
            if _page_usable(self.page):
                self.capture_page_state_after_operation(nav_outcome=outcome)
            return outcome, left_detail, url_after, recovered

        if outcome in ("resource_id_changed", "route_changed"):
            self.page, url_after = self._prefer_submit_outcome_page(
                url_before, outcome, url_after,
            )
            left_detail = False
            self.invalidate_page_state()
            if _page_alive(self.page):
                self.capture_page_state_after_operation(nav_outcome=outcome)
                url_after = _url_safe(self.page) or url_after
            return outcome, left_detail, url_after, recovered

        if not _page_usable(self.page):
            self._ensure_list_anchor()
            self.page, tab_recovered, url_after, tab_left = (
                pick_surviving_tab_after_detail_close(
                    self.page,
                    url_before=url_before,
                    list_anchor=self._list_tab_anchor,
                )
            )
            recovered = recovered or tab_recovered
            left_detail = left_detail or tab_left

        if not _page_usable(self.page):
            recover_cap = 5 if skip_list_reload else 30
            self.page, live_recovered = wait_and_recover_active_page(
                self.page, max_polls=recover_cap, prefer=self._list_tab_anchor,
            )
            recovered = recovered or live_recovered

        url_after = _url_safe(self.page) or url_after
        if is_detail_submission_url(url_before) and not is_detail_submission_url(url_after):
            left_detail = True
            if outcome in ("timeout", "settled", "submit_error"):
                outcome = "returned_to_list"

        if not _page_alive(self.page) and is_detail_submission_url(url_before):
            left_detail = True

        if _page_usable(self.page):
            if left_detail and not is_detail_submission_url(url_after):
                self._list_tab_anchor = self.page
                if (
                    not skip_list_reload
                    and outcome in ("timeout", "settled", "returned_to_list")
                ):
                    _reload_list_page(self.page)
            self.invalidate_page_state()
            self.capture_page_state_after_operation(nav_outcome=outcome)
            url_after = _url_safe(self.page) or url_after
        elif is_detail_submission_url(url_before):
            # tab 不可用: 尝试切到存活兄弟 tab 再抓 DOM
            self.page, tab_rec, url_after, tab_left = pick_surviving_tab_after_detail_close(
                self.page,
                url_before=url_before,
                list_anchor=self._list_tab_anchor,
            )
            recovered = recovered or tab_rec
            left_detail = left_detail or tab_left
            if _page_usable(self.page):
                self.invalidate_page_state()
                self.capture_page_state_after_operation(nav_outcome=outcome)
                url_after = _url_safe(self.page) or url_after

        return outcome, left_detail, url_after, recovered

    def _wait_after_detail_submit(self, url_before: str) -> tuple[bool, str]:
        self._ensure_list_anchor()
        self.page, outcome, recovered = wait_after_detail_submit(
            self.page,
            list_anchor=self._list_tab_anchor,
            url_before=url_before,
            budget_ms=DEFAULT_SUBMIT_WAIT_MS,
        )
        url_after = _url_safe(self.page) or ""

        if outcome in ("resource_id_changed", "route_changed"):
            self.page, url_after = self._prefer_submit_outcome_page(
                url_before, outcome, url_after,
            )
            same_tab_entity = True
            left_detail = False
        else:
            same_tab_entity = is_same_tab_detail_entity_nav(
                url_before, url_after, outcome,
            )
            if not same_tab_entity:
                self.page, live_recovered = wait_and_recover_active_page(
                    self.page,
                    prefer=self._list_tab_anchor,
                    max_polls=20,
                )
                recovered = recovered or live_recovered
                url_after = _url_safe(self.page) or url_after
            left_detail = False

        if outcome == "submit_error":
            self.page, tab_recovered, url_after = recover_after_submit_tab_close(
                self.page,
                url_before=url_before,
                list_anchor=self._list_tab_anchor,
            )
            recovered = recovered or tab_recovered
        elif outcome == "returned_to_list":
            left_detail = True
        elif outcome in ("resource_id_changed", "route_changed"):
            left_detail = not is_detail_submission_url(url_after)
        light_finalize = same_tab_entity or outcome in (
            "resource_id_changed", "route_changed",
        )
        outcome, left_detail, url_after, recovered = self._finalize_detail_submit_dom(
            url_before, outcome, recovered, left_detail, url_after,
            skip_list_reload=light_finalize,
        )
        if not url_after:
            url_after = _url_safe(self.page)
        flat_after = ""
        if self._page_state and self._page_state.get("semantic_items"):
            from .assert_scope import items_flat_text
            flat_after = items_flat_text(self._page_state["semantic_items"])
        elif _page_usable(self.page):
            try:
                flat_after = (self.page.inner_text("body") or "")[:6000]
            except Exception:
                flat_after = ""
        id_before, field_before = discover_active_entity(
            self.api_context, url=url_before, flat_text="",
        )
        id_after, field_after = discover_page_entity(
            self.api_context, url=url_after, flat_text=flat_after,
        )
        entity_field = field_before or field_after or "entity"
        self.last_dispatch_meta = {
            "navigation_outcome": outcome,
            "url_before": url_before,
            "url_after": url_after,
            "recovered": recovered,
            "left_detail_context": left_detail,
            "submit_click_ok": True,
            "entity_id_before": id_before,
            "entity_id_after": id_after,
            "entity_field": entity_field,
        }
        if not _page_alive(self.page) and is_detail_submission_url(url_before):
            self.last_dispatch_meta["detail_tab_closed"] = True
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
                entity_id_before=id_before,
                entity_id_after=id_after,
            )
        if submit_dispatch_should_succeed(self.last_dispatch_meta):
            return True, f"navigation_outcome={outcome}"
        if outcome == "submit_error":
            return False, f"navigation_outcome={outcome}"
        return False, f"navigation_outcome={outcome}"

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
            self._session.on_detail_opened(list_page, new_page)
            self.page = new_page
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
                        self._session.on_detail_opened(list_page, p)
                        self.page = p
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
        intent = (action.intent or "").strip()
        if "下拉" in intent and "展开" in intent:
            if self._wait_for_dropdown_visible():
                return True, "等待下拉面板展开"
            return False, "等待下拉面板超时"
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
        ok_st, _, _, err = self._get_page_state_for_assert()
        if not ok_st:
            prefix = "计数断言"
            if err.startswith("断言失败:"):
                return False, prefix + err[len("断言失败"):]
            return False, f"{prefix}: {err}"
        if not self._page_state or not PageSession.url_keys_equivalent(
            str(self._page_state.get("key") or ""), self._page_state_key(),
        ):
            return (
                False,
                "计数断言: 缺少操作后的页面状态, 请先执行会改变页面的操作步骤",
            )
        if self.trace:
            self.trace.emit("assert_use_state", url=self._page_state_key())
        op, threshold = _parse_count_spec(action)
        actual, source = self._measure_list_count()
        ok = _compare_count(actual, threshold, op)
        sym = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "="}.get(op, op)
        status = "通过" if ok else "未通过"
        if ok:
            record_assert_count(action, op, threshold, source)
        return ok, f"计数断言({source}): 实际{actual} {sym} {threshold} → {status}"

    def _measure_list_count(self) -> tuple[int, str]:
        """优先读「当前总数为:N」, 否则统计表格数据行."""
        key = self._page_state_key()
        if self._page_state and self._page_state.get("key") == key:
            flat = items_flat_text(self._page_state.get("semantic_items") or [])
            m = re.search(r"当前总数为[:：]\s*(\d+)", flat)
            if m:
                return int(m.group(1)), "当前总数"
        self._ensure_live_page()
        try:
            body = self.page.inner_text("body")
            m = re.search(r"当前总数为[:：]\s*(\d+)", body)
            if m:
                return int(m.group(1)), "当前总数"
        except Exception:
            pass
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

    # ---------- 文本断言 (字面量/scoped → semantic_assert, 不走五级定位链) ----------
    def _assert_text(self, action: PlannedAction) -> tuple[bool, str]:
        target = (action.value or action.intent or "").strip()
        if not target and not is_or_assert(action):
            return False, "断言缺少目标文本"
        intent = action.intent or ""
        scope = parse_assert_scope(intent, value=target, negate=action.negate)
        ok_st, items, dom_summary, err = self._get_page_state_for_assert()
        if not ok_st:
            return False, err
        flat_text = items_flat_text(items)
        assert_url = self._cached_assert_url()

        submit_ctx = dict(self.last_dispatch_meta or {})
        live_facts = build_live_submit_facts(
            page=self.page,
            items=items,
            dispatch_meta=submit_ctx,
            api_context=self.api_context,
            list_anchor=self._list_tab_anchor,
        )
        if live_facts is not None:
            submit_ctx = {
                **submit_ctx,
                "url_after": live_facts.url_after,
                "entity_id_after": live_facts.entity_id_after,
                "navigation_outcome": live_facts.navigation_outcome,
            }

        btn_state = self._try_assert_button_state(action, target)
        if btn_state is not None:
            return btn_state

        if scope.field_hint and not action.negate:
            field_hit = try_field_value_assert_items(
                scope, items, scope.field_hint, target,
            )
            if field_hit is not None:
                if field_hit[0]:
                    self._record_literal(action, target)
                return field_hit

        if not action.negate:
            scoped_hit = try_scoped_literal_items(scope, items, target)
            if scoped_hit is not None:
                if scoped_hit[0]:
                    self._record_literal(action, target)
                    return scoped_hit
                if not scope.explicit_region:
                    return scoped_hit
                # 区域字面未命中但全文有目标 (DOM 节点拆分/无 in_form 标记) → 仍走程序匹配, 不调 LLM
                if _text_contains(target, flat_text):
                    self._record_literal(action, target)
                    return True, f"断言: 页面含 {target!r}"

        skip_flat_literal = (
            scope.explicit_region or scope.field_hint or scope.exclude_nav
        )
        if action.negate:
            present = target in flat_text
            if not present:
                record_literal(action, target, negate=True, api_context=self.api_context)
            return (not present), (f"否定断言: 页面{'仍包含' if present else '不包含'} {target!r}")
        if not skip_flat_literal:
            present = target in flat_text
            if present:
                self._record_literal(action, target)
                return True, f"断言: 页面含 {target!r}"
        row_hit = self._try_assert_list_rows(action, target, scope)
        if row_hit is not None:
            if row_hit[0]:
                self._record_literal(action, target)
            return row_hit
        control = self._try_assert_control_mode(action)
        if control is not None:
            if control[0]:
                stats = self._read_control_stats()
                if stats is not None:
                    want_single = bool(re.search(r"单选|不能多选|互斥", action.intent or ""))
                    record_control_mode(action, stats, want_single=want_single)
            return control
        if is_or_assert(action) and not action.negate:
            branches = (action.extras or {}).get("branches") or []
            if branches:
                hit = try_or_branches(
                    self.page, branches, flat_text,
                    dispatch_meta=submit_ctx,
                    page_url=assert_url,
                    live_facts=live_facts,
                )
                if hit is not None:
                    if hit[0]:
                        record_or_branch(
                            action, self.page, branches, flat_text,
                            api_context=self.api_context,
                        )
                    return hit
            else:
                hit = try_or_heuristic(
                    self.page, combined_or_intent(action),
                    dispatch_meta=submit_ctx,
                    page_url=assert_url,
                    body_text=flat_text,
                )
                if hit is not None:
                    if hit[0]:
                        record_or_heuristic(action, self.page, combined_or_intent(action))
                    return hit
            ok, msg = self._semantic_assert(
                action, items, scope=scope, any_of=True, dom_summary=dom_summary,
            )
            if ok:
                record_semantic_pass(action, self.page, flat_text)
            return ok, msg
        if not self._should_semantic_fallback(scope):
            return False, f"断言未通过: {action.intent!r} (目标文本 {target!r})"
        ok, msg = self._semantic_assert(
            action, items, scope=scope, dom_summary=dom_summary,
        )
        if ok:
            record_semantic_pass(action, self.page, flat_text)
        return ok, msg

    def _try_assert_button_state(
        self, action: PlannedAction, target: str,
    ) -> Optional[tuple[bool, str]]:
        """按钮置灰/可点: 校验 disabled 状态, 不能仅用页面含文案糊弄."""
        intent = action.intent or ""
        extras = action.extras or {}
        state = str(extras.get("state") or "").strip().lower()
        want_disabled = state in ("disabled", "置灰", "不可点", "不可用") or bool(
            re.search(r"置灰|不可点|不可用|disabled", intent, re.I)
        )
        want_enabled = state in ("enabled", "可点", "高亮", "可用") or bool(
            re.search(r"高亮|可点击|enabled", intent, re.I)
        )
        if not want_disabled and not want_enabled:
            return None
        if want_disabled and want_enabled:
            want_enabled = False

        label = (target or extras.get("button") or "").strip()
        if not label:
            m = re.search(r"[「'\"']([^」'\"]+)[」'\"']", intent)
            label = (m.group(1) if m else "").strip()
        if not label:
            return False, "控件断言: 缺少按钮文案 (value 或 intent 引号内)"

        for variant in _button_label_variants(label):
            for role in ("button", "link"):
                loc = self.page.get_by_role(role, name=variant)
                try:
                    cnt = loc.count()
                except Exception:
                    continue
                if cnt == 0:
                    continue
                btn = loc.first if cnt == 1 else loc.last
                try:
                    is_dis = btn.is_disabled()
                except Exception:
                    try:
                        is_dis = btn.evaluate(
                            """el => !!(el.disabled || el.getAttribute('aria-disabled') === 'true'
                                || el.classList.contains('ant-btn-disabled')
                                || el.closest('.ant-btn-disabled, [disabled]'))"""
                        )
                    except Exception:
                        continue
                if want_disabled:
                    ok = bool(is_dis)
                    msg = (
                        f"控件断言(置灰): {variant!r} disabled={is_dis} → "
                        f"{'通过' if ok else '未通过(仍可点)'}"
                    )
                else:
                    ok = not bool(is_dis)
                    msg = (
                        f"控件断言(可点): {variant!r} disabled={is_dis} → "
                        f"{'通过' if ok else '未通过(仍置灰)'}"
                    )
                record_button_state(action, variant, disabled=bool(is_dis))
                return ok, msg
        return False, f"控件断言: 未找到按钮 {label!r}"

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
        items: list[dict],
        *,
        scope=None,
        dom_summary: Optional[str] = None,
        any_of: bool = False,
    ) -> tuple[bool, str]:
        """精确/locator 匹配失败时, 用共用 indexed DOM + LLM 判断."""
        or_mode = any_of or is_or_assert(action)
        if scope is None:
            target = (action.value or action.intent or "").strip()
            scope = parse_assert_scope(action.intent or "", value=target, negate=action.negate)

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
            return False, "语义断言: 缺少操作后的 DOM 摘要, 请先执行 UI 操作步骤"
        text_summary = build_semantic_text_summary_from_items(items, scope)
        or_note = "是 (满足任一分支即可)" if or_mode else "否"
        intent_text = combined_or_intent(action) if or_mode else (action.intent or "")
        scope_note = format_scope_note_for_semantic(scope)
        submit_facts = ""
        live_facts = build_live_submit_facts(
            page=self.page,
            items=items,
            dispatch_meta=self.last_dispatch_meta,
            api_context=self.api_context,
            list_anchor=self._list_tab_anchor,
        )
        if live_facts is not None:
            submit_facts = live_facts.format_facts()

        if self.console:
            self.console.print(f"  [dim]① 复用 indexed DOM 摘要[/dim]")
            self.console.print(f"  [dim]② 拼装 prompt 调用 LLM 语义分析[/dim]")
            self.console.print(f"  [dim]   断言意图: {intent_text}[/dim]")

        default_user = f"""断言意图: {intent_text}
断言目标(value): {action.value or "-"}
或断言: {or_note}
当前页面 URL: {self.page.url}

{scope_note}
{submit_facts}

页面 DOM 摘要 (每行 [索引] 与 semantic_items 下标一致):
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
            target = (action.value or "").strip()
            hint = target or action.intent or ""
            return False, f"语义断言 LLM 调用失败, 精确匹配也未找到 {hint!r}"

    @property
    def _llm(self):
        """获取 LLM 实例, 由外部注入."""
        return getattr(self, "_llm_instance", None)

    @_llm.setter
    def _llm(self, value):
        self._llm_instance = value

    def _assert_table(self, action: PlannedAction) -> tuple[bool, str]:
        """断言表格中某行某列的值 (行由 value / extras.row_key 标识)."""
        if not self._ensure_assert_tab(quick=True):
            return False, "表格断言: 当前浏览器 tab 不可用或已关闭"
        if not self._page_state or self._page_state.get("key") != self._page_state_key():
            return (
                False,
                "表格断言: 缺少操作后的页面状态, 请先执行会改变页面的操作步骤",
            )
        if self.trace:
            self.trace.emit("assert_use_state", url=self._page_state_key())
        extras = action.extras or {}
        row_key = (action.value or extras.get("row_key") or "").strip()
        key_col = str(extras.get("row_key_column") or "工单ID").strip()
        target_col = str(extras.get("column") or "").strip()
        expected = str(extras.get("expected") or extras.get("cell_value") or "").strip()
        if not row_key:
            return False, "assert_table 缺少行标识 (value 或 extras.row_key)"
        if not target_col or not expected:
            return False, "assert_table 缺少 extras.column 或 extras.expected"

        resolved_key, ops_hint = resolve_table_row_key(
            row_key, self.api_context, self._session_ops_cfg,
        )
        if ops_hint and resolved_key != row_key:
            row_key = resolved_key

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
            exact_row: Optional[tuple[list[str], str]] = None
            token_row: Optional[tuple[list[str], str]] = None
            for ri in range(body_rows.count()):
                cells = [c.strip() for c in body_rows.nth(ri).locator("td").all_inner_texts()]
                if key_idx >= len(cells):
                    continue
                cell_val = cells[key_idx]
                if not table_row_key_matches(cell_val, row_key):
                    continue
                if cell_val.strip() == row_key.strip():
                    exact_row = (cells, cell_val)
                    break
                if token_row is None:
                    token_row = (cells, cell_val)
            hit = exact_row or token_row
            if hit:
                cells, _ = hit
                actual = cells[col_idx] if col_idx < len(cells) else ""
                if expected in actual or actual == expected:
                    msg = (
                        f"表格断言: 行标识 {row_key!r} 列 {target_col!r} "
                        f"期望 {expected!r} 实际 {actual!r}"
                    )
                    if ops_hint:
                        msg = f"{msg} ({ops_hint})"
                    return True, msg
                return False, (
                    f"表格断言: 行标识 {row_key!r} 列 {target_col!r} "
                    f"期望 {expected!r} 实际 {actual!r}"
                )
        hint = _ops_resolve_hint(
            (action.value or "").strip(), self.api_context, self._session_ops_cfg,
        )
        return False, (
            f"表格断言: 未找到行标识 {row_key!r} (列 {key_col!r}) "
            f"或列 {target_col!r}; {hint}"
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
            remember_option_click(self, action, label)

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

    def _record_literal(
        self,
        action: PlannedAction,
        target: str,
        *,
        negate: bool = False,
    ) -> None:
        record_literal(
            action, target, negate=negate, api_context=self.api_context,
        )


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

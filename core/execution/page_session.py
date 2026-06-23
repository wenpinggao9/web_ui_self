"""浏览器 Tab / DOM 状态 —— 提交关详情 tab 后 handoff 与断言上下文的唯一入口.

不变量: 详情提交步结束或断言前, active 必须指向存活 tab, page_state 对应该 tab URL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .tab_follow import follow_active_tab, wait_and_recover_active_page
from ..locating.normalize import normalize_url
from .entity_discover import canonical_url_entity_map, url_entity_maps_differ
from .script_helpers import (
    _page_alive,
    _page_usable,
    _reload_list_page,
    _url_safe,
    bring_page_to_front,
    find_list_tab_anchor,
    is_detail_submission_url,
    pick_surviving_tab_after_detail_close,
    wait_and_recover_active_page,
    wait_before_assert,
)


@dataclass
class PageSession:
    """管理 active tab、list_anchor 与操作后 DOM 缓存."""

    active: Any = None
    list_anchor: Any = None
    page_state: Optional[dict[str, Any]] = field(default=None)

    def invalidate_dom(self) -> None:
        self.page_state = None

    @staticmethod
    def url_keys_equivalent(cached: str, current: str) -> bool:
        """比较 DOM 缓存 key 与当前 URL (大小写/实体参数归一)."""
        if not cached:
            return False
        if not current:
            return True
        c, u = cached.lower(), current.lower()
        if c == u:
            return True
        ent_c = canonical_url_entity_map(cached)
        ent_u = canonical_url_entity_map(current)
        if ent_c and ent_u:
            return ent_c == ent_u
        if url_entity_maps_differ(cached, current):
            return False
        return normalize_url(cached) == normalize_url(current) and bool(
            normalize_url(cached)
        )

    def page_key(self, page: Any = None) -> str:
        p = page if page is not None else self.active
        try:
            return (p.url or "").lower()
        except Exception:
            return ""

    def has_dom_cache(self) -> bool:
        return bool(
            self.page_state and (self.page_state.get("semantic_items") or [])
        )

    def cache_matches_active(self) -> bool:
        if not self.has_dom_cache():
            return False
        cached = str(self.page_state.get("key") or "")
        cur = self.page_key()
        return self.url_keys_equivalent(cached, cur)

    def on_detail_opened(self, list_page: Any, detail_page: Any) -> None:
        """popup 打开新 tab: anchor=打开前 page, active=新 tab (通用, 与业务 URL 无关)."""
        self.list_anchor = list_page
        self.active = detail_page
        self.page_state = None

    on_tab_opened = on_detail_opened

    def remember_list_if_non_detail(self) -> None:
        """仅一个存活 tab 时将其记为 anchor."""
        ctx = _context_from_any(self.active)
        if ctx is not None:
            try:
                alive = [p for p in ctx.pages if _page_alive(p)]
                if len(alive) == 1:
                    self.list_anchor = alive[0]
                    return
            except Exception:
                pass
        if self.active is not None and _page_alive(self.active):
            self.list_anchor = self.active

    def resolve_list_anchor(self) -> None:
        anchor = find_list_tab_anchor(self.active, self.list_anchor)
        if anchor is not None:
            self.list_anchor = anchor

    def ensure_alive(self, *, max_polls: int = 15) -> tuple[Any, bool]:
        self.resolve_list_anchor()
        self.active, switched, _ = follow_active_tab(self.active, self.list_anchor)
        if _page_usable(self.active, timeout_ms=400):
            return self.active, switched
        self.active, recovered = wait_and_recover_active_page(
            self.active, max_polls=max_polls, prefer=self.list_anchor,
        )
        return self.active, recovered or switched

    @staticmethod
    def needs_list_tab_handoff(meta: Optional[dict[str, Any]]) -> bool:
        """详情 tab 关闭或回到列表时才 recover 到 list tab."""
        meta = meta or {}
        if meta.get("detail_tab_closed") or meta.get("left_detail_context"):
            return True
        outcome = str(meta.get("navigation_outcome") or "")
        if outcome == "returned_to_list":
            return True
        url_before = str(meta.get("url_before") or "")
        if not (meta.get("submit_click_ok") and is_detail_submission_url(url_before)):
            return False
        # 同 tab 自动加载下一任务: 一直在 tab2, 不切 list
        if outcome in ("resource_id_changed", "route_changed"):
            return False
        if outcome in ("timeout", "settled", "submit_error"):
            return bool(
                meta.get("detail_tab_closed") or meta.get("left_detail_context"),
            )
        return False

    @staticmethod
    def is_same_tab_submit_nav(meta: Optional[dict[str, Any]]) -> bool:
        """同 tab 内提交后实体/路由切换 (如自动加载下一任务详情)."""
        meta = meta or {}
        outcome = str(meta.get("navigation_outcome") or "")
        if outcome not in ("resource_id_changed", "route_changed"):
            return False
        url_after = str(meta.get("url_after") or "")
        if url_after and is_detail_submission_url(url_after):
            return True
        if is_detail_submission_url(str(meta.get("url_before") or "")):
            return True
        return False

    @staticmethod
    def is_detail_submit_handoff(meta: Optional[dict[str, Any]]) -> bool:
        """兼容旧名: 仅 list tab recover 场景."""
        return PageSession.needs_list_tab_handoff(meta)

    def recover_after_detail_close(
        self,
        url_before: str,
        meta: Optional[dict[str, Any]] = None,
    ) -> tuple[Any, str, bool]:
        page, recovered, url, left = pick_surviving_tab_after_detail_close(
            self.active,
            url_before=url_before,
            list_anchor=self.list_anchor,
        )
        self.active = page
        if url and not is_detail_submission_url(url):
            self.list_anchor = page
        elif left and self.list_anchor is not None and _page_usable(self.list_anchor):
            self.active = self.list_anchor
            try:
                self.active.bring_to_front()
            except Exception:
                pass
            url = _url_safe(self.active)
        if meta is not None:
            if url:
                meta["url_after"] = url
            if recovered or left:
                meta["recovered"] = True
        bring_page_to_front(self.active)
        return self.active, url, left

    def finish_detail_submit_handoff(
        self,
        meta: Optional[dict[str, Any]],
        *,
        recovered_page: Any = None,
        recapture: bool = True,
        capture_fn: Optional[Callable[..., None]] = None,
        should_reload_fn: Optional[Callable[..., bool]] = None,
    ) -> dict[str, Any]:
        """详情提交步结束: recover → 切 list tab → 重抓 DOM."""
        merged = dict(meta or {})
        if recovered_page is not None:
            self.active = recovered_page
            bring_page_to_front(recovered_page)
            if self.list_anchor is None or not _page_usable(self.list_anchor):
                if not is_detail_submission_url(_url_safe(recovered_page)):
                    self.list_anchor = recovered_page

        url_before = str(merged.get("url_before") or "")

        # 调试: handoff 前 active tab 状态
        _dbg_url_before_handoff = ""
        _dbg_alive_before_handoff = False
        try:
            _dbg_url_before_handoff = _url_safe(self.active)
            _dbg_alive_before_handoff = _page_alive(self.active)
        except Exception:
            pass

        if PageSession.needs_list_tab_handoff(merged):
            if is_detail_submission_url(url_before):
                self.recover_after_detail_close(url_before, merged)
            self.ensure_alive(max_polls=12)
        else:
            self.ensure_alive(max_polls=3)

        # 调试: handoff 后 active tab 状态
        _dbg_url_after_handoff = ""
        _dbg_alive_after_handoff = False
        try:
            _dbg_url_after_handoff = _url_safe(self.active)
            _dbg_alive_after_handoff = _page_alive(self.active)
        except Exception:
            pass
        print(f"  [yellow]Handoff调试: before={_dbg_url_before_handoff[:80]} alive={_dbg_alive_before_handoff} → after={_dbg_url_after_handoff[:80]} alive={_dbg_alive_after_handoff}[/yellow]")

        if not recapture or capture_fn is None:
            return merged

        if not PageSession.needs_list_tab_handoff(merged):
            if self.cache_matches_active():
                return merged
            self.invalidate_dom()
            if _page_usable(self.active):
                nav = str(merged.get("navigation_outcome") or "")
                capture_fn(nav_outcome=nav)
            return merged

        self.invalidate_dom()
        if _page_usable(self.active):
            reload_fn = should_reload_fn or (lambda _m, _p: False)
            if reload_fn(merged, self.active):
                _reload_list_page(self.active)
            self.active = wait_before_assert(
                self.active,
                quiet_ms=300,
                timeout_ms=6000,
                list_anchor=self.list_anchor,
            )
            nav = str(merged.get("navigation_outcome") or "returned_to_list")
            capture_fn(nav_outcome=nav)
        elif _page_alive(self.active):
            # evaluate 超时但 tab 仍存活 → 也尝试抓 DOM
            try:
                nav = str(merged.get("navigation_outcome") or "returned_to_list")
                capture_fn(nav_outcome=nav)
            except Exception:
                pass
        return merged

    def context_for_assert(
        self,
        *,
        capture_fn: Callable[..., None],
        ensure_tab_fn: Callable[..., bool],
        trace: Any = None,
        force_live: bool = False,
    ) -> tuple[bool, list[dict], str, str]:
        """断言前保证有 DOM; force_live 时始终 tab 跟随并重抓, 不复用缓存."""
        if force_live:
            backup: Optional[dict[str, Any]] = None
            if self.has_dom_cache() and self.cache_matches_active():
                backup = dict(self.page_state or {})
            self.invalidate_dom()
            self.ensure_alive(max_polls=15)
            tab_ok = ensure_tab_fn(quick=False)
            if _page_usable(self.active, timeout_ms=800):
                capture_fn()
            elif not tab_ok:
                if backup:
                    self.page_state = backup
                    st = self.page_state or {}
                    if trace:
                        trace.emit(
                            "assert_use_state",
                            url=str(st.get("key") or ""),
                            shared=True,
                            cached_only=True,
                            fast=True,
                            live=False,
                            fallback=True,
                        )
                    return True, list(st.get("semantic_items") or []), st.get("dom_summary") or "", ""
                return False, [], "", "断言失败: 当前浏览器 tab 不可用或已关闭"
            if not self.page_state and _page_usable(self.active, timeout_ms=2000):
                capture_fn()
            if not self.page_state:
                if backup:
                    self.page_state = backup
                    st = self.page_state or {}
                    if trace:
                        trace.emit(
                            "assert_use_state",
                            url=str(st.get("key") or ""),
                            shared=True,
                            cached_only=True,
                            fast=True,
                            live=False,
                            fallback=True,
                        )
                    return True, list(st.get("semantic_items") or []), st.get("dom_summary") or "", ""
                return (
                    False, [], "",
                    "断言失败: 无法抓取当前 tab 的实时 DOM",
                )
            key = str(self.page_state.get("key") or "")
            if trace:
                trace.emit(
                    "assert_use_state",
                    url=key,
                    shared=False,
                    cached_only=False,
                    fast=False,
                    live=True,
                )
            st = self.page_state or {}
            return True, list(st.get("semantic_items") or []), st.get("dom_summary") or "", ""

        if (
            not force_live
            and self.has_dom_cache()
            and self.cache_matches_active()
        ):
            st = self.page_state or {}
            items = list(st.get("semantic_items") or [])
            key = str(st.get("key") or "")
            print(f"  [green]Assert缓存命中: url={key[:80]} items={len(items)}[/green]")
            if trace:
                trace.emit(
                    "assert_use_state",
                    url=key,
                    shared=True,
                    cached_only=True,
                    fast=True,
                )
            if _page_usable(self.active, timeout_ms=300):
                bring_page_to_front(self.active)
            return True, items, st.get("dom_summary") or "", ""

        _dbg_has_cache = self.has_dom_cache()
        _dbg_cache_matches = self.cache_matches_active()
        _dbg_force_live = force_live
        _dbg_cache_key = ""
        try:
            _dbg_cache_key = str((self.page_state or {}).get("key") or "")[:80]
        except Exception:
            pass
        _dbg_active_url = ""
        try:
            _dbg_active_url = self.page_key()[:80]
        except Exception:
            pass
        print(f"  [yellow]Assert非缓存路径: force_live={_dbg_force_live} has_cache={_dbg_has_cache} matches={_dbg_cache_matches} cache_key={_dbg_cache_key} active_url={_dbg_active_url}[/yellow]")

        self.ensure_alive(max_polls=30)
        tab_ok = ensure_tab_fn(quick=True)

        if not tab_ok and not self.has_dom_cache():
            self.ensure_alive(max_polls=30)
            if _page_usable(self.active):
                self.active = wait_before_assert(
                    self.active, list_anchor=self.list_anchor,
                )
                capture_fn()
                tab_ok = True
            else:
                return False, [], "", "断言失败: 当前浏览器 tab 不可用或已关闭"

        if tab_ok or _page_usable(self.active, timeout_ms=800):
            key = self.page_key()
            stale = (
                not self.page_state
                or not self.url_keys_equivalent(
                    str(self.page_state.get("key") or ""), key,
                )
            )
            if stale:
                if _page_usable(self.active, timeout_ms=800):
                    capture_fn()
                elif _page_alive(self.active):
                    # evaluate 超时但 tab 仍存活 → 用 wait_for_dom_stable 方式抓 DOM
                    try:
                        capture_fn()
                    except Exception:
                        pass
                key = self.page_key()

        if not self.page_state:
            if _page_usable(self.active, timeout_ms=2000):
                capture_fn()
            elif _page_alive(self.active):
                # evaluate 超时但 tab 仍存活 → 尝试抓 DOM
                try:
                    capture_fn()
                except Exception:
                    pass

        if not self.page_state:
            return (
                False, [], "",
                "断言失败: 缺少操作后的页面状态, 请先执行会改变页面的操作步骤",
            )

        key = str(self.page_state.get("key") or "")
        if tab_ok and not self.url_keys_equivalent(
            str(self.page_state.get("key") or ""), self.page_key(),
        ):
            return (
                False, [], "",
                "断言失败: 页面 URL 已变化, 请先执行操作步骤刷新页面状态",
            )
        if trace:
            trace.emit(
                "assert_use_state",
                url=key,
                shared=True,
                cached_only=not tab_ok,
            )
        st = self.page_state or {}
        return True, list(st.get("semantic_items") or []), st.get("dom_summary") or "", ""

"""供 run.py 与 codegen 脚本共用的页面辅助 (避免逻辑重复)."""
from __future__ import annotations

import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from ..locating.normalize import normalize_url, _NUM, _UUID
from .entity_discover import extract_url_entity_map, pick_primary_url_id, url_entity_maps_differ
from .nav_progress import (
    capture_submit_entity_before,
    detect_submit_navigation_progress,
    try_wait_url_entity_change,
)

_EMPTY_ROW_MARKERS = ("暂无数据", "无数据", "No data", "no data")
_SUBMIT_ERROR_MARKERS = ("任务已处理", "请勿重复提交", "不可重复提交")
FIRST_TABLE_ROW_KEY = "__first_row__"


def bring_page_to_front(page: Any) -> None:
    """将指定 page 所在浏览器窗口置于最前 (多角色/多 context 切换时用)."""
    try:
        if page is not None and _page_usable(page):
            page.bring_to_front()
    except Exception:
        pass


def _log_tab_handoff(message: str) -> None:
    print(f"  [cyan]TabHandoff: {message}[/cyan]")


def tab_handoff_snapshot(context: Any, *pages: Any) -> str:
    """所有相关 tab 的可读快照 (alive / usable / url)."""
    ctx = context or _context_from_any(*pages)
    seen: set[int] = set()
    parts: list[str] = []
    candidates: list[Any] = []
    for p in pages:
        if p is not None and id(p) not in seen:
            candidates.append(p)
            seen.add(id(p))
    if ctx is not None:
        try:
            for p in ctx.pages:
                if id(p) not in seen:
                    candidates.append(p)
                    seen.add(id(p))
        except Exception:
            pass
    for i, p in enumerate(candidates):
        url = _url_safe(p)[:70] if p is not None else ""
        parts.append(
            f"#{i+1} alive={_page_alive(p)} usable={_page_usable(p, timeout_ms=400)} "
            f"detail={is_detail_submission_url(url)} url={url or '<empty>'}"
        )
    return " | ".join(parts) if parts else "(no tabs)"


def find_newest_non_anchor_tab(
    context: Any,
    anchor: Any = None,
    *hints: Any,
) -> Any:
    """Context 中最新存活且非 anchor 的 tab (popup 子 tab, 纯 page 引用)."""
    ctx = context or _context_from_any(anchor, *hints)
    if ctx is None:
        return None
    try:
        alive = [p for p in ctx.pages if _page_alive(p)]
    except Exception:
        return None
    if not alive:
        return None
    if anchor is not None:
        others = [p for p in alive if p is not anchor]
        if others:
            return others[-1]
    if len(alive) >= 2:
        return alive[-1]
    return None


def find_preferred_detail_tab(context: Any, *hints: Any) -> Any:
    """兼容旧名: 优先走 anchor 模型 (非 anchor 的最新 tab)."""
    anchor = None
    for h in hints:
        if h is not None:
            anchor = h
            break
    return find_newest_non_anchor_tab(context, anchor, *hints)


def find_alive_detail_tab(context: Any, *hints: Any) -> Any:
    """兼容旧名 → find_newest_non_anchor_tab."""
    anchor = None
    for h in hints:
        if h is not None and not is_detail_submission_url(_url_safe(h)):
            anchor = h
            break
    return find_newest_non_anchor_tab(context, anchor, *hints)


def sticky_resolve_tab(
    active: Any,
    list_anchor: Any = None,
) -> tuple[Any, bool, str]:
    """纯 anchor 模型: active 未关闭则一直用 active; 关闭则回 list_anchor.

    不依赖 URL / 业务路径; 仅 _page_alive + 打开时记录的 anchor.
    """
    switched = False

    def _front(p: Any) -> None:
        try:
            p.bring_to_front()
        except Exception:
            pass

    if active is not None and _page_alive(active):
        _front(active)
        return active, False, "stick_active"

    if list_anchor is not None and _page_alive(list_anchor):
        switched = active is not list_anchor
        _front(list_anchor)
        return list_anchor, switched, "stick_anchor"

    ctx = _context_from_any(active, list_anchor)
    if ctx is not None:
        try:
            alive = [p for p in ctx.pages if _page_alive(p)]
        except Exception:
            alive = []
        if alive:
            pick = alive[-1]
            switched = pick is not active
            _front(pick)
            return pick, switched, "stick_newest"

    return active, False, "stick_none"


def pick_role_handoff_page(
    context: Any,
    current_page: Any = None,
    *,
    list_anchor: Any = None,
    primary_page: Any = None,
    prefer_current: bool = False,
    prefer_detail: bool = False,
    reason: str = "",
) -> Any:
    """跨用例/跨角色复用时选稳定 tab, 仅依赖运行时 tab 关系, 不假设 URL 形态.

    优先级 (默认): list_anchor > primary_page > 多 tab 时非 current > 首个可用 tab.
    prefer_current=True: 当前 tab 仍可用/仍存活时优先保留.
    prefer_detail=True: 在 prefer_current 路径下优先详情 tab (跨用例连续审详情).
    """
    tag = f"[{reason}] " if reason else ""
    _log_tab_handoff(
        f"{tag}start prefer_current={prefer_current} prefer_detail={prefer_detail} "
        f"current={(_url_safe(current_page) or '<none>')[:70]}"
    )
    _log_tab_handoff(
        f"{tag}inventory {tab_handoff_snapshot(context, current_page, list_anchor, primary_page)}"
    )

    if prefer_current and current_page is not None:
        if _page_usable(current_page):
            _log_tab_handoff(f"{tag}→ keep current (usable)")
            return current_page
        if _page_alive(current_page):
            if _page_usable(current_page, timeout_ms=4000):
                _log_tab_handoff(f"{tag}→ keep current (slow evaluate ok)")
                return current_page
            cur_url = _url_safe(current_page)
            if cur_url:
                _log_tab_handoff(
                    f"{tag}current alive but evaluate slow, url={cur_url[:70]}"
                )

    if prefer_current and prefer_detail:
        if current_page is not None and _page_alive(current_page):
            if list_anchor is None or current_page is not list_anchor:
                _log_tab_handoff(
                    f"{tag}→ keep current child tab {_url_safe(current_page)[:70]}"
                )
                try:
                    current_page.bring_to_front()
                except Exception:
                    pass
                return current_page
        child = find_newest_non_anchor_tab(
            context, list_anchor, current_page, primary_page,
        )
        if child is not None:
            _log_tab_handoff(f"{tag}→ prefer_child_tab {_url_safe(child)[:70]}")
            try:
                child.bring_to_front()
            except Exception:
                pass
            return child

    if prefer_current and current_page is not None and _page_alive(current_page):
        cur_url = _url_safe(current_page)
        if cur_url:
            _log_tab_handoff(f"{tag}→ keep current (alive+url, weak)")
            try:
                current_page.bring_to_front()
            except Exception:
                pass
            return current_page

    for candidate in (list_anchor, primary_page):
        if candidate is not None and _page_usable(candidate):
            _log_tab_handoff(
                f"{tag}→ fallback candidate {_url_safe(candidate)[:70]}"
            )
            return candidate

    usable: list[Any] = []
    try:
        if context is not None:
            usable = [p for p in context.pages if _page_usable(p)]
    except Exception:
        usable = []

    if usable:
        if current_page is not None and len(usable) > 1 and not prefer_current:
            others = [p for p in usable if p is not current_page]
            if others:
                _log_tab_handoff(
                    f"{tag}→ other usable tab (non-preserve) {_url_safe(others[0])[:70]}"
                )
                return others[0]
        _log_tab_handoff(f"{tag}→ first usable tab {_url_safe(usable[0])[:70]}")
        return usable[0]

    if current_page is not None:
        recovered, _ = recover_active_page(
            current_page, prefer=list_anchor or primary_page,
        )
        if _page_usable(recovered):
            _log_tab_handoff(f"{tag}→ recovered {_url_safe(recovered)[:70]}")
            return recovered
    _log_tab_handoff(f"{tag}→ return current unchanged")
    return current_page


def count_real_table_rows(page: Any) -> int:
    for sel in (".ant-table-tbody tr", "table tbody tr"):
        try:
            rows = page.locator(sel)
            count = 0
            for i in range(rows.count()):
                try:
                    text = rows.nth(i).inner_text(timeout=2000)
                except Exception:
                    text = ""
                if text.strip() and not any(m in text for m in _EMPTY_ROW_MARKERS):
                    count += 1
            if count > 0:
                return count
        except Exception:
            continue
    try:
        body = page.inner_text("body")
        m = re.search(r"当前总数为[:：]\s*(\d+)", body)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0


def measure_list_count(page: Any) -> tuple[int, str]:
    """与 dispatcher._measure_list_count 一致: 优先「当前总数为」, 否则数表格行."""
    page, _ = wait_and_recover_active_page(page)
    try:
        body = page.inner_text("body")
    except Exception:
        page, _ = wait_and_recover_active_page(page)
        try:
            body = page.inner_text("body")
        except Exception:
            body = page.content()
    m = re.search(r"当前总数为[:：]\s*(\d+)", body)
    if m:
        return int(m.group(1)), "当前总数"
    n = count_real_table_rows(page)
    if n > 0:
        return n, "表格行数"
    return 0, "未识别到列表"


def _page_alive(page: Any) -> bool:
    try:
        return page is not None and not page.is_closed()
    except Exception:
        return False


def _page_usable(page: Any, *, timeout_ms: int = 1500) -> bool:
    """is_closed 为 False 时 page 仍可能已不可用, 需轻量探测."""
    if not _page_alive(page):
        return False
    try:
        page.evaluate("() => true", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _read_body_safe(page: Any) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def _url_query_id(url: str) -> str:
    """主实体 ID (委托 entity_discover, 不硬编码业务字段名)."""
    return pick_primary_url_id(url)[0]


def _path_entity_segments(url: str) -> list[str]:
    """路径/fragment 中的数字或 UUID 段 (REST 式资源 URL)."""
    parsed = urlparse(url or "")
    path = parsed.fragment or parsed.path or ""
    segs = [s for s in path.split("?")[0].split("/") if s]
    return [s for s in segs if _NUM.match(s) or _UUID.match(s)]


def _url_safe(page: Any) -> str:
    """读取 URL 不依赖 evaluate, 导航中 page 可能暂时不可用."""
    try:
        return page.url or ""
    except Exception:
        return ""


def is_detail_submission_url(url: str) -> bool:
    """单资源上下文页: URL 含实体主键, 或路径/fragment 含数字/UUID 段 (无业务路径字面量)."""
    if extract_url_entity_map(url):
        return True
    parsed = urlparse(url or "")
    path = parsed.fragment or parsed.path or ""
    segs = [s for s in path.split("?")[0].split("/") if s]
    return any(_NUM.match(s) or _UUID.match(s) for s in segs)


def url_matches_anchor(url: str, anchor: Any) -> bool:
    """当前 URL 是否与运行时记录的兄弟 tab (list_anchor) 一致."""
    if anchor is None or not (url or "").strip():
        return False
    try:
        anchor_url = anchor.url or ""
    except Exception:
        return False
    if not anchor_url:
        return False
    return normalize_url(url) == normalize_url(anchor_url)


def submit_left_detail_context(
    url_before: str,
    url_now: str,
    *,
    list_anchor: Any = None,
) -> bool:
    """提交后是否已离开详情上下文 (回到 list_anchor 或其它非详情页)."""
    if not is_detail_submission_url(url_before):
        return False
    if url_now and url_matches_anchor(url_now, list_anchor):
        return True
    if url_now and not is_detail_submission_url(url_now):
        return normalize_url(url_now) != normalize_url(url_before)
    return False


def still_on_same_detail_after_submit(url_before: str, url_now: str) -> bool:
    """提交等待 timeout 后仍停在同一详情实体 (未关 tab / 未切下一任务)."""
    if not url_before or not url_now:
        return False
    if not is_detail_submission_url(url_before) or not is_detail_submission_url(url_now):
        return False
    if url_entity_maps_differ(url_before, url_now):
        return False
    if normalize_url(url_before) != normalize_url(url_now):
        return False
    id_b, id_n = _url_query_id(url_before), _url_query_id(url_now)
    if id_b and id_n and id_b != id_n:
        return False
    return True


def recover_after_submit_tab_close(
    page: Any,
    *,
    url_before: str = "",
    list_anchor: Any = None,
    max_polls: int = 20,
    poll_ms: int = 150,
) -> tuple[Any, bool, str]:
    """提交后详情 tab 可能关闭: 轮询切到仍存活 tab, 返回 (page, recovered, url_now)."""
    recovered = False
    cur = page
    want_leave = is_detail_submission_url(url_before)

    def _left(u: str) -> bool:
        return bool(want_leave and submit_left_detail_context(
            url_before, u, list_anchor=list_anchor,
        ))

    for _ in range(max_polls):
        if _page_alive(cur):
            u = _url_safe(cur)
            if still_on_same_detail_after_submit(url_before, u):
                return cur, recovered, u
            if _left(u) or not want_leave:
                return cur, recovered, u

        cur, changed = recover_active_page(cur, prefer=list_anchor)
        if changed:
            recovered = True

        if list_anchor is not None and _page_alive(list_anchor):
            cur_url = _url_safe(cur) if _page_alive(cur) else ""
            should_use_anchor = not _page_alive(cur) or _left(cur_url)
            if should_use_anchor:
                try:
                    list_anchor.bring_to_front()
                except Exception:
                    pass
                cur = list_anchor
                recovered = True
                if _page_alive(cur):
                    return cur, recovered, _url_safe(cur)

        ctx = _context_from_any(cur, list_anchor)
        if ctx is not None:
            for p in reversed(list(ctx.pages)):
                if not _page_alive(p):
                    continue
                u = _url_safe(p)
                if _left(u):
                    try:
                        p.bring_to_front()
                    except Exception:
                        pass
                    return p, True, u
        try:
            if _page_alive(cur) and ctx is not None:
                for p in ctx.pages:
                    if _page_usable(p):
                        p.wait_for_timeout(poll_ms)
                        break
                else:
                    time.sleep(poll_ms / 1000.0)
            else:
                time.sleep(poll_ms / 1000.0)
        except Exception:
            time.sleep(poll_ms / 1000.0)

    cur, changed = recover_active_page(cur, prefer=list_anchor)
    recovered = recovered or changed
    cur_url = _url_safe(cur) if _page_alive(cur) else ""
    if list_anchor is not None and _page_alive(list_anchor):
        if not _page_alive(cur) or submit_left_detail_context(
            url_before, cur_url, list_anchor=list_anchor,
        ):
            cur = list_anchor
            recovered = True
    u = _url_safe(cur) if _page_usable(cur) else ""
    if not u and list_anchor is not None and _page_usable(list_anchor):
        u = _url_safe(list_anchor)
        cur = list_anchor
    if want_leave and is_detail_submission_url(url_before):
        if _page_usable(cur) and _left(u):
            return cur, recovered or True, u
        for p in find_usable_context_pages(cur, list_anchor):
            pu = _url_safe(p)
            if not is_detail_submission_url(pu) or _left(pu):
                try:
                    p.bring_to_front()
                except Exception:
                    pass
                return p, True, pu
        usable = find_usable_context_pages(cur, list_anchor)
        if len(usable) == 1 and not _page_usable(cur):
            p = usable[0]
            try:
                p.bring_to_front()
            except Exception:
                pass
            return p, True, _url_safe(p)
    return cur, recovered, u


def find_usable_context_pages(*pages: Any) -> list[Any]:
    """同 browser context 内所有仍可用 tab."""
    ctx = _context_from_any(*pages)
    if ctx is None:
        out: list[Any] = []
        for p in pages:
            if p is not None and _page_usable(p) and p not in out:
                out.append(p)
        return out
    try:
        all_pages = list(ctx.pages)
    except Exception:
        return []

    # 优先返回 evaluate 可用的 tab
    usable = [p for p in all_pages if _page_usable(p)]
    if usable:
        return usable

    # evaluate 全部超时 → fallback 到 is_closed=False 的 tab
    alive = [p for p in all_pages if _page_alive(p)]
    return alive


def find_list_tab_anchor(page: Any, list_anchor: Any = None) -> Any:
    """打开 popup 时记录的 anchor; 无记录时取 context 中最早存活的 tab."""
    if list_anchor is not None and _page_alive(list_anchor):
        return list_anchor
    ctx = _context_from_any(page, list_anchor)
    if ctx is not None:
        try:
            alive = [p for p in ctx.pages if _page_alive(p)]
            if alive:
                return alive[0]
        except Exception:
            pass
    if _page_alive(page):
        return page
    return list_anchor


def pick_surviving_tab_after_detail_close(
    page: Any,
    *,
    url_before: str = "",
    list_anchor: Any = None,
) -> tuple[Any, bool, str, bool]:
    """active 关闭后选存活 tab. 返回 (page, recovered, url, left_context).

    Tab 切换纯 anchor 模型; url_before 仅用于提交层 left_detail 语义 (不参与选 tab).
    """
    url_now = _url_safe(page) if _page_usable(page) else ""
    if not url_now and _page_alive(page):
        url_now = _url_safe(page)
    _dbg_pick1 = f"page_alive={_page_alive(page)} page_usable={_page_usable(page)} url_now={url_now[:80] if url_now else '<empty>'}"

    if _page_alive(page):
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page, False, url_now or _url_safe(page), False

    anchor = find_list_tab_anchor(page, list_anchor)
    _dbg_anchor_url = ""
    try:
        _dbg_anchor_url = _url_safe(anchor) if anchor else "<None>"
    except Exception:
        _dbg_anchor_url = "<err>"

    page, recovered, url = recover_after_submit_tab_close(
        page,
        url_before=url_before,
        list_anchor=anchor,
        max_polls=8,
    )
    _dbg_recover = f"after_recover: page_alive={_page_alive(page)} page_usable={_page_usable(page)} url={(_url_safe(page) or '<none>')[:80]} recovered={recovered}"
    print(f"  [cyan]pick_surviving: [{_dbg_pick1}] anchor={_dbg_anchor_url[:80]} → {_dbg_recover}[/cyan]")

    left = False
    if not is_detail_submission_url(url_before):
        return page, recovered, url, left

    if not _page_alive(page):
        if anchor is not None and _page_alive(anchor):
            try:
                anchor.bring_to_front()
            except Exception:
                pass
            return anchor, True, _url_safe(anchor), True
        usable = find_usable_context_pages(page, anchor)
        if usable:
            p = usable[0]
            try:
                p.bring_to_front()
            except Exception:
                pass
            return p, True, _url_safe(p), True

    if submit_left_detail_context(url_before, url, list_anchor=anchor):
        left = True
    elif _page_usable(page) and not is_detail_submission_url(url):
        left = True
    elif anchor is not None and _page_alive(anchor) and page is not anchor:
        if still_on_same_detail_after_submit(url_before, url) and _page_alive(page):
            pass
        elif not _page_alive(page):
            page = anchor
            try:
                page.bring_to_front()
            except Exception:
                pass
            url = _url_safe(page)
            left = True
            recovered = True
    elif not _page_alive(page):
        usable = find_usable_context_pages(page, anchor)
        pick = None
        if anchor is not None and _page_alive(anchor):
            pick = anchor
        elif usable:
            pick = usable[0]
        if pick is not None:
            page = pick
            try:
                page.bring_to_front()
            except Exception:
                pass
            url = _url_safe(page)
            left = True
            recovered = True
    return page, recovered, url, left


def find_sibling_tab_anchor(current: Any) -> Any:
    """多 tab 时选取非当前详情页的兄弟 tab, 不依赖业务 URL 字面量."""
    ctx = _context_from_any(current)
    if ctx is None:
        return None
    try:
        pages = list(ctx.pages)
    except Exception:
        return None
    cur_detail = (
        is_detail_submission_url(_url_safe(current))
        if _page_usable(current)
        else True
    )
    others: list[Any] = []
    for p in pages:
        if not _page_usable(p) or p is current:
            continue
        others.append(p)
        if cur_detail and not is_detail_submission_url(_url_safe(p)):
            return p
    for p in others:
        if count_real_table_rows(p) > 0:
            return p
    return others[0] if others else None


_NAV_SETTLE_OUTCOMES = frozenset({
    "returned_to_list", "resource_id_changed", "route_changed",
})


def operation_caused_navigation(
    meta: Optional[dict[str, Any]],
    *,
    url_before: str = "",
    url_now: str = "",
    outcome: str = "",
) -> bool:
    """操作后是否发生需 DOM settle 的导航 (与列表/详情等业务 URL 无关)."""
    meta = meta or {}
    if outcome in _NAV_SETTLE_OUTCOMES:
        return True
    if meta.get("new_tab_opened"):
        return True
    before = (url_before or "").strip()
    now = (url_now or "").strip()
    if before and now and normalize_url(before) != normalize_url(now):
        return True
    return False


def classify_navigation_outcome(
    url_before: str,
    url_now: str,
    *,
    list_url: str = "",
) -> Optional[str]:
    """比较 URL 路径模板 / 资源 ID / 列表锚点, 不依赖业务路径字面量."""
    now = (url_now or "").strip()
    before = (url_before or "").strip()
    if not now:
        return None
    norm_now = normalize_url(now)
    norm_before = normalize_url(before)
    norm_list = normalize_url(list_url) if list_url else ""

    if norm_list and norm_now == norm_list:
        return "returned_to_list"
    if url_entity_maps_differ(before, now):
        return "resource_id_changed"
    id_before = _url_query_id(before)
    id_now = _url_query_id(now)
    if id_before and id_now and id_before != id_now:
        return "resource_id_changed"
    path_before = _path_entity_segments(before)
    path_now = _path_entity_segments(now)
    if path_before and path_now and path_before != path_now:
        return "resource_id_changed"
    if is_detail_submission_url(before) and now and not is_detail_submission_url(now):
        return "returned_to_list"
    if norm_before and norm_now != norm_before:
        return "route_changed"
    return None


_NAV_SUCCESS_OUTCOMES = frozenset({
    "resource_id_changed", "returned_to_list", "route_changed",
})


def is_same_tab_detail_entity_nav(
    url_before: str,
    url_after: str,
    outcome: str = "",
) -> bool:
    """同 tab 内详情实体切换 (自动下一任务), 无需 tab recover / 长 wait_before_assert."""
    if outcome and outcome not in ("resource_id_changed", "route_changed"):
        return False
    if not is_detail_submission_url(url_before):
        return False
    if not url_after or not is_detail_submission_url(url_after):
        return False
    if outcome in ("resource_id_changed", "route_changed"):
        return True
    return url_entity_maps_differ(url_before, url_after)


def _collect_context_pages(page: Any, list_anchor: Any = None) -> list[Any]:
    """收集当前 context 内可用 tab, 当前页优先."""
    pages: list[Any] = []
    seen: set[int] = set()
    if _page_usable(page):
        pages.append(page)
        seen.add(id(page))
    ctx = _context_from_any(page, list_anchor)
    if ctx is not None:
        try:
            for p in ctx.pages:
                if _page_usable(p) and id(p) not in seen:
                    pages.append(p)
                    seen.add(id(p))
        except Exception:
            pass
    if list_anchor is not None and _page_usable(list_anchor) and id(list_anchor) not in seen:
        pages.append(list_anchor)
    return pages


def _scan_pages_for_nav_outcome(
    pages: list[Any],
    url_before: str,
    *,
    list_url: str = "",
    entity_before: str = "",
) -> tuple[Optional[str], Any]:
    """扫描多 tab, 综合 URL/DOM 信号推断导航结局."""
    fallback: tuple[Optional[str], Any] = (None, None)
    for p in pages:
        if not _page_usable(p):
            continue
        out = detect_submit_navigation_progress(
            url_before,
            p,
            entity_before=entity_before,
            list_url=list_url,
            classify_fn=classify_navigation_outcome,
            url_safe_fn=_url_safe,
            page_usable_fn=_page_usable,
            read_body_fn=_read_body_safe,
            check_dom=bool(entity_before),
        )
        if out == "resource_id_changed":
            return out, p
        if out in _NAV_SUCCESS_OUTCOMES and fallback[0] is None:
            fallback = (out, p)
    return fallback


def _try_wait_url_entity_change(
    page: Any,
    url_before: str,
    *,
    timeout_ms: int = 2000,
) -> Optional[str]:
    return try_wait_url_entity_change(
        page,
        url_before,
        timeout_ms=timeout_ms,
        classify_fn=classify_navigation_outcome,
        url_safe_fn=_url_safe,
        page_usable_fn=_page_usable,
    )


def _body_has_submit_error(body: str) -> bool:
    return any(m in body for m in _SUBMIT_ERROR_MARKERS)


def _context_from_any(*pages: Any) -> Any:
    """从仍存活的 page 对象取得 browser context (已关闭 tab 上 context 可能不可用)."""
    for p in pages:
        if p is None:
            continue
        try:
            if not p.is_closed():
                return p.context
        except Exception:
            continue
    for p in pages:
        if p is None:
            continue
        try:
            return p.context
        except Exception:
            continue
    return None


def is_table_row_click_intent(intent: str) -> bool:
    """表格行内按钮 (对应/该行/工单等), 非侧栏/筛选区."""
    text = intent or ""
    if "点击" not in text:
        return False
    if any(w in text for w in ("侧栏", "菜单", "下拉", "筛选区")):
        return False
    if re.search(r"作为(搜索|筛选)类型|搜索类型|筛选类型", text):
        return False
    # 行内按钮: ${id}的'按钮' 或 数字ID的'按钮'
    if re.search(r"(?:\d{4,}|\$\{[^}]+\})的[「'\"]", text):
        return True
    # readiness/recovery 常见: 第一条任务(146550684)的查看按钮
    if re.search(r"任务[（(](?:\d{4,}|\$\{[^}]+\})[）)]", text):
        return True
    if re.search(r"第[一二三四五六七八九十\d]+条", text) and re.search(
        r"(?:任务|记录|工单|订单).*(?:的|中).*(?:按钮|查看|编辑|删除|日志)",
        text,
    ):
        return True
    markers = (
        "对应", "该行", "此行", "列表中", "某行", "工单", "订单", "记录", "行内",
        "第一个", "第一行", "首行", "首条", "第一条", "任务的",
    )
    if not any(m in text for m in markers):
        return False
    # 「工单」会误匹配筛选字段「工单ID」, 行内点击须含行标识 (变量/数字/的'按钮')
    if "工单" in text and not re.search(
        r"(?:的[「'\"]|对应|该行|列表中|\$\{|\d{4,})",
        text,
    ):
        return False
    return True


def _extract_status_hint_from_intent(intent: str) -> Optional[str]:
    """从 intent 抽取状态过滤值, 如 生产状态为「已退场」."""
    text = intent or ""
    for pat in (
        r"(?:生产)?状态[为是][「'\"]([^」'\"]+)[」'\"]",
        r"(?:生产)?状态[为是]\s*([^\s，。;；（(]+)",
    ):
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip()
            if val:
                return val
    return None


def parse_table_row_click(
    intent: str,
    extras: Optional[dict[str, Any]] = None,
) -> Optional[tuple[str, str, Optional[str]]]:
    """(按钮文案, 行提示, 状态列过滤值). extras 可覆盖 row_key / status_filter."""
    ex = extras or {}
    row_key_extra = str(ex.get("row_key") or "").strip()
    status_extra = str(ex.get("status_filter") or ex.get("status") or "").strip()

    text = (intent or "").strip()
    if not row_key_extra and not is_table_row_click_intent(text):
        return None

    button = str(ex.get("button") or "").strip()
    row_hint = row_key_extra
    status_hint = status_extra or None

    if text:
        quoted = re.findall(
            r"[\"'“”‘’「」『』]([^\"'“”‘’「」『』]+)[\"'“”‘’「」『』]",
            text,
        )
        if not button:
            btn_m = re.search(r"点击[「'\"]([^」'\"]+)[」'\"]", text)
            button = (btn_m.group(1) if btn_m else (quoted[-1] if quoted else "")).strip()
        if not button:
            bare_btn = re.search(r"的([^'\"「」\s，。;；（(]+)按钮", text)
            if bare_btn:
                button = bare_btn.group(1).strip()
        if not row_hint:
            task_id_m = re.search(r"任务[（(]([^）)]+)[）)]", text)
            if task_id_m:
                row_hint = task_id_m.group(1).strip()
            for block in reversed(re.findall(r"[（(]([^）)]+)[）)]", text)):
                m = re.search(r"选择[了]?[「'\"]([^」'\"]+)[」'\"]", block)
                if m:
                    row_hint = m.group(1).strip()
                    break
            if not row_hint:
                m = re.search(r"选择[了]?[「'\"]([^」'\"]+)[」'\"]", text)
                if m:
                    row_hint = m.group(1).strip()
            m = re.search(r"工单ID[为是]?\s*(\d+)", text)
            if m:
                row_hint = m.group(1).strip()
            if not row_hint and quoted:
                skip = {button}
                if status_hint:
                    skip.add(status_hint)
                for q in reversed(quoted):
                    if q not in skip:
                        row_hint = q.strip()
                        break
        if not status_hint:
            status_hint = _extract_status_hint_from_intent(text)

    if not button:
        return None
    if not row_hint:
        if re.search(r"第一个|第一行|首行|首条|第一条", text):
            row_hint = FIRST_TABLE_ROW_KEY
        elif is_table_row_click_intent(text):
            row_hint = FIRST_TABLE_ROW_KEY
        else:
            return None
    return button, row_hint, status_hint


def _button_label_variants(label: str) -> list[str]:
    """UI 按钮文案可能与用例不一致, 如「日志」vs「日 志」."""
    out: list[str] = []
    normalized = (label or "").replace("\u00a0", " ").strip()
    collapsed = re.sub(r"\s+", "", normalized)
    for v in (normalized, collapsed):
        if v and v not in out:
            out.append(v)
    # Ant Design 常在两字按钮 span 间插空格
    if collapsed and len(collapsed) == 2:
        spaced = f"{collapsed[0]} {collapsed[1]}"
        if spaced not in out:
            out.append(spaced)
    if collapsed in ("查看",) or label in ("查看", "查 看", "查\u00a0看"):
        for v in ("查看", "查 看"):
            if v not in out:
                out.append(v)
    return out


def _normalize_btn_label(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").replace("\u00a0", ""))


def _find_row_button(row: Any, label: str) -> Any:
    for variant in _button_label_variants(label):
        btn = row.get_by_role("button", name=variant)
        if btn.count():
            return btn.last
        btn = row.get_by_role("link", name=variant)
        if btn.count():
            return btn.last
        esc = variant.replace("'", "\\'")
        btn = row.locator(f"button:has-text('{esc}'), a:has-text('{esc}')")
        if btn.count():
            return btn.last
    collapsed_target = _normalize_btn_label(label)
    if not collapsed_target:
        return None
    for role in ("button", "link"):
        loc = row.get_by_role(role)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                txt = _normalize_btn_label(el.inner_text(timeout=800))
            except Exception:
                continue
            if txt == collapsed_target:
                return el
    for sel in ("button", "a", "[role='button']"):
        loc = row.locator(sel)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                txt = _normalize_btn_label(el.inner_text(timeout=800))
            except Exception:
                continue
            if txt == collapsed_target:
                return el
    return None


def _table_key_col_index(headers: list[str], configured: str) -> int:
    for name in (configured, "任务ID", "工单ID", "ID"):
        if name and name in headers:
            return headers.index(name)
    return -1


def _row_matches_key(cells: list[str], row_key: str, key_idx: int) -> bool:
    """行是否含目标 ID: 优先 key 列, 无列配置时扫整行任意单元格."""
    from .session_ops import table_row_key_matches

    if key_idx >= 0 and key_idx < len(cells):
        if table_row_key_matches(cells[key_idx], row_key):
            return True
    for cell in cells:
        if table_row_key_matches(cell, row_key):
            return True
    return False


def _row_text_contains_key(text: str, row_key: str) -> bool:
    """整行 inner_text 是否含 row_key (兼容 ant-table 固定列拆表)."""
    from .session_ops import table_row_key_matches

    key = (row_key or "").strip()
    blob = (text or "").strip()
    if not key or not blob:
        return False
    if key.isdigit():
        return bool(re.search(rf"(?<!\d){re.escape(key)}(?!\d)", blob))
    if table_row_key_matches(blob, key):
        return True
    for line in blob.splitlines():
        if table_row_key_matches(line.strip(), key):
            return True
    return False


def _locate_in_ant_split_table(
    page: Any,
    *,
    button_label: str,
    row_keys: list[str],
    status_filter: Optional[str] = None,
    status_column: str = "",
) -> tuple[Any, str]:
    """Ant Design 固定列: 工单ID 与操作列可能在不同 .ant-table-tbody 中, 按行索引对齐."""
    tbodies = page.locator(".ant-table-tbody")
    tbody_count = tbodies.count()
    if not tbody_count:
        return None, ""

    base_rows = tbodies.first.locator("tr")
    row_count = base_rows.count()
    if not row_count:
        return None, ""

    want_first = FIRST_TABLE_ROW_KEY in row_keys
    keys = [k for k in row_keys if k and k != FIRST_TABLE_ROW_KEY]

    for ri in range(row_count):
        row_parts: list[str] = []
        for bi in range(tbody_count):
            row = tbodies.nth(bi).locator("tr").nth(ri)
            try:
                row_parts.append(row.inner_text(timeout=1500))
            except Exception:
                continue
        joined = "\n".join(row_parts)
        if not joined.strip() or any(m in joined for m in _EMPTY_ROW_MARKERS):
            continue

        if want_first and not keys:
            if status_filter and status_filter not in joined:
                continue
            btn = _find_button_in_ant_row_index(page, ri, button_label)
            if btn is not None:
                return btn, f"ant_table_row[first:{ri}].{button_label}"
            continue

        matched_key = None
        for row_key in keys:
            if _row_text_contains_key(joined, row_key):
                if status_filter and status_filter not in joined:
                    continue
                matched_key = row_key
                break
        if not matched_key:
            continue
        btn = _find_button_in_ant_row_index(page, ri, button_label)
        if btn is not None:
            return btn, f"ant_table_row[{matched_key}].{button_label}"

    if want_first:
        return None, "行内定位: 未找到首行或按钮"
    return None, f"行内定位: 未找到 row_keys={keys[:3]!r}"


def _find_button_in_ant_row_index(page: Any, row_index: int, label: str) -> Any:
    tbodies = page.locator(".ant-table-tbody")
    for bi in range(tbodies.count()):
        row = tbodies.nth(bi).locator("tr").nth(row_index)
        btn = _find_row_button(row, label)
        if btn is not None:
            return btn
    return None


def _row_matches_status(cells: list[str], status_filter: str, status_idx: int) -> bool:
    if not status_filter:
        return True
    if status_idx >= 0 and status_idx < len(cells):
        return status_filter in cells[status_idx]
    joined = "".join(cells)
    return status_filter in joined


def locate_button_in_table_row(
    page: Any,
    *,
    button_label: str,
    row_keys: list[str],
    key_col: str = "",
    status_column: str = "",
    status_filter: Optional[str] = None,
) -> tuple[Any, str]:
    """在表格指定行操作列定位按钮; 多个按钮时取该行最后一个匹配.

    row_keys 由规划 extras 直接给出时, 无需 session_ops 列名配置, 会在整行单元格中匹配 ID.
    """
    label = (button_label or "").strip()
    keys = [k.strip() for k in row_keys if (k or "").strip()]
    if not label or not keys:
        return None, "行内定位: 缺少按钮或行标识"

    want_first = FIRST_TABLE_ROW_KEY in keys

    loc, note = _locate_in_ant_split_table(
        page,
        button_label=label,
        row_keys=keys,
        status_filter=status_filter,
        status_column=status_column,
    )
    if loc is not None:
        return loc, note

    for table_sel in ("table", ".ant-table table"):
        tables = page.locator(table_sel)
        for ti in range(tables.count()):
            table = tables.nth(ti)
            headers = [h.strip() for h in table.locator("thead th, thead td").all_inner_texts()]
            if not headers:
                continue
            key_idx = _table_key_col_index(headers, key_col)
            status_idx = (
                headers.index(status_column)
                if status_filter and status_column and status_column in headers
                else -1
            )
            body_rows = table.locator("tbody tr")
            if want_first:
                for ri in range(body_rows.count()):
                    row = body_rows.nth(ri)
                    cells = [c.strip() for c in row.locator("td").all_inner_texts()]
                    if not cells or any(m in "".join(cells) for m in _EMPTY_ROW_MARKERS):
                        continue
                    if status_idx >= 0 and status_filter:
                        if status_idx >= len(cells) or status_filter not in cells[status_idx]:
                            continue
                    btn = _find_row_button(row, label)
                    if btn is not None:
                        row_id = cells[key_idx] if 0 <= key_idx < len(cells) else str(ri)
                        return btn, f"table_row[first:{row_id}].{label}"
                continue
            for row_key in keys:
                if row_key == FIRST_TABLE_ROW_KEY:
                    continue
                for ri in range(body_rows.count()):
                    row = body_rows.nth(ri)
                    cells = [c.strip() for c in row.locator("td").all_inner_texts()]
                    if not _row_matches_key(cells, row_key, key_idx):
                        continue
                    if not _row_matches_status(cells, status_filter or "", status_idx):
                        continue
                    btn = _find_row_button(row, label)
                    if btn is not None:
                        hit = cells[key_idx] if 0 <= key_idx < len(cells) else row_key
                        return btn, f"table_row[{hit}].{label}"
    return None, f"行内定位: 未找到 row_keys={keys[:3]!r}"


def wait_for_table_row_button(
    page: Any,
    *,
    button_label: str,
    row_keys: list[str],
    key_col: str = "",
    status_column: str = "",
    status_filter: Optional[str] = None,
    timeout_ms: int = 15000,
) -> tuple[Any, str]:
    """轮询直到表格指定行的按钮出现 (搜索/列表刷新后再定位)."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_note = ""
    while time.monotonic() < deadline:
        loc, note = locate_button_in_table_row(
            page,
            button_label=button_label,
            row_keys=row_keys,
            key_col=key_col,
            status_column=status_column,
            status_filter=status_filter,
        )
        if loc is not None:
            return loc, note
        last_note = note
        try:
            page.wait_for_timeout(400)
        except Exception:
            break
    return None, last_note or "行内定位: 等待超时"


def _reload_list_page(page: Any, *, timeout_ms: int = 15000) -> None:
    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    try:
        from ..dom.semantic_dom import wait_for_dom_stable

        wait_for_dom_stable(page, quiet_ms=300, timeout_ms=min(timeout_ms, 8000))
    except Exception:
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass


def wait_before_assert(
    page: Any,
    quiet_ms: int = 300,
    timeout_ms: int = 3000,
    list_anchor: Any = None,
) -> Any:
    """断言前 tab 跟随 + 短稳定等待."""
    from .tab_follow import follow_active_tab, wait_and_recover_active_page

    page, _ = wait_and_recover_active_page(page, max_polls=12, prefer=list_anchor)
    page, _, _ = follow_active_tab(page, list_anchor)
    if not _page_usable(page):
        return page
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    try:
        from ..dom.semantic_dom import wait_for_dom_stable

        wait_for_dom_stable(page, quiet_ms=quiet_ms, timeout_ms=timeout_ms)
    except Exception:
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass
    return page


def wait_for_url_fragment(page: Any, fragment: str, *, timeout_ms: int = 15000) -> None:
    """等待执行期记录的 URL path 片段 (来自点击后实际跳转)."""
    if not fragment:
        return
    try:
        page.wait_for_url(f"**{fragment}**", timeout=timeout_ms)
    except Exception:
        pass
    wait_before_assert(page, timeout_ms=min(timeout_ms, 5000))


def wait_after_nav_click(page: Any, intent: str = "", *, timeout_ms: int = 15000) -> None:
    """已废弃: 保留兼容旧脚本; 新脚本应使用 wait_for_url_fragment + 执行期回填."""
    wait_before_assert(page, timeout_ms=min(timeout_ms, 5000))


def get_scoped_page_text(page: Any, region_keys: list[str] | None = None) -> str:
    """读取断言作用域内文本 (页头/左侧/表单等), 供生成脚本做区域断言."""
    from .assert_scope import AssertScope, extract_page_regions, get_scoped_text

    regions = extract_page_regions(page)
    keys = list(region_keys or ["main", "body"])
    scope = AssertScope(region_keys=keys, explicit_region=True, exclude_nav=True)
    return get_scoped_text(regions, scope)


def wait_for_list_count_at_least(
    page: Any, min_count: int, *, timeout_ms: int = 15000,
) -> tuple[int, str]:
    """轮询直到列表计数达到下限 (领取/提交后列表刷新用)."""
    deadline = time.monotonic() + timeout_ms / 1000
    last = (0, "未识别到列表")
    while time.monotonic() < deadline:
        last = measure_list_count(page)
        if last[0] >= min_count:
            return last
        try:
            page.wait_for_timeout(400)
        except Exception:
            break
    return last


def min_count_for_compare(op: str, threshold: int) -> int:
    """把 compare_count 条件转为 wait_for_list_count_at_least 的下限."""
    if op == ">":
        return threshold + 1
    if op in (">=", "=="):
        return threshold
    return 1


def compare_count(actual: int, threshold: int, op: str) -> bool:
    """与 dispatcher._compare_count 一致."""
    return {
        ">": actual > threshold,
        ">=": actual >= threshold,
        "<": actual < threshold,
        "<=": actual <= threshold,
        "==": actual == threshold,
    }.get(op, actual == threshold)


def iter_real_table_row_texts(page: Any) -> list[str]:
    """读取列表/表格有效数据行的 inner_text (排除空行与「暂无数据」)."""
    texts: list[str] = []
    for sel in (".ant-table-tbody tr", "table tbody tr"):
        try:
            rows = page.locator(sel)
            for i in range(rows.count()):
                try:
                    text = rows.nth(i).inner_text(timeout=2000)
                except Exception:
                    text = ""
                t = text.strip()
                if t and not any(m in t for m in _EMPTY_ROW_MARKERS):
                    texts.append(t)
            if texts:
                return texts
        except Exception:
            continue
    return texts


def assert_all_table_rows_contain(page: Any, needle: str) -> tuple[bool, str, int]:
    """列表页「所有行均含某文本」结构化断言."""
    rows = iter_real_table_row_texts(page)
    if not rows:
        return False, "列表行断言: 未识别到有效数据行", 0
    bad = [i + 1 for i, t in enumerate(rows) if needle not in t]
    if bad:
        sample = bad[:5]
        extra = f" 等{len(bad)}行" if len(bad) > 5 else ""
        return False, f"列表行断言: 第 {sample}{extra} 未包含 {needle!r}", len(rows)
    return True, f"列表行断言: {len(rows)} 行均包含 {needle!r}", len(rows)


def assert_no_table_row_contains(page: Any, needle: str) -> tuple[bool, str, int]:
    """列表页「所有行均不含某文本」结构化断言."""
    rows = iter_real_table_row_texts(page)
    if not rows:
        return True, f"列表行断言: 无数据行, 视为不包含 {needle!r}", 0
    bad = [i + 1 for i, t in enumerate(rows) if needle in t]
    if bad:
        return False, f"列表行断言: 第 {bad[:5]} 行仍包含 {needle!r}", len(rows)
    return True, f"列表行断言: {len(rows)} 行均不包含 {needle!r}", len(rows)


def extract_url_query(page: Any, *keys: str) -> dict[str, str]:
    """从当前页 URL 查询串提取参数 (如详情页 uniqId → workId)."""
    try:
        url = getattr(page, "url", "") or ""
    except Exception:
        return {}
    qs = parse_qs(urlparse(url).query)
    out: dict[str, str] = {}
    for key in keys:
        vals = qs.get(key) or []
        if vals and str(vals[0]).strip():
            out[key] = str(vals[0]).strip()
    if "uniqId" in out and "workId" not in out:
        out["workId"] = out["uniqId"]
    return out


def assert_table_cell(
    page: Any,
    row_key: str,
    key_col: str,
    target_col: str,
    expected: str,
) -> None:
    """断言表格中某行某列的值 (与 dispatcher._assert_table 行匹配规则一致)."""
    from .session_ops import table_row_key_matches

    tables = page.locator("table")
    for ti in range(tables.count()):
        table = tables.nth(ti)
        headers = [h.strip() for h in table.locator("thead th, thead td").all_inner_texts()]
        if not headers or key_col not in headers or target_col not in headers:
            continue
        key_idx = headers.index(key_col)
        col_idx = headers.index(target_col)
        body_rows = table.locator("tbody tr")
        exact_row: tuple[list[str], str] | None = None
        token_row: tuple[list[str], str] | None = None
        for ri in range(body_rows.count()):
            cells = [c.strip() for c in body_rows.nth(ri).locator("td").all_inner_texts()]
            if key_idx >= len(cells):
                continue
            cell_val = cells[key_idx]
            if not table_row_key_matches(cell_val, row_key):
                continue
            if cell_val.strip() == str(row_key).strip():
                exact_row = (cells, cell_val)
                break
            if token_row is None:
                token_row = (cells, cell_val)
        hit = exact_row or token_row
        if hit:
            cells, matched = hit
            actual = cells[col_idx] if col_idx < len(cells) else ""
            if expected in actual or actual == expected:
                return
            raise AssertionError(
                f"表格断言失败: 行 {matched!r} 列 {target_col!r} "
                f"期望 {expected!r} 实际 {actual!r}"
            )
    raise AssertionError(
        f"表格断言失败: 未找到行 {row_key!r} (列 {key_col!r}) 或列 {target_col!r}"
    )


from .tab_follow import (  # noqa: E402  — 统一 tab 跟随, 避免与上方循环 import
    recover_active_page,
    wait_and_recover_active_page,
    wait_after_detail_submit,
)

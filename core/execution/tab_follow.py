"""Tab 跟随: popup 打开 → active=新 tab; active 关闭 → 回 list_anchor.

Tab 切换仅依赖 page 引用与 anchor, 不用 URL 区分 tab 角色.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .script_helpers import (
    _NAV_SUCCESS_OUTCOMES,
    _body_has_submit_error,
    _context_from_any,
    _page_alive,
    _page_usable,
    _read_body_safe,
    _reload_list_page,
    _url_safe,
    classify_navigation_outcome,
    find_usable_context_pages,
    is_detail_submission_url,
    still_on_same_detail_after_submit,
    submit_left_detail_context,
    url_matches_anchor,
)
from .nav_progress import capture_submit_entity_before, try_wait_url_entity_change

# 提交后 tab/URL 变化探测上限 (与 click default_timeout 解耦)
DEFAULT_SUBMIT_WAIT_MS = 8000
_SUBMIT_POLL_MS = 100


def ordered_usable_tabs(*hints: Any) -> list[Any]:
    """context.pages 顺序下所有仍可用 tab (末尾 = 最新打开)."""
    ctx = _context_from_any(*hints)
    if ctx is None:
        out: list[Any] = []
        for p in hints:
            if p is not None and _page_usable(p, timeout_ms=300) and p not in out:
                out.append(p)
        return out
    try:
        return [p for p in ctx.pages if _page_usable(p, timeout_ms=300)]
    except Exception:
        return find_usable_context_pages(*hints)


def ordered_alive_tabs(*hints: Any) -> list[Any]:
    """仍存活 tab (含 evaluate 暂时超时但 URL 可读)."""
    ctx = _context_from_any(*hints)
    if ctx is None:
        out: list[Any] = []
        for p in hints:
            if p is not None and _page_alive(p) and p not in out:
                out.append(p)
        return out
    try:
        return [p for p in ctx.pages if _page_alive(p)]
    except Exception:
        alive: list[Any] = []
        for p in hints:
            if p is not None and _page_alive(p) and p not in alive:
                alive.append(p)
        return alive


def newest_usable_tab(*hints: Any) -> Any:
    tabs = ordered_usable_tabs(*hints)
    return tabs[-1] if tabs else None


def follow_active_tab(
    active: Any,
    list_anchor: Any = None,
) -> tuple[Any, bool, str]:
    """解析当前应操作的 tab (粘滞: 详情 tab 存活则一直在 tab2)."""
    from .script_helpers import sticky_resolve_tab

    return sticky_resolve_tab(active, list_anchor)


def recover_active_page(page: Any, prefer: Any = None) -> tuple[Any, bool]:
    """兼容旧 API: 切到应跟随的 tab."""
    anchor = prefer
    resolved, switched, _ = follow_active_tab(page, anchor)
    return resolved, switched


def wait_and_recover_active_page(
    page: Any,
    *,
    poll_ms: int = _SUBMIT_POLL_MS,
    max_polls: int = 15,
    prefer: Any = None,
) -> tuple[Any, bool]:
    """短轮询 + tab 跟随 (不再长时间 blind poll)."""
    recovered = False
    cur = page
    ctx = _context_from_any(page, prefer)
    count_seen = len(ctx.pages) if ctx else 1

    for _ in range(max_polls):
        cur, switched, _ = follow_active_tab(cur, prefer)
        if switched:
            recovered = True
        if _page_usable(cur, timeout_ms=400):
            return cur, recovered
        if ctx is not None:
            try:
                new_page = ctx.wait_for_event("page", timeout=min(poll_ms, 200))
                if _page_usable(new_page, timeout_ms=500):
                    try:
                        new_page.bring_to_front()
                    except Exception:
                        pass
                    return new_page, True
            except Exception:
                pass
            try:
                now_count = len(ctx.pages)
            except Exception:
                now_count = count_seen
            if now_count > count_seen:
                count_seen = now_count
                newest = newest_usable_tab(cur, prefer)
                if newest is not None:
                    cur = newest
                    recovered = True
                    if _page_usable(cur, timeout_ms=400):
                        return cur, recovered
            for p in ordered_usable_tabs(cur, prefer):
                try:
                    p.wait_for_timeout(min(poll_ms, 150))
                    break
                except Exception:
                    time.sleep(min(poll_ms, 150) / 1000.0)
                    break
            else:
                time.sleep(min(poll_ms, 150) / 1000.0)
        else:
            time.sleep(min(poll_ms, 150) / 1000.0)

    cur, switched, _ = follow_active_tab(cur, prefer)
    return cur, recovered or switched


def _scan_tabs_for_outcome(
    url_before: str,
    *,
    list_url: str = "",
    hints: tuple[Any, ...],
) -> tuple[Optional[str], Any]:
    from .script_helpers import _log_tab_handoff, tab_handoff_snapshot

    tabs = ordered_usable_tabs(*hints)
    for p in reversed(tabs):
        purl = _url_safe(p)
        out = classify_navigation_outcome(
            url_before, purl, list_url=list_url,
        )
        if out not in _NAV_SUCCESS_OUTCOMES:
            continue
        # 详情+列表双 tab 并存时, 列表 URL 恒「像」returned_to_list; 详情 tab 仍存活则忽略
        if (
            is_detail_submission_url(url_before)
            and out == "returned_to_list"
            and not is_detail_submission_url(purl)
        ):
            live_detail = any(
                _page_usable(t, timeout_ms=300)
                and is_detail_submission_url(_url_safe(t))
                for t in tabs
            )
            if not live_detail:
                live_detail = any(
                    _page_alive(t) and is_detail_submission_url(_url_safe(t))
                    for t in ordered_alive_tabs(*hints)
                )
            if live_detail:
                _log_tab_handoff(
                    "[submit_scan] skip returned_to_list on list tab; detail tab still alive"
                )
                continue
        _log_tab_handoff(
            f"[submit_scan] hit usable tab outcome={out} url={purl[:70]}"
        )
        return out, p

    # evaluate 暂时失败但 URL 已变的详情 tab (提交后同 tab 切下一任务)
    if is_detail_submission_url(url_before):
        for p in reversed(ordered_alive_tabs(*hints)):
            purl = _url_safe(p)
            if not purl or not is_detail_submission_url(purl):
                continue
            out = classify_navigation_outcome(
                url_before, purl, list_url=list_url,
            )
            if out in ("resource_id_changed", "route_changed"):
                _log_tab_handoff(
                    f"[submit_scan] hit weak detail tab outcome={out} url={purl[:70]}"
                )
                return out, p
    return None, None


def wait_after_detail_submit(
    page: Any,
    *,
    list_anchor: Any = None,
    url_before: str = "",
    budget_ms: int = DEFAULT_SUBMIT_WAIT_MS,
    poll_ms: int = _SUBMIT_POLL_MS,
    max_polls: int = 0,  # 兼容旧签名; 由 budget_ms 驱动
) -> tuple[Any, str, bool]:
    """提交后事件驱动等待: 跟 tab + 扫 URL, 预算内结束, 不返回 timeout."""
    del max_polls  # budget 驱动
    recovered = False
    cur = page
    list_url = _url_safe(list_anchor) if list_anchor is not None else ""
    ctx = _context_from_any(page, list_anchor)
    count_before = len(ctx.pages) if ctx else 1
    deadline = time.monotonic() + budget_ms / 1000.0

    def _finish(outcome: str, target: Any) -> tuple[Any, str, bool]:
        if outcome == "returned_to_list" and _page_usable(target, timeout_ms=500):
            _reload_list_page(target)
        try:
            target.bring_to_front()
        except Exception:
            pass
        return target, outcome, recovered

    out, hit = _scan_tabs_for_outcome(
        url_before, list_url=list_url, hints=(cur, list_anchor),
    )
    if hit is not None and out:
        if hit is not cur:
            recovered = True
            cur = hit
        return _finish(out, cur)

    entity_before = capture_submit_entity_before(url_before)
    if entity_before and _page_usable(cur, timeout_ms=400):
        fast_out = try_wait_url_entity_change(
            cur,
            url_before,
            timeout_ms=min(2000, budget_ms),
            classify_fn=classify_navigation_outcome,
            url_safe_fn=_url_safe,
            page_usable_fn=_page_usable,
        )
        if fast_out in _NAV_SUCCESS_OUTCOMES:
            return _finish(fast_out, cur)

    while time.monotonic() < deadline:
        remain_ms = int((deadline - time.monotonic()) * 1000)
        if remain_ms <= 0:
            break

        cur, switched, _ = follow_active_tab(cur, list_anchor)
        if switched:
            recovered = True

        if not _page_alive(cur) or not _page_usable(cur, timeout_ms=200):
            cur, switched, _ = follow_active_tab(cur, list_anchor)
            if switched:
                recovered = True
            out, hit = _scan_tabs_for_outcome(
                url_before, list_url=list_url, hints=(cur, list_anchor, page),
            )
            if hit and out:
                cur = hit
                recovered = True
                return _finish(out, cur)
            # cur 暂时不可用时不在此处切 list_anchor, 继续轮询弱详情 tab

        if ctx is not None:
            try:
                if len(ctx.pages) > count_before:
                    count_before = len(ctx.pages)
                    newest = newest_usable_tab(cur, list_anchor)
                    if newest is not None and newest is not cur:
                        cur = newest
                        recovered = True
                        out, _ = _scan_tabs_for_outcome(
                            url_before, list_url=list_url, hints=(cur, list_anchor),
                        )
                        if out:
                            return _finish(out, cur)
            except Exception:
                pass
            try:
                new_p = ctx.wait_for_event("page", timeout=min(remain_ms, poll_ms))
                if _page_usable(new_p, timeout_ms=500):
                    cur = new_p
                    recovered = True
                    out, _ = _scan_tabs_for_outcome(
                        url_before, list_url=list_url, hints=(cur, list_anchor),
                    )
                    if out:
                        return _finish(out, cur)
            except Exception:
                pass

        out, hit = _scan_tabs_for_outcome(
            url_before, list_url=list_url, hints=(cur, list_anchor),
        )
        if hit and out:
            if hit is not cur:
                cur = hit
                recovered = True
            return _finish(out, cur)

        if _page_usable(cur, timeout_ms=200):
            body = _read_body_safe(cur)
            if _body_has_submit_error(body):
                return cur, "submit_error", recovered

        for p in ordered_usable_tabs(cur, list_anchor):
            try:
                p.wait_for_timeout(min(poll_ms, remain_ms, 150))
                break
            except Exception:
                time.sleep(min(poll_ms, 150) / 1000.0)
                break
        else:
            time.sleep(min(poll_ms, 150) / 1000.0)

    cur, switched, _ = follow_active_tab(cur, list_anchor)
    recovered = recovered or switched

    out, hit = _scan_tabs_for_outcome(
        url_before, list_url=list_url, hints=(cur, list_anchor, page),
    )
    if hit and out:
        return _finish(out, hit)

    if is_detail_submission_url(url_before):
        for p in reversed(ordered_alive_tabs(cur, list_anchor, page)):
            purl = _url_safe(p)
            if not is_detail_submission_url(purl):
                continue
            late_out = classify_navigation_outcome(
                url_before, purl, list_url=list_url,
            )
            if late_out in ("resource_id_changed", "route_changed"):
                from .script_helpers import _log_tab_handoff
                _log_tab_handoff(
                    f"[submit_scan] final weak detail outcome={late_out} url={purl[:70]}"
                )
                return _finish(late_out, p)

    url_now = _url_safe(cur)
    if not _page_alive(page) and list_anchor is not None and _page_usable(list_anchor, timeout_ms=400):
        recovered = True
        return _finish("returned_to_list", list_anchor)

    if submit_left_detail_context(url_before, url_now, list_anchor=list_anchor):
        return _finish("returned_to_list", cur)

    if url_matches_anchor(url_now, list_anchor):
        return _finish("returned_to_list", cur)

    if still_on_same_detail_after_submit(url_before, url_now) and _page_usable(cur, timeout_ms=400):
        return cur, "settled", recovered

    if _page_usable(cur, timeout_ms=400):
        body = _read_body_safe(cur)
        if _body_has_submit_error(body):
            return cur, "submit_error", recovered
        return cur, "settled", recovered

    if list_anchor is not None and _page_usable(list_anchor, timeout_ms=400):
        recovered = True
        return _finish("returned_to_list", list_anchor)

    return cur, "settled", recovered

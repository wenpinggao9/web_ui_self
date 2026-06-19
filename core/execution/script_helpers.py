"""供 run.py 与 codegen 脚本共用的页面辅助 (避免逻辑重复)."""
from __future__ import annotations

import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from ..locating.normalize import normalize_url

_EMPTY_ROW_MARKERS = ("暂无数据", "无数据", "No data", "no data")
_SUBMIT_ERROR_MARKERS = ("任务已处理", "请勿重复提交", "不可重复提交")


def bring_page_to_front(page: Any) -> None:
    """将指定 page 所在浏览器窗口置于最前 (多角色/多 context 切换时用)."""
    try:
        if page is not None and _page_usable(page):
            page.bring_to_front()
    except Exception:
        pass


def pick_role_handoff_page(
    context: Any,
    current_page: Any = None,
    *,
    list_anchor: Any = None,
    primary_page: Any = None,
) -> Any:
    """跨用例/跨角色复用时选稳定 tab, 仅依赖运行时 tab 关系, 不假设 URL 形态.

    优先级: list_anchor > primary_page > 多 tab 时非 current 的首个 tab > 首个可用 tab > recover.
    """
    for candidate in (list_anchor, primary_page):
        if candidate is not None and _page_usable(candidate):
            return candidate

    usable: list[Any] = []
    try:
        if context is not None:
            usable = [p for p in context.pages if _page_usable(p)]
    except Exception:
        usable = []

    if usable:
        # 用例结束时 current_page 常为最后聚焦 tab (如 click 新开的 tab), 优先回到其它仍存活的 tab
        if current_page is not None and len(usable) > 1:
            others = [p for p in usable if p is not current_page]
            if others:
                return others[0]
        return usable[0]

    if current_page is not None:
        recovered, _ = recover_active_page(
            current_page, prefer=list_anchor or primary_page,
        )
        if _page_usable(recovered):
            return recovered
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
    qs = parse_qs(urlparse(url or "").query)
    for key in ("uniqId", "workId", "orderId"):
        vals = qs.get(key) or []
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    return ""


def _url_safe(page: Any) -> str:
    """读取 URL 不依赖 evaluate, 导航中 page 可能暂时不可用."""
    try:
        return page.url or ""
    except Exception:
        return ""


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
    if norm_before and norm_now != norm_before:
        return "route_changed"
    id_before = _url_query_id(before)
    id_now = _url_query_id(now)
    if id_before and id_now and id_before != id_now:
        return "resource_id_changed"
    return None


def _body_has_submit_error(body: str) -> bool:
    return any(m in body for m in _SUBMIT_ERROR_MARKERS)


def recover_active_page(page: Any, prefer: Any = None) -> tuple[Any, bool]:
    """当前 tab 不可用/已关闭时, 切到同 context 内仍打开的 tab (可优先列表锚点)."""
    if _page_usable(page):
        return page, False
    if prefer is not None and _page_usable(prefer):
        try:
            prefer.bring_to_front()
        except Exception:
            pass
        return prefer, True
    ctx = _context_from_any(page, prefer)
    if ctx is None:
        return page, False
    for p in reversed(ctx.pages):
        if _page_usable(p):
            try:
                p.bring_to_front()
            except Exception:
                pass
            return p, True
    return page, False


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


def _is_list_page_url(url: str) -> bool:
    u = (url or "").lower()
    return "/wait-preview" in u or "/all-question" in u


def wait_and_recover_active_page(
    page: Any, *, poll_ms: int = 200, max_polls: int = 25, prefer: Any = None,
) -> tuple[Any, bool]:
    """提交/跳转后 tab 可能异步关闭, 轮询直到落到仍存活的 page."""
    recovered = False
    cur = page
    ctx = _context_from_any(page, prefer)
    for _ in range(max_polls):
        cur, changed = recover_active_page(cur, prefer=prefer)
        if changed:
            recovered = True
        if _page_usable(cur):
            return cur, recovered
        # 勿在已关闭 tab 上 wait_for_timeout (会立刻抛错中断轮询)
        if ctx is not None:
            try:
                new_page = ctx.wait_for_event("page", timeout=poll_ms)
                if _page_usable(new_page):
                    try:
                        new_page.bring_to_front()
                    except Exception:
                        pass
                    return new_page, True
            except Exception:
                pass
            for p in ctx.pages:
                if _page_usable(p):
                    try:
                        p.wait_for_timeout(poll_ms)
                    except Exception:
                        time.sleep(poll_ms / 1000.0)
                    break
            else:
                time.sleep(poll_ms / 1000.0)
        else:
            time.sleep(poll_ms / 1000.0)
    cur, changed = recover_active_page(cur, prefer=prefer)
    if changed:
        recovered = True
    return cur, recovered


def _reload_list_page(page: Any, *, timeout_ms: int = 15000) -> None:
    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass


def wait_after_detail_submit(
    page: Any,
    *,
    list_anchor: Any = None,
    url_before: str = "",
    poll_ms: int = 200,
    max_polls: int = 50,
) -> tuple[Any, str, bool]:
    """提交后等待页面结局: 列表锚点 / 路由变化 / 资源 ID 变化 / 失败提示."""
    recovered = False
    cur = page
    list_url = _url_safe(list_anchor) if list_anchor is not None else ""

    def _finish(outcome: str, target: Any) -> tuple[Any, str, bool]:
        if outcome == "returned_to_list" and _page_usable(target):
            _reload_list_page(target)
        return target, outcome, recovered

    def _poll_outcome(target: Any) -> Optional[str]:
        return classify_navigation_outcome(
            url_before, _url_safe(target), list_url=list_url,
        )

    for _ in range(max_polls):
        url_outcome = _poll_outcome(cur)
        if url_outcome in ("resource_id_changed", "returned_to_list", "route_changed"):
            return _finish(url_outcome, cur)

        if not _page_usable(cur):
            cur, changed = recover_active_page(cur, prefer=list_anchor)
            if changed:
                recovered = True
            url_outcome = _poll_outcome(cur)
            if url_outcome in ("resource_id_changed", "returned_to_list", "route_changed"):
                return _finish(url_outcome, cur)
        else:
            body = _read_body_safe(cur)
            if _body_has_submit_error(body):
                return cur, "submit_error", recovered

        if list_anchor is not None and _page_usable(list_anchor):
            if _url_query_id(url_before) and not _page_usable(cur):
                try:
                    list_anchor.bring_to_front()
                except Exception:
                    pass
                recovered = True
                return _finish("returned_to_list", list_anchor)

        if ctx_sleep := (cur.context if _page_alive(cur) else None):
            try:
                for p in ctx_sleep.pages:
                    if _page_usable(p):
                        try:
                            p.wait_for_timeout(poll_ms)
                        except Exception:
                            time.sleep(poll_ms / 1000.0)
                        break
                else:
                    time.sleep(poll_ms / 1000.0)
            except Exception:
                time.sleep(poll_ms / 1000.0)
        else:
            time.sleep(poll_ms / 1000.0)

    cur, changed = recover_active_page(cur, prefer=list_anchor)
    if changed:
        recovered = True
    url_outcome = _poll_outcome(cur)
    if url_outcome:
        return _finish(url_outcome, cur)
    if _page_usable(cur):
        body = _read_body_safe(cur)
        if _body_has_submit_error(body):
            return cur, "submit_error", recovered
        return cur, "settled", recovered
    if list_anchor is not None and _page_usable(list_anchor):
        return _finish("returned_to_list", list_anchor)
    return cur, "timeout", recovered


def wait_before_assert(
    page: Any,
    quiet_ms: int = 300,
    timeout_ms: int = 3000,
    list_anchor: Any = None,
) -> Any:
    """断言前切到存活 tab 并等待页面稳定; 返回可能已切换的 page."""
    page, _ = wait_and_recover_active_page(page, max_polls=30, prefer=list_anchor)
    if not _page_usable(page) and list_anchor is not None and _page_usable(list_anchor):
        page = list_anchor
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

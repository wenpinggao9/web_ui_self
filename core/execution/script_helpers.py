"""供 run.py 与 codegen 脚本共用的页面辅助 (避免逻辑重复)."""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

_EMPTY_ROW_MARKERS = ("暂无数据", "无数据", "No data", "no data")


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


def _page_usable(page: Any) -> bool:
    """is_closed 为 False 时 page 仍可能已不可用, 需轻量探测."""
    if not _page_alive(page):
        return False
    try:
        page.evaluate("() => true", timeout=1500)
        return True
    except Exception:
        return False


def recover_active_page(page: Any) -> tuple[Any, bool]:
    """当前 tab 不可用/已关闭时, 切到同 context 内仍打开的 tab."""
    if _page_usable(page):
        return page, False
    try:
        ctx = page.context
    except Exception:
        return page, False
    for p in reversed(ctx.pages):
        if _page_usable(p):
            try:
                p.bring_to_front()
            except Exception:
                pass
            return p, True
    return page, False


def wait_and_recover_active_page(
    page: Any, *, poll_ms: int = 200, max_polls: int = 25,
) -> tuple[Any, bool]:
    """提交/跳转后 tab 可能异步关闭, 轮询直到落到仍存活的 page."""
    recovered = False
    cur = page
    ctx = None
    try:
        ctx = page.context
    except Exception:
        pass
    for _ in range(max_polls):
        cur, changed = recover_active_page(cur)
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
    cur, changed = recover_active_page(cur)
    if changed:
        recovered = True
    return cur, recovered


def wait_before_assert(page: Any, quiet_ms: int = 300, timeout_ms: int = 3000) -> Any:
    """断言前切到存活 tab 并等待页面稳定; 返回可能已切换的 page."""
    page, _ = wait_and_recover_active_page(page)
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

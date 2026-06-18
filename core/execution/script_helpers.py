"""供 run.py 与 codegen 脚本共用的页面辅助 (避免逻辑重复)."""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

_EMPTY_ROW_MARKERS = ("暂无数据", "无数据", "No data", "no data")
_SUBMIT_ERROR_MARKERS = ("任务已处理", "请勿重复提交", "不可重复提交")


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
    page: Any, *, poll_ms: int = 200, max_polls: int = 25, prefer: Any = None,
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
    """详情页提交后等待结局: 关 tab 回列表 / 同 tab 下一题 / 提交失败提示."""
    uniq_before = _url_query_id(url_before)
    recovered = False
    cur = page

    for _ in range(max_polls):
        if not _page_usable(cur):
            cur, changed = recover_active_page(cur, prefer=list_anchor)
            if changed:
                recovered = True
        else:
            body = _read_body_safe(cur)
            if _body_has_submit_error(body):
                return cur, "submit_error", recovered

            url_now = (cur.url or "").lower()
            if "/detail" in url_now:
                uniq_now = _url_query_id(url_now)
                if uniq_before and uniq_now and uniq_now != uniq_before:
                    return cur, "next_detail", recovered
            elif "/wait-preview" in url_now:
                _reload_list_page(cur)
                return cur, "returned_to_list", recovered

        if list_anchor is not None and _page_usable(list_anchor):
            detail_gone = not _page_usable(cur) or "/detail" not in ((cur.url or "").lower())
            was_detail = "/detail" in (url_before or "").lower()
            if was_detail and detail_gone:
                try:
                    list_anchor.bring_to_front()
                except Exception:
                    pass
                _reload_list_page(list_anchor)
                return list_anchor, "returned_to_list", True

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
    if _page_usable(cur):
        body = _read_body_safe(cur)
        if _body_has_submit_error(body):
            return cur, "submit_error", recovered
        return cur, "settled", recovered
    if list_anchor is not None and _page_usable(list_anchor):
        _reload_list_page(list_anchor)
        return list_anchor, "returned_to_list", True
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

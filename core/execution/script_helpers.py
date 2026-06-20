"""供 run.py 与 codegen 脚本共用的页面辅助 (避免逻辑重复)."""
from __future__ import annotations

import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from ..locating.normalize import normalize_url

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


def is_table_row_click_intent(intent: str) -> bool:
    """表格行内按钮 (对应/该行/工单等), 非侧栏/筛选区."""
    text = intent or ""
    if "点击" not in text:
        return False
    if any(w in text for w in ("侧栏", "菜单", "下拉", "筛选区")):
        return False
    markers = (
        "对应", "该行", "此行", "列表中", "某行", "工单", "订单", "记录", "行内",
        "第一个", "第一行", "首行", "首条", "任务的",
    )
    return any(m in text for m in markers)


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
        if not row_hint:
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
        if re.search(r"第一个|第一行|首行|首条", text):
            row_hint = FIRST_TABLE_ROW_KEY
        elif is_table_row_click_intent(text):
            row_hint = FIRST_TABLE_ROW_KEY
        else:
            return None
    return button, row_hint, status_hint


def _button_label_variants(label: str) -> list[str]:
    """UI 按钮文案可能与用例不一致, 如「查看」vs「查 看」."""
    out: list[str] = []
    for v in (label, label.replace("\u00a0", " "), label.replace(" ", "")):
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    collapsed = label.replace(" ", "").replace("\u00a0", "")
    if collapsed in ("查看",) or label in ("查看", "查 看", "查\u00a0看"):
        for v in ("查看", "查 看"):
            if v not in out:
                out.append(v)
    return out


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
    try:
        from ..dom.semantic_dom import wait_for_dom_stable

        wait_for_dom_stable(page, quiet_ms=300, timeout_ms=min(timeout_ms, 8000))
    except Exception:
        try:
            page.wait_for_timeout(500)
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

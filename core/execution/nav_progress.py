"""提交/导航进度推断 —— 框架级信号 (URL 模板 / 实体参数 / DOM), 无业务字段假设."""
from __future__ import annotations

from typing import Any, Optional

from .entity_discover import (
    canonical_url_entity_map,
    discover_page_entity,
    pick_primary_url_id,
    url_entity_maps_differ,
)

_NAV_SUCCESS = frozenset({
    "resource_id_changed", "returned_to_list", "route_changed",
})

# 与 entity_discover._ID_QUERY_KEY_RE 对齐, 供浏览器端 wait_for_function 使用
_ENTITY_KEY_JS = r"/^(?:uniq|work|order|task|entity|record)?id$|_id$/i"
_LOOKS_LIKE_ID_JS = r"/^\d+$/.test(v) || (v.length >= 4 && /\d/.test(v))"


def try_wait_url_entity_change(
    page: Any,
    url_before: str,
    *,
    timeout_ms: int = 2000,
    classify_fn: Any,
    url_safe_fn: Any,
    page_usable_fn: Any,
) -> Optional[str]:
    """Playwright 监听 URL 中任意实体型查询参数变化 (键名由正则识别, 非硬编码列表)."""
    before_map = canonical_url_entity_map(url_before)
    if not before_map or not page_usable_fn(page):
        return None
    try:
        page.wait_for_function(
            f"""(beforeEntities) => {{
                const looksLikeId = (v) => {_LOOKS_LIKE_ID_JS};
                const idKeyRe = {_ENTITY_KEY_JS};
                const u = new URL(location.href);
                for (const [key, val] of u.searchParams) {{
                    if (!looksLikeId(val)) continue;
                    if (!idKeyRe.test(key) && key.toLowerCase() !== 'id') continue;
                    const lk = key.toLowerCase();
                    for (const [bk, bv] of Object.entries(beforeEntities)) {{
                        if (bk.toLowerCase() === lk && String(val) !== String(bv)) return true;
                    }}
                }}
                return false;
            }}""",
            arg=before_map,
            timeout=timeout_ms,
        )
    except Exception:
        return None
    return classify_fn(url_before, url_safe_fn(page))


def detect_submit_navigation_progress(
    url_before: str,
    page: Any,
    *,
    entity_before: str = "",
    list_url: str = "",
    classify_fn: Any,
    url_safe_fn: Any,
    page_usable_fn: Any,
    read_body_fn: Any,
    api_context: Optional[dict] = None,
    check_dom: bool = False,
) -> Optional[str]:
    """综合 URL 路由/实体参数/DOM 推断提交后是否已推进."""
    url_now = url_safe_fn(page)
    out = classify_fn(url_before, url_now, list_url=list_url)
    if out in _NAV_SUCCESS:
        return out
    if url_entity_maps_differ(url_before, url_now):
        return "resource_id_changed"
    if not check_dom or not entity_before or not page_usable_fn(page):
        return None
    flat = (read_body_fn(page) or "")[:4000]
    if not flat.strip():
        return None
    ctx = api_context if api_context is not None else {}
    eid_after, _ = discover_page_entity(ctx, url=url_now, flat_text=flat)
    if eid_after and eid_after != entity_before:
        return "resource_id_changed"
    return None


def capture_submit_entity_before(url_before: str) -> str:
    """提交前记录主实体 ID, 供后续 DOM/URL 对比."""
    return pick_primary_url_id(url_before)[0]

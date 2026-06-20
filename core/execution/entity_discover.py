"""实体 ID 自动发现 —— 框架从 URL / DOM / 会话账本推断, 无需业务配置字段名."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

_KNOWN_URL_ID_KEYS = (
    "uniqId", "workId", "orderId", "taskId", "entityId", "recordId", "id",
)
_ID_QUERY_KEY_RE = re.compile(
    r"^(?:uniq|work|order|task|entity|record)?[Ii]d$|_id$",
)
_DOM_LABEL_ID_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_]*)\s*ID[：:\s]*(\d+)",
    re.I,
)
_DOM_GENERIC_ID_RE = re.compile(r"(?:^|[\n\r])\s*ID[：:\s]*(\d+)", re.I | re.M)
_SKIP_CTX_KEYS = frozenset({"ops", "_ops_index"})


def _looks_like_id(val: str) -> bool:
    s = (val or "").strip()
    return bool(s) and (s.isdigit() or (len(s) >= 4 and any(c.isdigit() for c in s)))


def extract_url_entity_map(url: str) -> dict[str, str]:
    """解析 URL 查询串中所有像资源主键的参数."""
    qs = parse_qs(urlparse(url or "").query)
    out: dict[str, str] = {}
    for key, vals in qs.items():
        if not vals:
            continue
        val = str(vals[0]).strip()
        if not _looks_like_id(val):
            continue
        if key in _KNOWN_URL_ID_KEYS or _ID_QUERY_KEY_RE.match(key):
            out[key] = val
    if "uniqId" in out and "workId" not in out:
        out["workId"] = out["uniqId"]
    return out


def pick_primary_url_id(url: str) -> tuple[str, str]:
    """返回 (id, 来源键名)."""
    url_map = extract_url_entity_map(url)
    for key in _KNOWN_URL_ID_KEYS:
        if key in url_map:
            return url_map[key], key
    if url_map:
        k, v = next(iter(url_map.items()))
        return v, k
    return "", ""


def discover_dom_entity_ids(flat_text: str) -> list[tuple[str, str]]:
    """从 DOM 扁平文本提取 (标签, id) 列表."""
    text = flat_text or ""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _DOM_LABEL_ID_RE.finditer(text):
        label, eid = m.group(1).strip(), m.group(2).strip()
        if eid and eid not in seen:
            seen.add(eid)
            found.append((label, eid))
    for m in _DOM_GENERIC_ID_RE.finditer(text):
        eid = m.group(1).strip()
        if eid and eid not in seen:
            seen.add(eid)
            found.append(("ID", eid))
    return found


def _ops_keys(api_context: dict[str, Any]) -> list[str]:
    ops = api_context.get("ops")
    if not isinstance(ops, dict) or not ops:
        return []
    return [str(k) for k in ops if str(k).strip()]


def _context_scalar_ids(api_context: dict[str, Any]) -> list[tuple[str, str]]:
    """api_context 中的数字型标量变量 (orderId1, workId 等)."""
    out: list[tuple[str, str]] = []
    for key, val in api_context.items():
        if key.startswith("_") or key in _SKIP_CTX_KEYS:
            continue
        if val is None:
            continue
        sv = str(val).strip()
        if _looks_like_id(sv):
            out.append((str(key), sv))
    return out


def discover_active_entity(
    api_context: dict[str, Any],
    *,
    url: str = "",
    flat_text: str = "",
) -> tuple[str, str]:
    """推断当前页面对应的实体 ID (提交前 / bind 后).

    优先级: URL↔ops 对齐 → URL → 最近 bind → DOM → ops 单键 → 上下文标量.
    """
    url_id, url_key = pick_primary_url_id(url)
    ops_keys = _ops_keys(api_context)
    last_entity = str(api_context.get("_last_entity_id") or "").strip()

    if url_id and url_id in ops_keys:
        return url_id, f"ops+url({url_key})"
    if url_id:
        return url_id, url_key or "url"
    if last_entity:
        return last_entity, "_last_entity"
    for label, eid in discover_dom_entity_ids(flat_text):
        if eid in ops_keys:
            return eid, f"ops+dom({label})"
    dom_ids = discover_dom_entity_ids(flat_text)
    if dom_ids:
        return dom_ids[0][1], f"dom({dom_ids[0][0]})"
    if len(ops_keys) == 1:
        return ops_keys[0], "ops"
    if last_entity and last_entity in ops_keys:
        return last_entity, "ops"
    for key, sv in _context_scalar_ids(api_context):
        if url_id and sv == url_id:
            return sv, key
    if ops_keys:
        return ops_keys[-1], "ops(recent)"
    for key, sv in _context_scalar_ids(api_context):
        return sv, key
    return "", ""


def discover_page_entity(
    api_context: dict[str, Any],
    *,
    url: str = "",
    flat_text: str = "",
) -> tuple[str, str]:
    """推断页面上当前展示的实体 ID (提交后). URL 优先, 其次 DOM."""
    url_id, url_key = pick_primary_url_id(url)
    if url_id:
        return url_id, url_key or "url"
    for label, eid in discover_dom_entity_ids(flat_text):
        return eid, f"dom({label})"
    return discover_active_entity(api_context, url=url, flat_text=flat_text)


def summarize_recorded_context(
    api_context: dict[str, Any],
    entity_id: str,
) -> dict[str, Any]:
    """提取与当前实体相关的会话记录摘要 (自动, 不读业务配置)."""
    rec: dict[str, Any] = {}
    if entity_id:
        ops = api_context.get("ops")
        if isinstance(ops, dict) and entity_id in ops:
            rec["ops_entry"] = ops[entity_id]
    for key, val in _context_scalar_ids(api_context):
        if entity_id and str(val) == entity_id:
            rec[key] = val
    last = api_context.get("_last_entity_id")
    if last:
        rec["_last_entity_id"] = last
    return rec

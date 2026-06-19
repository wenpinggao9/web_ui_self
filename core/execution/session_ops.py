"""跨用例会话操作账本 —— ops[实体ID] = { 本次操作字段 }.

框架只做通用解析链; 字段名、API、URL 捕获、resolve 文案均在业务知识 session_ops / page_capture 中配置.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from .script_helpers import extract_url_query

_DEFAULT_SESSION_OPS: dict[str, Any] = {
    "ops_key_field": "id",
    "table_row_field": "",
    "entity_id": {"sources": ["extras", "context", "api"]},
    "resolve": {},
    "fields": {},
    "index_by": [],
}


def get_ops_index(ctx: dict[str, Any]) -> dict[str, dict[str, str]]:
    """字段值 → 实体 ID 反向索引, 如 reason → orderId."""
    idx = ctx.get("_ops_index")
    if not isinstance(idx, dict):
        idx = {}
        ctx["_ops_index"] = idx
    return idx


def _update_ops_index(
    ctx: dict[str, Any],
    entity_id: str,
    entry: dict[str, Any],
    index_by: list[str],
) -> None:
    if not index_by:
        return
    bucket = get_ops_index(ctx)
    for field in index_by:
        raw = entry.get(field)
        if raw is None:
            continue
        val = str(raw).strip()
        if not val:
            continue
        bucket.setdefault(str(field), {})[val] = str(entity_id)


def record_op(
    ctx: dict[str, Any],
    entity_id: str,
    fields: dict[str, Any],
    *,
    index_by: Optional[list[str]] = None,
) -> dict[str, Any]:
    """写入 ops[entity_id], 合并已有条目, 并按 index_by 更新反向索引."""
    key = str(entity_id or "").strip()
    if not key:
        raise ValueError("缺少实体 ID, 无法写入 ops")
    ops = get_ops_bucket(ctx)
    prev = dict(ops.get(key, {}))
    entry = dict(prev)
    entry.update({k: v for k, v in fields.items() if v is not None and v != ""})
    ops[key] = entry
    if index_by:
        _update_ops_index(ctx, key, entry, index_by)
    return entry


def get_ops_bucket(ctx: dict[str, Any]) -> dict[str, Any]:
    ops = ctx.get("ops")
    if not isinstance(ops, dict):
        ops = {}
        ctx["ops"] = ops
    return ops


def _entry_table_row_value(entry: Any, table_row_field: str) -> Optional[str]:
    if not isinstance(entry, dict) or not table_row_field:
        return None
    val = entry.get(table_row_field)
    if val is None or str(val).strip() == "":
        return None
    return str(val).strip()


def resolve_table_row_key(
    row_key: str,
    ctx: dict[str, Any],
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> tuple[str, Optional[str]]:
    """将 assert_table 的行标识解析为表格主键列的值 (如 reason → orderId).

    ops 主键 (ops_key_field) 与表格行键 (table_row_field) 可不同, 如 workId vs orderId.
    """
    key = (row_key or "").strip()
    if not key or not ctx:
        return key, None

    cfg = _merge_cfg(session_ops_cfg)
    ops_key_field = str(cfg.get("ops_key_field") or "id").strip()
    table_row_field = str(cfg.get("table_row_field") or ops_key_field).strip()
    ops = get_ops_bucket(ctx)

    if key in ops:
        entry = ops[key]
        row_val = _entry_table_row_value(entry, table_row_field)
        if row_val and table_row_field != ops_key_field:
            return row_val, f"ops[{key}].{table_row_field}={row_val}"
        return key, None

    if ctx.get(table_row_field) is not None and str(ctx.get(table_row_field)).strip() == key:
        return key, None

    index_by = cfg.get("index_by") or []
    if isinstance(index_by, str):
        index_by = [index_by]

    idx_root = ctx.get("_ops_index") or {}
    for field in index_by:
        entity_id = (idx_root.get(str(field)) or {}).get(key)
        if not entity_id:
            continue
        entry = ops.get(str(entity_id))
        row_val = _entry_table_row_value(entry, table_row_field)
        if row_val:
            return row_val, f"ops[{field}]→{table_row_field}={row_val}"

    for entity_id, entry in ops.items():
        if not isinstance(entry, dict):
            continue
        for field in index_by:
            ev = str(entry.get(field) or "").strip()
            if ev and ev == key:
                row_val = _entry_table_row_value(entry, table_row_field)
                if row_val:
                    return row_val, f"ops[{entity_id}].{field}→{table_row_field}={row_val}"

    return key, None


def capture_from_page(page: Any, page_capture: Optional[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not page_capture:
        return out
    for name, spec in page_capture.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("from") != "url_query":
            continue
        qkey = str(spec.get("key") or name)
        found = extract_url_query(page, qkey)
        val = found.get(qkey)
        if val:
            out[str(name)] = val
    return out


def _merge_cfg(overrides: Optional[dict[str, Any]]) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "ops_key_field": _DEFAULT_SESSION_OPS["ops_key_field"],
        "table_row_field": _DEFAULT_SESSION_OPS["table_row_field"],
        "entity_id": dict(_DEFAULT_SESSION_OPS["entity_id"]),
        "resolve": dict(_DEFAULT_SESSION_OPS["resolve"]),
        "fields": dict(_DEFAULT_SESSION_OPS["fields"]),
        "index_by": list(_DEFAULT_SESSION_OPS["index_by"]),
    }
    if not overrides:
        return cfg
    if overrides.get("ops_key_field"):
        cfg["ops_key_field"] = overrides["ops_key_field"]
    if overrides.get("table_row_field"):
        cfg["table_row_field"] = overrides["table_row_field"]
    if isinstance(overrides.get("entity_id"), dict):
        cfg["entity_id"].update(overrides["entity_id"])
    if isinstance(overrides.get("resolve"), dict):
        cfg["resolve"].update(overrides["resolve"])
    if isinstance(overrides.get("fields"), dict):
        cfg["fields"].update(overrides["fields"])
    if overrides.get("index_by"):
        raw = overrides["index_by"]
        cfg["index_by"] = [raw] if isinstance(raw, str) else list(raw)
    # 兼容旧配置顶层的 resolve_intent
    if overrides.get("resolve_intent"):
        cfg["resolve"]["intent"] = overrides["resolve_intent"]
    return cfg


def _inject_page_query(page: Any, *targets: dict[str, Any]) -> None:
    """把 URL 查询参数写入多个 dict (不覆盖已有键)."""
    found = extract_url_query(page)
    for tgt in targets:
        for k, v in found.items():
            if v:
                tgt.setdefault(k, v)


def _entity_sources(cfg: dict[str, Any]) -> list[str]:
    raw = (cfg.get("entity_id") or {}).get("sources")
    if isinstance(raw, list) and raw:
        return [str(s) for s in raw]
    return list(_DEFAULT_SESSION_OPS["entity_id"]["sources"])


def pick_entity_from_context(ctx: dict[str, Any], key_field: str) -> Optional[str]:
    """从会话变量池读取 ops 主键 (含单一编号变体如 id1/id2)."""
    val = ctx.get(key_field)
    if val is not None and str(val).strip():
        return str(val).strip()
    m = re.match(r"^([a-zA-Z]+)", key_field)
    if not m:
        return None
    prefix = m.group(1)
    numbered = [
        (k, v) for k, v in ctx.items()
        if k != "ops" and isinstance(v, (str, int))
        and k.startswith(prefix) and re.search(r"\d$", k)
    ]
    if len(numbered) == 1:
        return str(numbered[0][1]).strip()
    return None


def _refresh_row_fields_via_api(
    api_runner: Any,
    api_context: dict[str, Any],
    captured: dict[str, str],
    cfg: dict[str, Any],
    entity_id: str,
) -> None:
    """bind_session 前按当前实体 ID 刷新 table_row_field (如 orderId), 避免复用旧上下文."""
    resolve_intent = str((cfg.get("resolve") or {}).get("intent") or "").strip()
    table_row_field = str(cfg.get("table_row_field") or "").strip()
    if not resolve_intent or not table_row_field or api_runner is None:
        return
    fields_cfg = cfg.get("fields") or {}
    needs = any(
        isinstance(spec, dict)
        and spec.get("from") == "context"
        and str(spec.get("key") or name) == table_row_field
        for name, spec in fields_cfg.items()
    )
    if not needs:
        return
    key_field = str(cfg.get("ops_key_field") or "id").strip()
    api_runner.context.update(api_context)
    api_runner.context.update(captured)
    if entity_id:
        api_runner.context[key_field] = entity_id
    api_runner.run_preconditions([resolve_intent])
    api_context.update(api_runner.context)


def _resolve_entity_via_api(
    api_runner: Any,
    api_context: dict[str, Any],
    captured: dict[str, str],
    *,
    key_field: str,
    resolve_intent: str,
) -> Optional[str]:
    if not resolve_intent.strip():
        return None
    runner_before = dict(api_runner.context)
    api_runner.context.update(api_context)
    api_runner.context.update(captured)
    api_runner.run_preconditions([resolve_intent.strip()])
    api_context.update(api_runner.context)
    if key_field in api_runner.context and api_runner.context.get(key_field) != runner_before.get(key_field):
        return str(api_runner.context[key_field]).strip()
    val = api_runner.context.get(key_field)
    if val is not None and str(val).strip():
        return str(val).strip()
    return None


def _build_fields(
    field_cfg: dict[str, Any],
    *,
    captured: dict[str, str],
    case_id: str,
    prev_click: Optional[str],
    extras_fields: Optional[dict[str, Any]],
    api_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    ctx = api_context or {}
    if isinstance(extras_fields, dict):
        out.update(extras_fields)
    for name, spec in (field_cfg or {}).items():
        if not isinstance(spec, dict):
            continue
        if name in out and out[name] not in (None, ""):
            continue
        src = spec.get("from")
        if src == "captured":
            key = str(spec.get("key") or name)
            if captured.get(key):
                out[name] = captured[key]
        elif src == "context":
            key = str(spec.get("key") or name)
            val = ctx.get(key)
            if val is not None and str(val).strip():
                out[name] = str(val).strip()
        elif src == "prev_click" and prev_click:
            out[name] = prev_click
        elif src == "case_id" and case_id:
            out[name] = case_id
        elif src == "literal" and spec.get("value") is not None:
            out[name] = spec.get("value")
    return out


def resolve_entity_id(
    page: Any,
    api_context: dict[str, Any],
    *,
    api_runner: Any,
    key_field: str,
    cfg: dict[str, Any],
    extras: Optional[dict[str, Any]] = None,
    intent: str = "",
    page_capture: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """按 entity_id.sources 顺序解析实体 ID."""
    ex = extras or {}
    captured = capture_from_page(page, page_capture)
    _inject_page_query(page, api_context, captured)

    if key_field in captured and str(captured[key_field]).strip():
        return str(captured[key_field]).strip()

    resolve_cfg = cfg.get("resolve") if isinstance(cfg.get("resolve"), dict) else {}
    resolve_intent = str(
        ex.get("resolve_intent") or resolve_cfg.get("intent") or ""
    ).strip()

    for source in _entity_sources(cfg):
        if source == "extras":
            eid = str(ex.get("entity_id") or "").strip()
            if eid:
                return eid
        elif source == "context":
            eid = pick_entity_from_context(api_context, key_field)
            if eid:
                return eid
        elif source == "api" and api_runner is not None:
            api_intent = resolve_intent or (intent or "").strip()
            eid = _resolve_entity_via_api(
                api_runner, api_context, captured,
                key_field=key_field, resolve_intent=api_intent,
            )
            if eid:
                return eid
    return None


def execute_bind_session(
    page: Any,
    api_context: dict[str, Any],
    *,
    api_runner: Any,
    case_id: str = "",
    prev_click: Optional[str] = None,
    intent: str = "",
    extras: Optional[dict[str, Any]] = None,
    session_ops_cfg: Optional[dict[str, Any]] = None,
    page_capture: Optional[dict[str, Any]] = None,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    """解析实体 ID 并写入 ops. 返回 (ok, message, ops_entry)."""
    cfg = _merge_cfg(session_ops_cfg)
    ex = extras or {}
    captured = capture_from_page(page, page_capture)
    key_field = str(ex.get("ops_key_field") or cfg.get("ops_key_field") or "id")

    entity_id = resolve_entity_id(
        page,
        api_context,
        api_runner=api_runner,
        key_field=key_field,
        cfg=cfg,
        extras=ex,
        intent=intent,
        page_capture=page_capture,
    )

    if not entity_id:
        return False, (
            f"bind_session 失败: 未能解析实体 ID (key={key_field!r}, "
            f"sources={_entity_sources(cfg)!r})"
        ), None

    _refresh_row_fields_via_api(
        api_runner, api_context, captured, cfg, entity_id,
    )

    var_name = _match_session_var(api_context, entity_id)
    fields = _build_fields(
        cfg.get("fields") or {},
        captured=captured,
        case_id=case_id,
        prev_click=prev_click,
        extras_fields=ex.get("fields") if isinstance(ex.get("fields"), dict) else None,
        api_context=api_context,
    )
    if var_name:
        fields.setdefault("var", var_name)

    entry = record_op(
        api_context,
        entity_id,
        fields,
        index_by=cfg.get("index_by") or None,
    )
    msg = f"会话记录 ops[{entity_id}] = {json.dumps(entry, ensure_ascii=False)}"
    return True, msg, entry


def _match_session_var(ctx: dict[str, Any], entity_id: str) -> Optional[str]:
    """若实体 ID 与某会话标量变量一致, 记录 var 名."""
    for k, v in ctx.items():
        if k == "ops" or not isinstance(v, (str, int)):
            continue
        if str(v) == str(entity_id) and re.match(r"^[a-zA-Z]+\d*$", k):
            return k
    return None

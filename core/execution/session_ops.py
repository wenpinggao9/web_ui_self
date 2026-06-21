"""跨用例会话操作账本 —— ops[实体ID] = { 字段 }.

可选能力: 业务可在 action.extras 传入 ops 字段; 框架默认不预设字段名或索引规则.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from .script_helpers import FIRST_TABLE_ROW_KEY, extract_url_query

_DEFAULT_SESSION_OPS: dict[str, Any] = {
    "ops_key_field": "id",
    "table_row_field": "",
    "table_row_key_column": "",
    "status_column": "",
    "entity_id": {"sources": ["extras", "context", "api"]},
    "resolve": {},
    "fields": {},
    "index_by": [],
}


def get_ops_index(ctx: dict[str, Any]) -> dict[str, dict[str, str]]:
    """字段值 → 实体 ID 反向索引 (仅 bind_session + index_by 时使用)."""
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
    ctx["_last_entity_id"] = key
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


def _extract_label_from_intent(intent: str) -> Optional[str]:
    """从 bind/记录类 intent 的括号或引号中提取标签文案."""
    text = intent or ""
    if not text:
        return None
    paren = re.findall(r"[（(]([^）)]+)[）)]", text)
    if paren:
        return paren[-1].strip() or None
    quoted = re.findall(
        r"[\"'“”‘’「」『』]([^\"'“”‘’「」『』]+)[\"'“”‘’「」『』]",
        text,
    )
    if quoted:
        return quoted[-1].strip() or None
    return None


def table_row_key_matches(cell: str, row_key: str) -> bool:
    """行主键列与 row_key 精确匹配 (整格或 token 相等, 禁止子串误匹配)."""
    key = (row_key or "").strip()
    if not key:
        return False
    cell = (cell or "").strip()
    if not cell:
        return False
    if cell == key:
        return True
    tokens = [t.strip() for t in re.split(r"[\s,;|/、]+", cell) if t.strip()]
    return key in tokens


def _ops_resolve_hint(
    row_key: str,
    ctx: dict[str, Any],
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> str:
    """assert_table 未命中时, 列出 ops 反向索引里已有的 key."""
    cfg = _merge_cfg(session_ops_cfg)
    index_by = cfg.get("index_by") or []
    if isinstance(index_by, str):
        index_by = [index_by]
    idx_root = ctx.get("_ops_index") or {}
    parts: list[str] = []
    for field in index_by:
        bucket = idx_root.get(str(field)) or {}
        if bucket:
            parts.append(f"{field}→{list(bucket.keys())[:8]}")
    if parts:
        return f"会话 ops 索引已有: {'; '.join(parts)}"
    return f"会话 ops 中无 {row_key!r} 的索引, 需先 bind_session 记录"


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

    # reason 等索引键允许子串互含 (如用例写「不良导向」、ops 存全文)
    for field in index_by:
        bucket = idx_root.get(str(field)) or {}
        if not isinstance(bucket, dict):
            continue
        for indexed_val, entity_id in bucket.items():
            iv = str(indexed_val).strip()
            if not iv or (key != iv and key not in iv and iv not in key):
                continue
            entry = ops.get(str(entity_id))
            row_val = _entry_table_row_value(entry, table_row_field)
            if row_val:
                return row_val, f"ops[{field}]~{iv!r}→{table_row_field}={row_val}"

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


def _effective_table_row_field(cfg: dict[str, Any]) -> str:
    ops_key = str(cfg.get("ops_key_field") or "id").strip()
    return str(cfg.get("table_row_field") or ops_key).strip() or ops_key


def extract_row_key_from_extras(
    extras: Optional[dict[str, Any]],
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> str:
    """读取规划 extras 里直接给出的行 ID (row_key / table_row_field / 列名键)."""
    ex = extras or {}
    cfg = _merge_cfg(session_ops_cfg)
    for name in (
        _effective_table_row_field(cfg),
        str(cfg.get("table_row_key_column") or "").strip(),
        "row_key",
    ):
        if name and ex.get(name) is not None and str(ex[name]).strip():
            return str(ex[name]).strip()
    return ""


def explicit_row_keys_from_action(
    extras: Optional[dict[str, Any]],
    api_context: dict[str, Any],
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> list[tuple[str, Optional[str]]]:
    """规划已给出目标行 ID → 直接定位, 不经 ops 二次转换."""
    ex = extras or {}
    direct = extract_row_key_from_extras(ex, session_ops_cfg)
    if not direct:
        return []
    if direct == FIRST_TABLE_ROW_KEY:
        return [(FIRST_TABLE_ROW_KEY, "extras.first_row")]

    cfg = _merge_cfg(session_ops_cfg)
    table_field = _effective_table_row_field(cfg)
    if ex.get(table_field) and str(ex[table_field]).strip() == direct:
        return [(direct, f"extras.{table_field}")]
    if ex.get("row_key") and str(ex["row_key"]).strip() == direct:
        src = str(ex.get("row_key_source") or "").strip()
        return [(direct, src or "extras.row_key")]

    ops = get_ops_bucket(api_context)
    if direct in ops:
        entry = ops[direct]
        row_val = _entry_table_row_value(entry, table_field)
        if row_val and row_val != direct:
            return [(
                row_val,
                f"ops[{direct}].{table_field}={row_val}",
            )]

    if direct.isdigit() or len(direct) >= 4:
        return [(direct, "extras直接指定")]
    return []


def resolve_click_row_candidates(
    row_hint: str,
    ctx: dict[str, Any],
    session_ops_cfg: Optional[dict[str, Any]] = None,
    *,
    status_hint: str = "",
) -> list[tuple[str, Optional[str]]]:
    """行内按钮: 将行提示 (index_by 字段值 / 数字 ID) 解析为表格主键列候选值."""
    key = (row_hint or "").strip()
    cfg = _merge_cfg(session_ops_cfg)
    table_field = str(cfg.get("table_row_field") or "orderId").strip()

    if key == FIRST_TABLE_ROW_KEY:
        return [(FIRST_TABLE_ROW_KEY, "first_table_row")]

    if key:
        resolved, hint = resolve_table_row_key(key, ctx, session_ops_cfg)
        if hint:
            return [(resolved, hint)]
        if resolved.isdigit():
            return [(resolved, hint)]

    # ops 索引未命中且带状态过滤时, 枚举会话内 orderId, 由表格 status_column 二次筛选
    if status_hint:
        ops = get_ops_bucket(ctx)
        seen: set[str] = set()
        out: list[tuple[str, Optional[str]]] = []
        for entry in ops.values():
            if not isinstance(entry, dict):
                continue
            v = str(entry.get(table_field) or "").strip()
            if v and v not in seen:
                out.append((v, f"ops.{table_field}={v}"))
                seen.add(v)
        if out:
            return out

    if key:
        return [(key, None)]
    return []


def enrich_table_row_clicks(
    actions: list[Any],
    ctx: dict[str, Any] | None,
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> list[Any]:
    """规划后补全行内 click 的 extras.row_key (索引字段 → 表格行主键), 避免仅靠 intent 无法定位."""
    if not actions or not ctx:
        return actions
    from .script_helpers import FIRST_TABLE_ROW_KEY, is_table_row_click_intent, parse_table_row_click

    for action in actions:
        if getattr(action, "type", None) != "click":
            continue
        ex = dict(getattr(action, "extras", None) or {})
        intent = getattr(action, "intent", "") or ""
        explicit_id = extract_row_key_from_extras(ex, session_ops_cfg)
        if not ex.get("row_key") and not is_table_row_click_intent(intent) and not explicit_id:
            continue
        if explicit_id and not ex.get("row_key"):
            ex["row_key"] = explicit_id
        parsed = parse_table_row_click(intent, ex)
        row_hint = ""
        status_hint: Optional[str] = None
        if parsed:
            button, row_hint, status_hint = parsed
            ex.setdefault("button", button)
            if status_hint:
                ex.setdefault("status_filter", status_hint)
        if not row_hint:
            skip = {str(ex.get("button") or ""), "日志", "查看", "编辑", "删除"}
            row_hint = extract_assert_row_hint(intent, skip=skip)
        if not ex.get("button"):
            for label in ("日志", "查看", "编辑", "删除"):
                if label in intent:
                    ex["button"] = label
                    break
        row_key = str(ex.get("row_key") or "").strip()
        explicit_cands = explicit_row_keys_from_action(ex, ctx, session_ops_cfg)
        if explicit_cands and explicit_cands[0][0]:
            ex["row_key"] = explicit_cands[0][0]
            if explicit_cands[0][1]:
                ex["row_key_source"] = explicit_cands[0][1]
        elif not row_key or not row_key.isdigit():
            hint = row_hint or row_key
            if hint:
                resolved, ops_hint = resolve_table_row_key(hint, ctx, session_ops_cfg)
                if ops_hint and resolved and str(resolved).strip().isdigit():
                    ex["row_key"] = str(resolved).strip()
        if not ex.get("row_key") and is_table_row_click_intent(intent):
            if parsed and parsed[1] == FIRST_TABLE_ROW_KEY:
                ex["row_key"] = FIRST_TABLE_ROW_KEY
            elif not row_key and ex.get("button"):
                ex["row_key"] = FIRST_TABLE_ROW_KEY
        if ex:
            action.extras = ex
    return actions


def extract_assert_row_hint(intent: str, *, skip: Optional[set[str]] = None) -> str:
    """从 assert_table intent 提取索引字段值 (如 选择「多题」→ 多题)."""
    text = (intent or "").strip()
    if not text:
        return ""
    skip = skip or set()
    for pat in (
        r"前面选择了[「'\"]([^」'\"]+)[」'\"]",
        r"选择[了]?[「'\"]([^」'\"]+)[」'\"]",
        r"前序选择[了]?[「'\"]([^」'\"]+)[」'\"]",
        r"记录[为是]?[「'\"]([^」'\"]+)[」'\"]",
    ):
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip()
            if val and val not in skip:
                return val
    for q in re.findall(r"[「'\"]([^」'\"]+)[」'\"]", text):
        q = q.strip()
        if q and q not in skip:
            return q
    return ""


def resolve_assert_table_row_key(
    action: Any,
    ctx: dict[str, Any],
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> bool:
    """将 assert_table 的 value/row_key 从索引语义或残留 ${} 解析为表格行主键值. 成功返回 True."""
    if getattr(action, "type", None) != "assert_table":
        return False
    ex = dict(getattr(action, "extras", None) or {})
    row_key = str(getattr(action, "value", None) or ex.get("row_key") or "").strip()
    if row_key.isdigit():
        return False
    skip = {str(ex.get("expected") or ""), str(ex.get("column") or "")}
    hint = row_key if row_key and not row_key.startswith("${") else ""
    if not hint or "${" in hint:
        hint = extract_assert_row_hint(getattr(action, "intent", "") or "", skip=skip)
    if not hint:
        return False
    resolved, ops_hint = resolve_table_row_key(hint, ctx, session_ops_cfg)
    if not ops_hint or not resolved or not str(resolved).strip().isdigit():
        return False
    action.value = str(resolved).strip()
    if ex.get("row_key"):
        ex["row_key"] = action.value
        action.extras = ex
    return True


def enrich_session_assertions(
    actions: list[Any],
    ctx: dict[str, Any] | None,
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> list[Any]:
    """规划后补全 assert_table 行标识 (索引字段 → 表格行主键)."""
    if not actions or not ctx:
        return actions
    for action in actions:
        resolve_assert_table_row_key(action, ctx, session_ops_cfg)
    return actions


def enrich_session_actions(
    actions: list[Any],
    ctx: dict[str, Any] | None,
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> list[Any]:
    """规划后统一补全会话相关的 click / assert_table 行主键."""
    enrich_table_row_clicks(actions, ctx, session_ops_cfg)
    enrich_session_assertions(actions, ctx, session_ops_cfg)
    return actions


def get_table_columns_cfg(
    session_ops_cfg: Optional[dict[str, Any]] = None,
) -> tuple[str, str, str]:
    """(table_row_field, table_row_key_column, status_column) 来自 session_ops 或 extras 覆盖."""
    cfg = _merge_cfg(session_ops_cfg)
    return (
        str(cfg.get("table_row_field") or "orderId").strip(),
        str(cfg.get("table_row_key_column") or "").strip(),
        str(cfg.get("status_column") or "").strip(),
    )


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
        "table_row_key_column": _DEFAULT_SESSION_OPS["table_row_key_column"],
        "status_column": _DEFAULT_SESSION_OPS["status_column"],
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
    if overrides.get("table_row_key_column"):
        cfg["table_row_key_column"] = overrides["table_row_key_column"]
    if overrides.get("status_column"):
        cfg["status_column"] = overrides["status_column"]
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
    intent: str = "",
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
        elif src == "prev_click":
            hinted = _extract_label_from_intent(intent)
            # intent 括号/引号内的文案优先于可能来自上一用例的陈旧 prev_click
            if hinted and (not prev_click or hinted in intent):
                out[name] = hinted
            elif prev_click:
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
        intent=intent,
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

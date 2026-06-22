"""变量替换工具 —— 将步骤/预期中的 ${varName} 替换为运行时值."""
from __future__ import annotations

import json
import re
from typing import Any

_API_CTX_SKIP_KEYS = frozenset({"ops", "_ops_index"})


def substitute_variables(text: str, context: dict[str, Any]) -> str:
    """将 ${varName} 替换为 context 中的值.

    例: "工单ID=${orderId1}的生产状态" + {"orderId1": "118743302"}
       → "工单ID=118743302的生产状态"
    """
    if not context:
        return text
    for k, v in context.items():
        # 只替换 ${name} 这种显式占位符, 避免误替换普通业务文本.
        placeholder = "${" + k + "}"
        if placeholder in text:
            text = text.replace(placeholder, str(v))
    return text


def find_api_var_for_value(text: str, context: dict[str, Any]) -> str | None:
    """若字面量等于 context 中某 API 标量值, 返回变量名 (优先 orderId/id 类)."""
    raw = (text or "").strip()
    if not raw or not context:
        return None
    matches: list[str] = []
    for k, v in context.items():
        if k in _API_CTX_SKIP_KEYS or isinstance(v, (dict, list)):
            continue
        if str(v) == raw:
            matches.append(k)
    if not matches:
        return None

    def _rank(key: str) -> tuple[int, str]:
        kl = key.lower()
        if "orderid" in kl or re.search(r"id\d+$", kl) or key.endswith("Id"):
            return (0, key)
        return (1, key)

    matches.sort(key=_rank)
    return matches[0]


def substitute_in_list(items: list[str], context: dict[str, Any]) -> list[str]:
    """批量替换列表中的字符串."""
    if not context:
        # 没有上下文时原样返回, 保持调用方列表对象不被无意义复制.
        return items
    return [substitute_variables(item, context) for item in items]


def format_session_context(
    ctx: dict[str, Any] | None,
    session_ops_cfg: dict[str, Any] | None = None,
) -> str:
    """将跨用例会话变量/ops 格式化为动作规划可读摘要."""
    if not ctx:
        return "(无)"
    cfg = session_ops_cfg or {}
    table_field = str(cfg.get("table_row_field") or "").strip() or "行主键"
    index_by = cfg.get("index_by") or []
    if isinstance(index_by, str):
        index_by = [index_by]

    lines: list[str] = []
    skip = frozenset({"ops", "_ops_index"})
    scalars: list[str] = []
    for k, v in sorted(ctx.items()):
        if k in skip or isinstance(v, (dict, list)):
            continue
        scalars.append(f"- {k} = {v}")
    if scalars:
        lines.append("标量变量 (步骤中可用 ${名称} 引用):")
        lines.extend(scalars)

    ops = ctx.get("ops")
    if isinstance(ops, dict) and ops:
        lines.append("ops 记录 (可选 bind_session; 一般用 api_call 标量变量即可):")
        for eid, entry in ops.items():
            if not isinstance(entry, dict):
                continue
            row_val = entry.get(table_field) or entry.get("orderId") or ""
            if index_by and row_val:
                tags = [
                    f"{f}={entry.get(f)!r}"
                    for f in index_by
                    if entry.get(f) not in (None, "")
                ]
                if tags:
                    lines.append(f"- {' | '.join(tags)} → {table_field}={row_val}")
                    continue
            lines.append(
                f"- ops[{eid}] = {json.dumps(entry, ensure_ascii=False)}"
            )
    idx = ctx.get("_ops_index")
    if isinstance(idx, dict) and idx:
        lines.append("ops 反向索引 (索引字段值 → 实体ID):")
        for field, bucket in idx.items():
            if not isinstance(bucket, dict) or not bucket:
                continue
            pairs = ", ".join(
                f"{k!r}→{v}" for k, v in list(bucket.items())[:12]
            )
            lines.append(f"- {field}: {pairs}")

    return "\n".join(lines) if lines else "(无)"

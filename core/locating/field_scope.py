"""表单项范围内从语义 DOM 解析字段关联 id（L3 dropdown_trigger 等共用）."""
from __future__ import annotations

from typing import Any, Dict, List

_COMBOBOX_SCAN_WINDOW = 25


def _is_label_node(node: Dict[str, Any]) -> bool:
    return str(node.get("tag") or "").lower() == "label"


def _is_combobox_node(node: Dict[str, Any]) -> bool:
    return str(node.get("role") or "").lower() == "combobox"


def combobox_ids_near_label(
    semantic_dom: List[Dict[str, Any]],
    label: str,
    *,
    scan_window: int = _COMBOBOX_SCAN_WINDOW,
) -> List[str]:
    """从语义 DOM 中按 label 查找本表单项内 combobox 的 id。

    扫描窗口默认 25 节点；遇下一 label 停止；只认该区间内第一个 combobox：
    有 id 则返回，无 id 则视为本字段无合格 id（不继续扫隔壁字段）。
    """
    label = (label or "").strip()
    if not label or not semantic_dom:
        return []
    ids: List[str] = []
    limit = max(1, int(scan_window))
    for i, node in enumerate(semantic_dom):
        if not _is_label_node(node):
            continue
        text = str(node.get("text") or "").strip()
        if label not in text:
            continue
        end = min(i + 1 + limit, len(semantic_dom))
        for j in range(i + 1, end):
            child = semantic_dom[j]
            if _is_label_node(child):
                break
            if not _is_combobox_node(child):
                continue
            cid = str(child.get("id") or "").strip()
            if cid:
                ids.append(cid)
            break
    return list(dict.fromkeys(ids))


__all__ = ["combobox_ids_near_label"]

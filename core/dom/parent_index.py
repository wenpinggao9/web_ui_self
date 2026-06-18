"""为语义 DOM 条目补全 parent_index (在 Python 侧重排后映射)."""
from __future__ import annotations


def attach_parent_indices(items: list[dict]) -> None:
    """根据 _parent_id 在重排后的 items 上写入 parent_index."""
    id_to_index: dict[str, int] = {}
    for i, it in enumerate(items):
        for key in ("_id", "id"):
            eid = (it.get(key) or "").strip()
            if eid:
                id_to_index[eid] = i

    for i, it in enumerate(items):
        parent_id = (it.get("_parent_id") or "").strip()
        if parent_id and parent_id in id_to_index:
            it["parent_index"] = id_to_index[parent_id]

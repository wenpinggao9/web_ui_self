"""L2 记忆库 selector_type 推断 (对齐 V3 selector_memory.put)."""
from __future__ import annotations


def infer_selector_type(
    info: dict,
    *,
    source: str = "",
    from_rule: bool = False,
    from_skill: bool = False,
) -> str:
    sel = str(info.get("selector") or "")
    if from_rule or source == "L3规则":
        return "rule"
    if from_skill or source in ("L3Skill", "L3纠偏") or info.get("_from_skill"):
        return "skill"
    if sel.startswith(("/", "(")) or sel.startswith("xpath="):
        return "xpath"
    if info.get("method") == "role":
        return "role"
    return "css"

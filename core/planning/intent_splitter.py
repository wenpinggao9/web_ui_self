"""规划后处理 —— 剥离与模块路径重复的菜单点击 (步骤⑤ 已完成导航)."""
from __future__ import annotations

import re

from .action_schema import PlannedAction


def strip_duplicate_menu_clicks(actions: list[PlannedAction], module_path: list[str]) -> list[PlannedAction]:
    """剥离动作列表开头与 module_path 重复的菜单点击."""
    if not module_path:
        return actions
    menu_terms = {m.strip() for m in module_path if m.strip()}
    out = list(actions)
    while out:
        a = out[0]
        if a.type == "click" and any(_contains_term(a.intent, term) for term in menu_terms):
            out.pop(0)
            continue
        break
    return out


def _contains_term(intent: str, term: str) -> bool:
    cleaned = re.sub(r"[\"'“”‘’「」『』]", "", intent)
    return term in cleaned

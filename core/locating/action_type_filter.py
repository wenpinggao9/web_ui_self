"""按 action_type 预过滤语义 DOM 候选."""
from __future__ import annotations

from typing import Any, Optional


def filter_items_by_action_type(
    items: list[dict],
    action_type: Optional[str],
) -> list[dict]:
    """返回仍保留原始下标的 items 子集."""
    if not action_type or not items:
        return items
    at = action_type.lower()
    out: list[dict] = []
    for it in items:
        if _item_matches_action_type(it, at):
            out.append(it)
    return out if out else items


def item_matches_action_type(item: dict, action_type: Optional[str]) -> bool:
    if not action_type:
        return True
    return _item_matches_action_type(item, action_type.lower())


def _item_matches_action_type(element: dict, action_type_lower: str) -> bool:
    tag = (element.get("tag") or "").lower()
    role = (element.get("role") or "").lower()

    if action_type_lower == "fill":
        if tag in ("input", "textarea"):
            input_type = (element.get("type") or "text").lower()
            if tag == "input" and input_type in (
                "button", "submit", "reset", "checkbox", "radio", "file", "image", "hidden",
            ):
                return False
            if tag == "input" and element.get("readOnly"):
                return False
            return True
        return role == "textbox" or bool(element.get("contenteditable"))

    if action_type_lower == "click":
        if tag in ("button", "a"):
            return True
        if role in ("button", "link", "menuitem", "tab", "option", "checkbox", "radio"):
            return True
        if tag == "input":
            input_type = (element.get("type") or "").lower()
            return input_type in ("checkbox", "radio")
        return True

    if action_type_lower == "upload":
        if tag != "input":
            return False
        input_type = (element.get("type") or "").lower()
        if input_type == "file":
            return True
        if element.get("name") == "file":
            cls = str(element.get("class") or "").lower()
            return any(k in cls for k in ("upload", "el-upload"))
        return False

    if action_type_lower in ("check", "uncheck", "toggle"):
        if tag == "input" and (element.get("type") or "").lower() in ("checkbox", "radio"):
            return True
        return role in ("checkbox", "switch")

    if action_type_lower == "assert_table":
        if tag in ("table", "th"):
            return True
        if tag == "div":
            cls = str(element.get("class") or "").lower()
            return any(kw in cls for kw in ("el-table", "ant-table", "table"))
        return False

    return True

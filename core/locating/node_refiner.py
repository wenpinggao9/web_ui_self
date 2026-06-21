"""L3 节点纠偏 (2A) —— LLM 选出 index 后, 用组件选择器二次精炼.

本框架在 L3 大模型之后自动触发纠偏, 无需规划层显式返回 skill 指令.
"""
from __future__ import annotations

from typing import Any, Optional

from .intent_route import is_ant_radio_option
from .skill_invoke import invoke_skill


def refine_node_index(
    items: list[dict],
    base_index: int,
    intent: str,
    action_type: str,
    *,
    action_value: str = "",
) -> tuple[int, str]:
    """返回 (精炼后 index, 使用的技能名或空字符串)."""
    if base_index is None or base_index < 0 or base_index >= len(items):
        return base_index, ""

    at = (action_type or "").lower()
    intent_lower = (intent or "").lower()

    if at == "click":
        if "状态开关" in intent or ("开关" in intent and "行" in intent):
            sw = invoke_skill("find_switch_in_row", items, intent)
            if sw is not None and sw != base_index:
                return sw, "find_switch_in_row"

        if any(k in intent_lower for k in ("勾选", "复选框", "checkbox", "选中", "取消勾选")):
            refined = invoke_skill("choose_best_checkbox_target", items, base_index, intent)
            if refined != base_index:
                return refined, "choose_best_checkbox_target"

        if is_ant_radio_option(intent):
            refined = invoke_skill("choose_best_click_target", items, base_index, intent)
            if refined != base_index:
                return refined, "choose_best_click_target"

        if _should_refine_click(items[base_index], intent):
            refined = invoke_skill("choose_best_click_target", items, base_index, intent)
            if refined != base_index:
                return refined, "choose_best_click_target"

    elif at == "fill":
        refined = invoke_skill(
            "choose_best_input_target",
            items, base_index, intent, expected_text=action_value or "",
        )
        if refined != base_index:
            return refined, "choose_best_input_target"

    return base_index, ""


def _should_refine_click(node: dict, intent: str) -> bool:
    tag = (node.get("tag") or "").lower()
    role = (node.get("role") or "").lower()
    cls = (node.get("class") or "").lower()
    if tag in ("span", "label", "div", "p", "li"):
        return True
    if tag == "input" and role in ("combobox", ""):
        return True
    if any(k in cls for k in (
        "el-select__input", "ant-radio", "ant-checkbox", "radio-wrapper",
    )):
        return True
    if any(k in (intent or "").lower() for k in ("下拉", "dropdown", "select")):
        return True
    return False

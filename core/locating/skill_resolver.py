"""Skill 分发: 2A 节点纠偏 + 2B selector 构建 (三级链 L3 大模型之后)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from ..skill_loader import load_component_class_features
from . import intent_route
from .intent_align import detect_component_library_from_items
from .playwright_api import infer_from_selector, validate_locator
from .skill_invoke import invoke_skill

_NODE_SKILLS = frozenset({
    "choose_best_input_target",
    "choose_best_click_target",
    "choose_best_checkbox_target",
    "find_switch_in_row",
})

_SELECTOR_SKILLS = frozenset({
    "build_dropdown_option_selector",
    "build_el_select_trigger_selector",
    "build_checkbox_selector",
    "build_radio_selector",
    "build_tree_checkbox_selector",
    "build_tree_node_selector",
    "build_date_picker_selector",
})

_COMPONENT_TYPE_TO_SKILL = {
    "select_trigger": "build_el_select_trigger_selector",
    "dropdown_option": "build_dropdown_option_selector",
    "checkbox": "build_checkbox_selector",
    "radio": "build_radio_selector",
    "tree_checkbox": "build_tree_checkbox_selector",
    "tree_node": "build_tree_node_selector",
    "date_picker": "build_date_picker_selector",
}


def info_from_recommended_selector(sel: str, nth: int = 0) -> dict:
    """将 skill 产出的 selector 转为 locator_info."""
    s = (sel or "").strip()
    if not s:
        return {"method": "css", "selector": "", "nth": nth}
    if not s.startswith("xpath=") and (s.startswith("(") or s.startswith("//")):
        s = f"xpath={s}"
    return infer_from_selector(s, nth=nth)


def pick_valid_selector(
    page: Any,
    candidates: list[str],
    *,
    exclude: Optional[set[str]] = None,
) -> Optional[str]:
    """在页面上验证 skill 候选, 返回第一个可见 selector."""
    excl = exclude or set()
    for raw in candidates:
        sel = (raw or "").strip()
        if not sel or sel in excl:
            continue
        info = info_from_recommended_selector(sel)
        key = info.get("selector") or sel
        if key in excl:
            continue
        if validate_locator(page, info):
            return sel
    return None


def extract_target_text_from_intent(intent: str) -> Optional[str]:
    """从 intent 提取引号内或下拉/checkbox 目标文案."""
    if not intent:
        return None
    for pat in (r"[「\"'](.*?)[」\"']", r'"([^"]+)"', r"'([^']+)'"):
        m = re.search(pat, intent)
        if m and (m.group(1) or "").strip():
            return m.group(1).strip()
    for pat in (
        r"(?:弹窗|对话框|抽屉)中的\s*[\"']?(.+?)[\"']?\s*下拉",
        r"(?:勾选|取消勾选|选中)\s*[\"']?(.+?)[\"']?\s*(?:复选框|checkbox)?",
    ):
        m = re.search(pat, intent)
        if m and (m.group(1) or "").strip():
            return m.group(1).strip()
    return None


def detect_component_type_from_intent(intent: str, action_type: Optional[str]) -> Optional[str]:
    """根据 intent 推断组件类型 (非规则引擎层, 供 skill 脚本选用)."""
    if action_type and action_type not in ("click", "fill", "check", "uncheck", "toggle", "select"):
        return None
    if intent_route.is_dropdown_option(intent):
        return "dropdown_option"
    if intent_route.is_select_trigger(intent):
        return "select_trigger"
    if intent_route.is_tree_checkbox(intent):
        return "tree_checkbox"
    if intent_route.is_checkbox(intent):
        return "checkbox"
    if intent_route.is_ant_radio_option(intent):
        return "radio"
    if intent_route.is_tree_node(intent):
        return "tree_node"
    if intent_route.is_date_picker(intent):
        return "date_picker"
    return None


def _invoke_build_helper(skill_name: str, items: list[dict], intent: str, target_text: str = "") -> Optional[dict]:
    try:
        if skill_name == "build_dropdown_option_selector":
            return invoke_skill("build_dropdown_option_selector", items, intent)
        if skill_name == "build_el_select_trigger_selector":
            return invoke_skill("build_el_select_trigger_selector", items, intent)
        if skill_name == "build_checkbox_selector":
            return invoke_skill("build_checkbox_selector", items, intent, target_text)
        if skill_name == "build_radio_selector":
            return invoke_skill("build_radio_selector", items, intent, target_text)
        if skill_name == "build_tree_checkbox_selector":
            return invoke_skill("build_tree_checkbox_selector", items, intent, target_text)
        if skill_name == "build_tree_node_selector":
            return invoke_skill("build_tree_node_selector", items, intent, target_text)
        if skill_name == "build_date_picker_selector":
            return invoke_skill("build_date_picker_selector", items, intent, target_text)
    except Exception:
        return None
    return None


def build_selector_via_skill(
    skill_name: str,
    items: list[dict],
    intent: str,
    *,
    target_text: str = "",
    page: Any = None,
    exclude: Optional[set[str]] = None,
) -> Optional[str]:
    """调用 build_* skill 并在页面上验证候选."""
    result = _invoke_build_helper(skill_name, items, intent, target_text)
    if not result:
        return None
    primary = result.get("selector")
    candidates = list(result.get("candidates") or [])
    if primary and primary not in candidates:
        candidates.insert(0, primary)
    if not candidates:
        return None
    if page is None:
        return str(candidates[0]).strip() or None
    return pick_valid_selector(page, candidates, exclude=exclude)


def dispatch_skill(
    skill_name: str,
    items: list[dict],
    intent: str,
    action_type: str,
    *,
    base_index: Optional[int] = None,
    action_value: str = "",
    target_text: str = "",
    page: Any = None,
    exclude: Optional[set[str]] = None,
) -> tuple[Optional[int], Optional[str]]:
    """LLM use_skill 分发. 返回 (node_index, recommended_selector)."""
    if skill_name in _NODE_SKILLS:
        payload_args: tuple[Any, ...]
        if skill_name == "choose_best_input_target":
            if base_index is None:
                return None, None
            idx = invoke_skill(
                skill_name, items, base_index, intent, expected_text=action_value or "",
            )
        elif skill_name == "find_switch_in_row":
            idx = invoke_skill(skill_name, items, intent)
        else:
            if base_index is None:
                return None, None
            idx = invoke_skill(skill_name, items, base_index, intent)
        if isinstance(idx, int) and 0 <= idx < len(items):
            sel = None
            if action_type == "click" and page is not None:
                sel = try_auto_skill_selector(
                    items, intent, action_type, idx, page=page, exclude=exclude,
                )
            return idx, sel
        return base_index, None

    if skill_name in _SELECTOR_SKILLS:
        sel = build_selector_via_skill(
            skill_name, items, intent,
            target_text=target_text,
            page=page, exclude=exclude,
        )
        return base_index, sel

    return base_index, None


def try_auto_skill_selector(
    items: list[dict],
    intent: str,
    action_type: Optional[str],
    base_index: int,
    *,
    page: Any = None,
    exclude: Optional[set[str]] = None,
    skill_path: str | Path | None = None,
    llm_xpath_builder: Optional[Callable[[str, str, str], Optional[str]]] = None,
) -> Optional[str]:
    """LLM 返回 index 后自动尝试 2B selector 构建."""
    comp_type = detect_component_type_from_intent(intent, action_type)
    if not comp_type:
        return None
    skill_name = _COMPONENT_TYPE_TO_SKILL.get(comp_type)
    if not skill_name:
        return None
    target_text = extract_target_text_from_intent(intent) or ""
    if comp_type in ("dropdown_option", "checkbox", "radio", "tree_checkbox", "tree_node") and not target_text:
        return None

    sel = build_selector_via_skill(
        skill_name, items, intent,
        target_text=target_text,
        page=page, exclude=exclude,
    )
    if sel:
        return sel

    if not skill_path or not llm_xpath_builder or not target_text:
        return None

    class_features = load_component_class_features(skill_path)
    component_library = detect_component_library_from_items(items, class_features) or "generic"
    return llm_xpath_builder(skill_name, target_text, component_library)

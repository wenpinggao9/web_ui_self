"""框架级: 从语义 DOM 推断组件类型 (无业务字段名)."""
from __future__ import annotations

# 先加载 execution 链, 避免 core.locating 包 __init__ 循环依赖
from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.locating.skill_dom_helpers import build_fill_input_selector, build_radio_selector
from core.locating.skill_resolver import infer_component_type_from_dom, resolve_component_type


def test_infer_radio_from_label_and_nearby_input():
    items = [
        {"tag": "label", "text": "选项A", "type": ""},
        {"tag": "input", "text": "", "type": "radio", "value": "optA"},
        {"tag": "label", "text": "选项B", "type": ""},
        {"tag": "input", "text": "", "type": "radio", "value": "optB"},
    ]
    intent = "在筛选区点击'选项A'作为搜索类型"
    assert infer_component_type_from_dom(items, intent, "click") == "radio"
    assert resolve_component_type(items, intent, "click") == "radio"


def test_build_radio_selector_uses_dom_value_not_business_name():
    items = [
        {"tag": "label", "text": "选项A", "type": ""},
        {"tag": "input", "text": "", "type": "radio", "value": "optA"},
    ]
    result = build_radio_selector(items, "点击'选项A'", "选项A")
    assert result["selector"] == 'input[type="radio"][value="optA"]'


def test_fill_input_selector_prefers_search_text_id():
    items = [
        {"tag": "input", "id": "searchText", "type": "text", "in_form": True, "in_dialog": False},
        {"tag": "input", "id": "otherField", "type": "text", "in_form": True, "in_dialog": False},
    ]
    intent = "在筛选区搜索框输入工单ID"
    assert resolve_component_type(items, intent, "fill") == "text_input"
    result = build_fill_input_selector(items, intent)
    assert result["selector"] == "input#searchText"

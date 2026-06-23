"""dropdown_trigger: 表单项范围内 combobox id 门禁与候选顺序."""
from __future__ import annotations

from core.locating.field_scope import combobox_ids_near_label
from core.locating.intent_rule_engine import _build_dropdown_trigger_candidates


def test_combobox_ids_near_label_adjacent_with_id():
    dom = [
        {"tag": "label", "text": "来源"},
        {"tag": "input", "role": "combobox", "id": "source"},
    ]
    assert combobox_ids_near_label(dom, "来源") == ["source"]


def test_combobox_ids_near_label_no_id_stops_before_neighbor():
    """本字段 combobox 无 id 时, 不继续扫隔壁字段的 #supplier."""
    dom = [
        {"tag": "label", "text": "类型"},
        {"tag": "div", "role": "combobox"},
        {"tag": "label", "text": "供应商"},
        {"tag": "input", "role": "combobox", "id": "supplier"},
    ]
    assert combobox_ids_near_label(dom, "类型") == []


def test_combobox_ids_near_label_stops_at_next_label():
    dom = [
        {"tag": "label", "text": "类型"},
        {"tag": "span", "text": "辅助说明"},
        {"tag": "label", "text": "供应商"},
        {"tag": "input", "role": "combobox", "id": "supplier"},
    ]
    assert combobox_ids_near_label(dom, "类型") == []


def test_combobox_ids_near_label_field_has_own_id():
    dom = [
        {"tag": "label", "text": "类型"},
        {"tag": "input", "role": "combobox", "id": "type"},
        {"tag": "label", "text": "供应商"},
        {"tag": "input", "role": "combobox", "id": "supplier"},
    ]
    assert combobox_ids_near_label(dom, "类型") == ["type"]


def test_dropdown_trigger_candidates_skip_wrong_supplier_id():
    dom = [
        {"tag": "label", "text": "类型"},
        {"tag": "div", "role": "combobox"},
        {"tag": "label", "text": "供应商"},
        {"tag": "input", "role": "combobox", "id": "supplier"},
    ]
    cands = _build_dropdown_trigger_candidates("类型", dom, "点击类型下拉框")
    assert "#supplier" not in cands
    assert any("ant-form-item" in c for c in cands)


def test_dropdown_trigger_candidates_prefers_scoped_id():
    dom = [
        {"tag": "label", "text": "来源"},
        {"tag": "input", "role": "combobox", "id": "source"},
    ]
    cands = _build_dropdown_trigger_candidates("来源", dom, "点击来源下拉框")
    assert cands[0] == "#source"

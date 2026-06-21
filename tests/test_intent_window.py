"""L3 意图窗口: 从大 DOM 中抽与 intent 相关的候选."""
from __future__ import annotations

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.dom.semantic_dom import dom_index_from_picked_indices
from core.locating.intent_window import pick_intent_window_indices


def _padding(n: int) -> list[dict]:
    return [{"tag": "span", "text": f"noise{i}"} for i in range(n)]


def test_intent_window_finds_radio_beyond_first_80():
    items = _padding(100) + [
        {"tag": "label", "text": "工单ID", "in_form": True},
        {"tag": "input", "text": "", "type": "radio", "value": "orderId", "in_form": True},
    ]
    intent = "点击'工单ID'作为搜索类型"
    picked = pick_intent_window_indices(items, intent, "click", limit=80)
    assert 100 in picked
    assert 101 in picked
    dom = dom_index_from_picked_indices(items, picked)
    assert "[100]" in dom.numbered_text
    assert "工单ID" in dom.numbered_text


def test_intent_window_small_list_unchanged():
    items = [
        {"tag": "button", "text": "提交"},
        {"tag": "input", "text": "", "placeholder": "搜索"},
    ]
    picked = pick_intent_window_indices(items, "点击提交", "click", limit=80)
    assert picked == [0, 1]


def test_intent_window_prefers_form_for_fill():
    items = _padding(50) + [
        {"tag": "span", "text": "页脚"},
        {"tag": "input", "text": "", "placeholder": "工单ID", "in_form": True},
    ]
    intent = "在筛选区搜索框输入工单ID"
    picked = pick_intent_window_indices(items, intent, "fill", limit=10)
    assert 51 in picked

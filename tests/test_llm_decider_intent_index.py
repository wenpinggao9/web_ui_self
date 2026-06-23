"""L5 element_decide: 意图窗口下 LLM 返回原始 index 的解析."""
from __future__ import annotations

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.dom.semantic_dom import DomIndex, dom_index_from_picked_indices
from core.locating.intent_window import pick_intent_window_indices
from core.locating.llm_decider import LLMElementDecider


def _padding(n: int) -> list[dict]:
    return [{"tag": "span", "text": f"noise{i}"} for i in range(n)]


def test_parse_index_accepts_original_index_beyond_dom_selectors_len():
    """意图窗口: dom.selectors 仅含抽样子集, 但 index 应对齐全量 items."""
    items = _padding(1076)
    submit_idx = 191
    items[submit_idx] = {
        "tag": "button",
        "text": "提 交",
        "role": "button",
        "in_form": True,
    }
    intent = "点击'提交'按钮查看结果"
    picked = pick_intent_window_indices(items, intent, "click", limit=120)
    dom = dom_index_from_picked_indices(items, picked)
    assert submit_idx in picked
    assert len(dom.selectors) == 120
    assert len(items) == 1076

    decider = LLMElementDecider(llm=None, prompts=None)  # type: ignore[arg-type]
    data = {"index": submit_idx, "reason": "提交按钮", "confidence": 0.98}
    idx, confidence, reason = decider._parse_index_response(
        data, dom, exclude=None, items=items,
    )
    assert idx == submit_idx
    assert confidence == 0.98
    assert reason == "提交按钮"


def test_parse_index_still_uses_dom_len_without_items():
    dom = DomIndex(
        "[0] <button> 提交\n[1] <span> 其他",
        [{"method": "css", "selector": "button"}, {"method": "css", "selector": "span"}],
    )
    decider = LLMElementDecider(llm=None, prompts=None)  # type: ignore[arg-type]
    idx, _, _ = decider._parse_index_response(
        {"index": 1, "confidence": 0.9}, dom, exclude=None,
    )
    assert idx == 1
    idx_oob, _, _ = decider._parse_index_response(
        {"index": 5, "confidence": 0.9}, dom, exclude=None,
    )
    assert idx_oob is None

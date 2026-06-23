"""五级定位可观测性 + LLMElementDecider.stats 测试."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.locating.locate_observability import counter_delta, normalize_resolve_hits, summarize_locating_stats
from core.locating.llm_decider import LLMElementDecider
from core.locating.resolver import LocatorResolver
from core.dom import DomIndex


def test_counter_delta():
    assert counter_delta({"L1缓存": 2}, {"L1缓存": 5, "L3规则": 1}) == {
        "L1缓存": 3,
        "L3规则": 1,
    }


def test_normalize_resolve_hits_merges_l2_substrategies():
    assert normalize_resolve_hits({
        "L2记忆": 3,
        "L2通用": 2,
        "L1自愈": 1,
        "L5大模型": 1,
    }) == {
        "L2记忆": 5,
        "L1缓存": 1,
        "L5大模型": 1,
    }


def test_decider_stats_tracks_fallback(monkeypatch):
    decider = LLMElementDecider(llm=MagicMock(), prompts=MagicMock())
    items = [{"tag": "button", "text": "提交", "class": ""}]

    class _Resp:
        data = {"index": -1, "reason": "无", "confidence": 0.0}

    decider.llm.complete_json = MagicMock(return_value=_Resp())
    dom = DomIndex(["0. button 提交"], ["text=提交"])

    result = decider.decide(dom, "点击「提交」按钮", "click", items=items)
    assert result.index == 0
    st = decider.stats
    assert st["llm_calls"] >= 1
    assert st["fallback_hits"] >= 1


def test_case_locating_stats_delta():
    resolver = LocatorResolver(
        decider=MagicMock(stats_snapshot=MagicMock(return_value={"llm_calls": 0})),
        cache=None,
        memory=None,
        learner=None,
    )
    resolver.begin_case_stats()
    resolver._resolve_hits["L3规则"] = 2
    stats = resolver.case_locating_stats()
    assert stats["scope"] == "case"
    assert stats["resolve_distribution"]["L3规则"] == 2


def test_summarize_includes_element_decider():
    decider = MagicMock()
    decider.stats = {"llm_calls": 3, "fallback_hits": 1, "hit_rate": 66.7}
    summary = summarize_locating_stats(decider=decider, resolve_hits={"L5大模型": 1})
    assert "element_decider" in summary
    assert summary["overall"]["non_llm_rate"] == 0.0

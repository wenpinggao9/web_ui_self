"""归一化与 IntentRuleEngine 测试."""
from __future__ import annotations

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.locating.intent_rule_engine import IntentRuleEngine
from core.locating.normalize import (
    normalize_intent_cache,
    normalize_intent_relaxed,
    normalize_url,
)


def test_normalize_url_hash_route():
    assert normalize_url("https://host/#/video/all-question") == "/video/all-question"


def test_normalize_intent_relaxed_removes_spaces_and_lowercases():
    assert normalize_intent_relaxed("点击'工单ID'作为搜索类型") == "点击工单id作为搜索类型"


def test_normalize_intent_cache_keeps_word_boundary():
    assert normalize_intent_cache("点击 提交 按钮") == "点击 提交 按钮"


def test_intent_rule_engine_resolve_when_selector_valid(monkeypatch):
    engine = IntentRuleEngine()
    monkeypatch.setattr(engine, "_validate_selector", lambda _p, _s: True)
    items = [{"tag": "div", "text": "广东省", "role": "option"}]
    sel = engine.resolve(None, "选择'广东省'", "click", items)
    assert sel is not None
    assert engine.last_matched_rule() == "dropdown_option"

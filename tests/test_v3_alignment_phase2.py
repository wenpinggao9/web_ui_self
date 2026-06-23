"""L5 fallback + L1 短路 + selector_type 测试."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.locating.fallback_resolve import fallback_resolve_index
from core.locating.selector_type import infer_selector_type
from core.locating.cache import SelectorCache
from core.locating.resolver import LocatorResolver
from core.planning import PlannedAction


def test_fallback_resolve_click_quoted_text():
    items = [
        {"tag": "button", "text": "提交", "class": ""},
        {"tag": "button", "text": "取消", "class": ""},
    ]
    idx = fallback_resolve_index(items, "点击「提交」按钮", "click")
    assert idx == 0


def test_infer_selector_type_rule_and_skill():
    assert infer_selector_type({"selector": "#a"}, source="L3规则") == "rule"
    assert infer_selector_type({"selector": ".x"}, source="L3Skill", from_skill=True) == "skill"
    assert infer_selector_type({"selector": "//div[1]"}) == "xpath"


def test_try_acceleration_only_skips_dom(monkeypatch, tmp_path):
    cache = SelectorCache(path=tmp_path / "c.json")
    cache.put("https://x.com/p", "click", "点按钮", {"selector": "#btn"})
    page = MagicMock()
    page.url = "https://x.com/p"
    resolver = LocatorResolver(decider=MagicMock(), cache=cache, memory=None, learner=None)
    monkeypatch.setattr(
        "core.locating.cache.validate_selector",
        lambda _p, _info, timeout_ms=1500: True,
    )
    info = resolver.try_acceleration_only(page, "点按钮", "click")
    assert info is not None
    assert info.get("_source") == "L1缓存"


def test_dispatcher_fast_path_no_dom_prep(monkeypatch, tmp_path):
    cache = SelectorCache(path=tmp_path / "c.json")
    cache.put("https://x.com/p", "click", "点提交", {"selector": "#submit"})
    page = MagicMock()
    page.url = "https://x.com/p"
    loc = MagicMock()
    loc.evaluate.return_value = "<button>提交</button>"
    page.locator.return_value.first = loc
    page.locator.return_value.count.return_value = 1

    resolver = LocatorResolver(decider=MagicMock(), cache=cache, memory=None, learner=None)
    monkeypatch.setattr(
        "core.locating.cache.validate_selector",
        lambda _p, _info, timeout_ms=1500: True,
    )
    monkeypatch.setattr(
        "core.execution.dispatcher.resolve_locator",
        lambda _p, info: loc,
    )
    dom_called = {"n": 0}

    def _should_not_prepare(_self, _action):
        dom_called["n"] += 1
        return [], "fail"

    monkeypatch.setattr(
        ActionDispatcher, "_prepare_semantic_items_for_locate", _should_not_prepare,
    )

    disp = ActionDispatcher(page, resolver, default_timeout_ms=5000)
    action = PlannedAction(type="click", intent="点提交")
    loc_out, _ = disp._resolve(action)
    assert loc_out is not None
    assert dom_called["n"] == 0

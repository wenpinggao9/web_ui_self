"""下拉选项 intent: 禁止 L2 通用 bare text= 短路, 重试仍走 L3."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.locating.cache import SelectorCache
from core.locating.intent_route import is_unsafe_dropdown_option_selector
from core.locating.memory import SelectorMemory
from core.locating.resolver import LocatorResolver


def test_is_unsafe_dropdown_option_selector():
    intent = "在下拉选项中点击'前审'"
    assert is_unsafe_dropdown_option_selector(intent, 'text="前审"')
    assert not is_unsafe_dropdown_option_selector(intent, "#showType_list_0")
    assert not is_unsafe_dropdown_option_selector(
        intent,
        '.ant-select-dropdown:visible >> role=option >> text="前审"',
    )
    assert not is_unsafe_dropdown_option_selector("点击'提交'", 'text="提交"')


def test_acceleration_skips_generic_text_for_dropdown_option(monkeypatch, tmp_path):
    memory = SelectorMemory(tmp_path / "mem.json")
    memory.put_generic(
        "ant-design",
        "dropdown_option",
        'text="{text}"',
    )
    page = MagicMock()
    page.url = "https://x.com/check-history"

    def _locator(sel: str):
        loc = MagicMock()
        if sel == 'text="前审"':
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
        else:
            loc.count.return_value = 0
        return loc

    page.locator.side_effect = _locator
    monkeypatch.setattr(
        "core.locating.skill_resolver.resolve_component_type",
        lambda _items, _intent, _at: "dropdown_option",
    )
    monkeypatch.setattr(
        "core.locating.skill_resolver.extract_target_text_from_intent",
        lambda _intent: "前审",
    )

    resolver = LocatorResolver(
        decider=MagicMock(),
        cache=None,
        memory=memory,
        learner=None,
    )
    hit = resolver.try_acceleration_only(
        page, "在下拉选项中点击'前审'", "click", semantic_items=[{"class": "ant-table"}],
    )
    assert hit is None


def test_l1_bare_text_rejected_for_dropdown_option(monkeypatch, tmp_path):
    cache = SelectorCache(path=tmp_path / "c.json")
    cache.put(
        "https://x.com/h",
        "click",
        "在下拉选项中点击前审",
        {"selector": 'text="前审"'},
    )
    page = MagicMock()
    page.url = "https://x.com/h"
    monkeypatch.setattr(
        "core.locating.cache.validate_selector",
        lambda _p, _info, timeout_ms=1500: True,
    )

    rule_engine = MagicMock()
    rule_engine.resolve.return_value = (
        '.ant-select-dropdown:visible >> role=option >> text="前审"'
    )
    rule_engine.last_matched_rule.return_value = "dropdown_option"

    resolver = LocatorResolver(
        decider=MagicMock(),
        cache=cache,
        memory=None,
        learner=None,
        rule_engine=rule_engine,
    )
    monkeypatch.setattr(
        "core.locating.resolver.validate_selector",
        lambda _p, _info: True,
    )
    info = resolver.resolve(
        page,
        "在下拉选项中点击'前审'",
        "click",
        semantic_items=[{"tag": "div", "text": "x"}],
        skip_acceleration=True,
    )
    assert info is not None
    assert "ant-select-dropdown" in info.get("selector", "")
    assert info.get("_source") == "L3规则"
    rule_engine.resolve.assert_called()


def test_skip_heuristics_retry_still_runs_l3_for_dropdown_option(monkeypatch, tmp_path):
    page = MagicMock()
    page.url = "https://x.com/h"
    rule_engine = MagicMock()
    rule_engine.resolve.return_value = (
        '.ant-select-dropdown:visible .ant-select-item-option-content:has-text("前审")'
    )
    rule_engine.last_matched_rule.return_value = "dropdown_option"

    resolver = LocatorResolver(
        decider=MagicMock(),
        cache=None,
        memory=None,
        learner=MagicMock(resolve=MagicMock(return_value=None)),
        rule_engine=rule_engine,
    )
    monkeypatch.setattr(
        "core.locating.resolver.validate_selector",
        lambda _p, _info: True,
    )
    info = resolver.resolve(
        page,
        "在下拉选项中点击'前审'",
        "click",
        semantic_items=[{"tag": "option", "text": "前审", "role": "option"}],
        skip_heuristics=True,
        skip_acceleration=True,
    )
    assert info is not None
    assert info.get("_source") == "L3规则"
    resolver.learner.resolve.assert_not_called()

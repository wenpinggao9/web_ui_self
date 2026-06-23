"""L2 generic + skip_heuristics 重试策略."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.locating.memory import SelectorMemory, _instantiate_generic_template
from core.locating.resolver import LocatorResolver


def test_instantiate_generic_template():
    assert _instantiate_generic_template(
        ".ant-radio-wrapper:has-text('{text}')", "男",
    ) == '.ant-radio-wrapper:has-text("男")'


def test_lookup_generic_hit(monkeypatch):
    memory = SelectorMemory("/tmp/unused_memory.json")
    memory.put_generic(
        "ant-design", "radio",
        ".ant-radio-wrapper:has-text('{text}')",
    )
    page = MagicMock()

    def _locator(sel: str):
        loc = MagicMock()
        loc.count.return_value = 1
        loc.first.is_visible.return_value = True
        return loc

    page.locator.side_effect = _locator
    monkeypatch.setattr(
        "core.locating.skill_resolver.resolve_component_type",
        lambda _items, _intent, _at: "radio",
    )
    monkeypatch.setattr(
        "core.locating.skill_resolver.extract_target_text_from_intent",
        lambda _intent: "男",
    )

    info = memory.lookup_generic(
        page, "click", "选择「男」", [],
        component_library="ant-design",
    )
    assert info is not None
    assert "男" in info["selector"]
    assert memory.stats["generic_hits"] == 1


def test_skip_heuristics_skips_l3_l4_not_l1(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.json"
    from core.locating.cache import SelectorCache

    cache = SelectorCache(path=cache_path)
    cache.put("https://x.com/p", "click", "点按钮", {"selector": "#btn"})
    page = MagicMock()
    page.url = "https://x.com/p"
    monkeypatch.setattr(
        "core.locating.cache.validate_selector",
        lambda _p, _info, timeout_ms=1500: True,
    )

    resolver = LocatorResolver(
        decider=MagicMock(),
        cache=cache,
        memory=None,
        learner=MagicMock(resolve=MagicMock(return_value={"selector": "#learned"})),
        rule_engine=MagicMock(
            resolve=MagicMock(return_value="#rule"),
            last_matched_rule=MagicMock(return_value="test_rule"),
        ),
    )
    info = resolver.resolve(
        page, "点按钮", "click",
        skip_heuristics=True,
    )
    assert info is not None
    assert info.get("_source") == "L1缓存"
    resolver.rule_engine.resolve.assert_not_called()
    resolver.learner.resolve.assert_not_called()

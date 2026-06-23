"""L4 命中回填 L1/L2."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.locating.cache import SelectorCache
from core.locating.resolver import LocatorResolver
from core.locating.structure_learner import StructureLearner


def test_l4_hit_backfills_l1(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    cache = SelectorCache(path=cache_path)
    learner = StructureLearner(tmp_path / "learn.json")
    learner._records.append({
        "route": "/p",
        "action_type": "click",
        "intent": "点按钮",
        "tokens": {"点", "按钮"},
        "selector": "#btn",
        "nth": 0,
    })

    page = MagicMock()
    page.url = "https://host/p"
    monkeypatch.setattr(
        "core.locating.resolver.validate_selector",
        lambda _p, _info, timeout_ms=1500: True,
    )
    monkeypatch.setattr(
        "core.locating.structure_learner.validate_selector",
        lambda _p, _info, timeout_ms=1500: True,
    )

    resolver = LocatorResolver(
        decider=MagicMock(),
        cache=cache,
        memory=None,
        learner=learner,
        rule_engine=MagicMock(
            resolve=MagicMock(return_value=None),
            last_matched_rule=MagicMock(return_value=None),
        ),
    )
    info = resolver.resolve(page, "点按钮", "click", skip_acceleration=False)
    assert info is not None
    assert info.get("_source") == "L4学习"
    assert cache.get("https://host/p", "click", "点按钮") is not None

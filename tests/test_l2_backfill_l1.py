"""L2 命中回填 L1."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.locating.cache import SelectorCache
from core.locating.memory import SelectorMemory
from core.locating.resolver import LocatorResolver


def test_l2_hit_backfills_l1(tmp_path, monkeypatch):
    mem_path = tmp_path / "memory.json"
    cache_path = tmp_path / "cache.json"
    memory = SelectorMemory(mem_path)
    cache = SelectorCache(path=cache_path)
    memory._store["/p|click|点按钮"] = {
        "selector": "#btn",
        "method": "css",
        "success_count": 2,
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    memory.save()

    page = MagicMock()
    page.url = "https://host/p"
    monkeypatch.setattr(
        "core.locating.memory.SelectorMemory._validate_selector",
        staticmethod(lambda _page, _selector: True),
    )
    monkeypatch.setattr(
        "core.locating.resolver.validate_selector",
        lambda _p, _info, timeout_ms=1500: True,
    )

    resolver = LocatorResolver(
        decider=MagicMock(),
        cache=cache,
        memory=memory,
        learner=None,
        rule_engine=MagicMock(resolve=MagicMock(return_value=None), last_matched_rule=MagicMock(return_value=None)),
    )
    info = resolver.resolve(page, "点按钮", "click", skip_acceleration=False)
    assert info is not None
    assert info.get("_source") == "L2记忆"
    assert cache.get("https://host/p", "click", "点按钮") is not None

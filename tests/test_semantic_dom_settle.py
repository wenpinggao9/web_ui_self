"""语义 DOM 节点数收敛等待."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.dom.semantic_dom import wait_for_semantic_items_settle


def test_wait_for_semantic_items_settle_stops_when_count_stable(monkeypatch):
    page = MagicMock()
    counts = iter([80, 96, 130, 130])
    calls: list[int] = []

    def fake_extract(*_a, **_k):
        n = next(counts, 130)
        calls.append(n)
        return [{"text": f"n{i}"} for i in range(n)]

    monkeypatch.setattr("core.dom.semantic_dom.extract_items", fake_extract)
    monkeypatch.setattr("core.dom.semantic_dom.wait_for_dom_stable", lambda *_a, **_k: None)

    items = wait_for_semantic_items_settle(
        page, poll_ms=0, stable_rounds=2, timeout_ms=5000, pre_quiet_ms=0,
    )
    assert len(items) == 130
    assert 130 in calls


def test_wait_for_semantic_items_settle_returns_last_on_timeout(monkeypatch):
    page = MagicMock()
    seq = [50, 80, 96]

    def fake_extract(*_a, **_k):
        n = seq.pop(0) if seq else 96
        return [{"text": "x"}] * n

    monkeypatch.setattr("core.dom.semantic_dom.extract_items", fake_extract)
    monkeypatch.setattr("core.dom.semantic_dom.wait_for_dom_stable", lambda *_a, **_k: None)

    items = wait_for_semantic_items_settle(
        page, poll_ms=0, stable_rounds=3, timeout_ms=1, pre_quiet_ms=0,
    )
    assert len(items) == 96

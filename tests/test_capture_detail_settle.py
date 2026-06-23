"""capture settle 由通用导航信号触发, 不依赖列表/详情 URL 形态."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.dispatcher import ActionDispatcher
from core.execution.script_helpers import operation_caused_navigation


def test_operation_caused_navigation_signals():
    assert operation_caused_navigation(
        {}, outcome="route_changed",
    )
    assert operation_caused_navigation(
        {"new_tab_opened": True},
        url_before="https://a/list", url_now="https://a/list",
    )
    assert operation_caused_navigation(
        {},
        url_before="https://a/list", url_now="https://a/item/42",
    )
    assert not operation_caused_navigation(
        {},
        url_before="https://a/list", url_now="https://a/list",
    )


def test_capture_after_navigation_uses_semantic_settle(monkeypatch):
    page = MagicMock()
    page.url = "https://x/app/item/42"
    page.is_closed.return_value = False

    resolver = MagicMock()
    resolver._framework_selectors = None
    d = ActionDispatcher(page, resolver, console=MagicMock())
    d.last_dispatch_meta = {
        "navigation_outcome": "route_changed",
        "url_before": "https://x/app/list",
        "url_after": "https://x/app/item/42",
        "new_tab_opened": True,
    }

    settle_calls: list[bool] = []

    monkeypatch.setattr(
        "core.execution.dispatcher.wait_for_semantic_items_settle",
        lambda *_a, **_k: settle_calls.append(True)
        or [{"tag": "div", "text": f"n{i}"} for i in range(130)],
    )
    monkeypatch.setattr(
        "core.execution.dispatcher.extract_items",
        lambda *_a, **_k: [{"tag": "div", "text": "x"}] * 96,
    )
    monkeypatch.setattr(
        "core.execution.dispatcher.wait_for_dom_stable", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(d, "_ensure_live_page", lambda **_k: None)
    monkeypatch.setattr(
        "core.execution.dispatcher.bring_page_to_front", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "core.execution.dispatcher._page_alive", lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "core.execution.dispatcher.recover_active_page",
        lambda p, **_k: (p, False),
    )

    d.capture_page_state_after_operation()

    assert settle_calls
    assert len(d._page_state["semantic_items"]) == 130


def test_capture_without_navigation_skips_settle(monkeypatch):
    page = MagicMock()
    page.url = "https://x/app/list"
    page.is_closed.return_value = False

    resolver = MagicMock()
    resolver._framework_selectors = None
    d = ActionDispatcher(page, resolver, console=MagicMock())

    settle_calls: list[bool] = []

    monkeypatch.setattr(
        "core.execution.dispatcher.wait_for_semantic_items_settle",
        lambda *_a, **_k: settle_calls.append(True) or [],
    )
    monkeypatch.setattr(
        "core.execution.dispatcher.extract_items",
        lambda *_a, **_k: [{"tag": "tr", "text": "行"}] * 40,
    )
    monkeypatch.setattr(
        "core.execution.dispatcher.wait_for_dom_stable", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(d, "_ensure_live_page", lambda **_k: None)
    monkeypatch.setattr(
        "core.execution.dispatcher.bring_page_to_front", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "core.execution.dispatcher._page_alive", lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "core.execution.dispatcher.recover_active_page",
        lambda p, **_k: (p, False),
    )

    d.capture_page_state_after_operation()

    assert not settle_calls
    assert len(d._page_state["semantic_items"]) == 40

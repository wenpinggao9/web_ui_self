"""PageSession: tab handoff 与断言 DOM 上下文."""
from __future__ import annotations

from core.execution.page_session import PageSession


class _Page:
    def __init__(self, url: str, *, closed: bool = False) -> None:
        self.url = url
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed

    def bring_to_front(self) -> None:
        pass


def test_on_detail_opened_records_list_anchor():
    sess = PageSession()
    list_p = _Page("https://host/list")
    detail = _Page("https://host/detail?id=1")
    sess.on_detail_opened(list_p, detail)
    assert sess.list_anchor is list_p
    assert sess.active is detail
    assert sess.page_state is None


def test_cache_preserved_when_page_busy():
    sess = PageSession(active=_Page("https://host/list"))
    sess.page_state = {
        "key": "https://host/list",
        "semantic_items": [{"text": "146482421"}],
        "dom_summary": "146482421",
    }
    ok, items, _, err = sess.context_for_assert(
        capture_fn=lambda **_: None,
        ensure_tab_fn=lambda **_: False,
    )
    assert ok and not err
    assert "146482421" in str(items)


def test_is_detail_submit_handoff_requires_detail_url():
    assert PageSession.is_detail_submit_handoff({
        "submit_click_ok": True,
        "url_before": "https://host/list",
    }) is False
    assert PageSession.needs_list_tab_handoff({
        "submit_click_ok": True,
        "url_before": "https://host/detail?uniqId=1",
        "navigation_outcome": "resource_id_changed",
        "url_after": "https://host/detail?uniqId=2",
    }) is False


def test_same_tab_entity_change_not_list_handoff():
    meta = {
        "submit_click_ok": True,
        "url_before": "https://host/video/detail/?uniqId=774",
        "url_after": "https://host/video/detail/?uniqId=777",
        "navigation_outcome": "resource_id_changed",
    }
    assert PageSession.is_same_tab_submit_nav(meta) is True
    assert PageSession.needs_list_tab_handoff(meta) is False


def test_uniqid_case_insensitive_cache_match():
    sess = PageSession(active=_Page("https://host/detail?uniqId=777"))
    sess.page_state = {
        "key": "https://host/detail?uniqid=777",
        "semantic_items": [{"text": "详情"}],
    }
    assert sess.cache_matches_active() is True


def test_cache_matches_active_same_url():
    sess = PageSession(active=_Page("https://host/List"))
    sess.page_state = {"key": "https://host/list", "semantic_items": [{}]}
    assert sess.cache_matches_active() is True

"""Tab 跟随: 新开 tab 跟随 / 关闭回 anchor / 提交等待不 timeout."""
from __future__ import annotations

import time
from typing import Any

import pytest

from core.execution.dispatcher import ActionDispatcher
from core.execution.tab_follow import (
    DEFAULT_SUBMIT_WAIT_MS,
    follow_active_tab,
    wait_after_detail_submit,
)

LIST = "https://example.com/list"
DETAIL_A = "https://example.com/detail/?uniqId=100"
DETAIL_B = "https://example.com/detail/?uniqId=200"


class _Page:
    def __init__(self, url: str, *, closed: bool = False, ctx: Any = None) -> None:
        self.url = url
        self._closed = closed
        self.context = ctx

    def is_closed(self) -> bool:
        return self._closed

    def bring_to_front(self) -> None:
        pass

    def evaluate(self, *_: Any, **__: Any) -> bool:
        if self._closed:
            raise RuntimeError("closed")
        return True

    def inner_text(self, _: str, **__: Any) -> str:
        return ""

    def wait_for_timeout(self, _: int) -> None:
        pass

    def reload(self, **_: Any) -> None:
        pass


class _Ctx:
    def __init__(self, pages: list[_Page]) -> None:
        self.pages = pages

    def wait_for_event(self, name: str, timeout: int = 0) -> Any:
        raise TimeoutError(name)


def _two_tab_ctx() -> tuple[_Page, _Page, _Ctx]:
    list_p = _Page(LIST)
    detail = _Page(DETAIL_A)
    ctx = _Ctx([list_p, detail])
    list_p.context = ctx
    detail.context = ctx
    return list_p, detail, ctx


def test_follow_newest_detail_when_second_tab_open():
    list_p, detail, _ = _two_tab_ctx()
    page, switched, rule = follow_active_tab(list_p, list_p)
    assert page is detail
    assert switched is True
    assert rule == "follow_newest_sibling"


def test_fallback_list_anchor_when_detail_closed():
    list_p, detail, _ = _two_tab_ctx()
    detail._closed = True
    page, switched, rule = follow_active_tab(detail, list_p)
    assert page is list_p
    assert switched is True
    assert rule == "fallback_list_anchor"


def test_submit_wait_no_timeout_outcome():
    list_p, detail, _ = _two_tab_ctx()
    t0 = time.monotonic()
    out_page, outcome, _ = wait_after_detail_submit(
        detail,
        list_anchor=list_p,
        url_before=DETAIL_A,
        budget_ms=500,
    )
    elapsed = time.monotonic() - t0
    assert outcome != "timeout"
    assert outcome in ("settled", "returned_to_list", "resource_id_changed", "submit_error")
    assert elapsed < DEFAULT_SUBMIT_WAIT_MS / 1000 + 1.0
    assert out_page is detail


def test_submit_wait_instant_entity_change():
    list_p, detail, _ = _two_tab_ctx()
    detail.url = DETAIL_B
    out_page, outcome, _ = wait_after_detail_submit(
        detail,
        list_anchor=list_p,
        url_before=DETAIL_A,
        budget_ms=500,
    )
    assert outcome == "resource_id_changed"
    assert out_page is detail


def test_submit_wait_closed_detail_returns_list():
    list_p, detail, _ = _two_tab_ctx()
    detail._closed = True
    out_page, outcome, recovered = wait_after_detail_submit(
        detail,
        list_anchor=list_p,
        url_before=DETAIL_A,
        budget_ms=800,
    )
    assert recovered is True
    assert outcome == "returned_to_list"
    assert out_page is list_p


def test_dispatcher_submit_wait_budget_capped():
    resolver = pytest.importorskip("unittest.mock").MagicMock()
    resolver._framework_selectors = None
    list_p, detail, _ = _two_tab_ctx()
    d = ActionDispatcher(detail, resolver)
    d._list_tab_anchor = list_p
    t0 = time.monotonic()
    d._wait_after_detail_submit(DETAIL_A)
    assert time.monotonic() - t0 < DEFAULT_SUBMIT_WAIT_MS / 1000 + 1.5
    assert (d.last_dispatch_meta or {}).get("navigation_outcome") != "timeout"

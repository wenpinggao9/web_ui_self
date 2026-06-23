"""粘滞 tab 规则 (纯 anchor 模型)."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.script_helpers import (
    find_newest_non_anchor_tab,
    pick_surviving_tab_after_detail_close,
    sticky_resolve_tab,
)


def _page(url: str, *, closed: bool = False, usable: bool = True) -> MagicMock:
    p = MagicMock()
    p.url = url
    p.is_closed.return_value = closed
    if closed:
        p.evaluate.side_effect = Exception("closed")
    elif not usable:
        p.evaluate.side_effect = TimeoutError("slow")
    else:
        p.evaluate.return_value = True
    return p


def _ctx(*pages):
    c = MagicMock()
    c.pages = list(pages)
    for p in pages:
        p.context = c
    return c


def test_sticky_stays_on_active_even_if_slow():
    list_p = _page("https://x/list")
    child = _page("https://x/child", usable=False)
    _ctx(list_p, child)
    resolved, switched, reason = sticky_resolve_tab(child, list_p)
    assert resolved is child
    assert switched is False
    assert reason == "stick_active"


def test_sticky_falls_back_to_anchor_when_active_closed():
    list_p = _page("https://x/list")
    dead = _page("https://x/child", closed=True)
    _ctx(list_p, dead)
    resolved, switched, reason = sticky_resolve_tab(dead, list_p)
    assert resolved is list_p
    assert switched is True
    assert reason == "stick_anchor"


def test_pick_surviving_skips_anchor_while_child_alive():
    list_p = _page("https://x/list")
    child = _page("https://x/child", usable=False)
    _ctx(list_p, child)
    page, rec, url, left = pick_surviving_tab_after_detail_close(
        child,
        url_before="https://x/child?id=1",
        list_anchor=list_p,
    )
    assert page is child
    assert left is False


def test_find_newest_non_anchor():
    list_p = _page("https://x/list")
    child = _page("https://x/child", usable=False)
    ctx = _ctx(list_p, child)
    assert find_newest_non_anchor_tab(ctx, list_p) is child

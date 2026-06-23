"""跨用例 tab  handoff: 详情页连续操作时不应被切回列表 tab."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.script_helpers import pick_role_handoff_page
from core.planning.page_nav import should_preserve_page_on_case_start


class _Page:
    def __init__(self, url: str, *, closed: bool = False) -> None:
        self.url = url
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed

    def evaluate(self, *_: object, **__: object) -> bool:
        return True


class _Ctx:
    def __init__(self, pages: list) -> None:
        self.pages = pages


def test_should_preserve_for_detail_page_step():
    assert should_preserve_page_on_case_start(
        [],
        ["前审在详情页选择「题目内容涉及不良导向/敏感信息」"],
    )


def test_pick_role_handoff_prefer_current_keeps_detail():
    list_tab = _Page("https://x/video/wait-preview")
    detail_tab = _Page("https://x/video/detail/?uniqId=146550684")
    primary = list_tab
    ctx = _Ctx([list_tab, detail_tab])

    without = pick_role_handoff_page(
        ctx, detail_tab, primary_page=primary, prefer_current=False,
        reason="test",
    )
    assert without is list_tab

    with_pref = pick_role_handoff_page(
        ctx, detail_tab, primary_page=primary,
        prefer_current=True, prefer_detail=True,
        reason="test",
    )
    assert with_pref is detail_tab


class _SlowDetail(_Page):
    def evaluate(self, *_: object, **__: object) -> bool:
        raise TimeoutError("slow page")


def test_pick_role_handoff_weak_detail_over_welcome():
    list_tab = _Page("https://x/video/wait-preview")
    detail_tab = _SlowDetail("https://x/video/detail/?uniqId=146550684")
    welcome = _Page("https://x/video/welcome")
    ctx = _Ctx([list_tab, detail_tab, welcome])

    got = pick_role_handoff_page(
        ctx,
        detail_tab,
        primary_page=welcome,
        prefer_current=True,
        prefer_detail=True,
        reason="test_weak",
    )
    assert got is detail_tab

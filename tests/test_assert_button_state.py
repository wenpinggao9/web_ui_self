"""按钮置灰/可点断言: 规划 extras.state 必须走 live page + 控件断言."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.dispatcher import ActionDispatcher
from core.locating.resolver import LocatorResolver
from core.planning import PlannedAction


def _dispatcher() -> ActionDispatcher:
    return ActionDispatcher(page=MagicMock(), resolver=MagicMock(spec=LocatorResolver))


def test_assert_needs_live_page_for_disabled_extras():
    d = _dispatcher()
    action = PlannedAction(
        type="assert_text",
        intent="验证'领取题目'按钮置灰",
        value="领取题目",
        extras={"state": "disabled"},
    )
    assert d._assert_needs_live_page(action) is True


def test_assert_needs_live_page_for_plain_text():
    d = _dispatcher()
    action = PlannedAction(
        type="assert_text",
        intent="验证页面含领取题目",
        value="领取题目",
    )
    assert d._assert_needs_live_page(action) is False


def _mock_button_locator(*, disabled: bool) -> MagicMock:
    btn = MagicMock()
    btn.is_disabled.return_value = disabled
    loc = MagicMock()
    loc.count.return_value = 1
    loc.nth.return_value = btn
    return loc


def _mock_page_with_button(disabled: bool) -> MagicMock:
    page = MagicMock()
    loc = _mock_button_locator(disabled=disabled)

    def _locator(sel: str) -> MagicMock:
        wrap = MagicMock()
        wrap.first = MagicMock()
        wrap.first.get_by_role = MagicMock(return_value=loc)
        return wrap

    page.locator.side_effect = _locator
    page.get_by_role = MagicMock(return_value=MagicMock(count=MagicMock(return_value=0)))
    return page


def test_try_assert_button_state_disabled_pass():
    d = _dispatcher()
    d.page = _mock_page_with_button(disabled=True)

    action = PlannedAction(
        type="assert_text",
        intent="验证'领取题目'按钮置灰",
        value="领取题目",
        extras={"state": "disabled"},
    )
    ok, msg = d._try_assert_button_state(action, "领取题目")
    assert ok is True
    assert "控件断言(置灰)" in msg
    assert "disabled=True" in msg


def test_try_assert_button_state_enabled_fails_when_disabled_expected():
    d = _dispatcher()
    d.page = _mock_page_with_button(disabled=False)

    action = PlannedAction(
        type="assert_text",
        intent="验证'领取题目'按钮置灰",
        value="领取题目",
        extras={"state": "disabled"},
    )
    ok, msg = d._try_assert_button_state(action, "领取题目")
    assert ok is False
    assert "未通过(仍可点)" in msg

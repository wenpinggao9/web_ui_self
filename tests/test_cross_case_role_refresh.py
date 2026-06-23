"""跨用例多角色切回时刷新页面."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.cross_case_session import (
    refresh_page_on_role_reentry,
    role_reentry_needs_refresh,
)


class _Page:
    def __init__(self) -> None:
        self.reload = MagicMock()


def test_role_reentry_needs_refresh_when_switching_back():
    assert role_reentry_needs_refresh(
        role="teacherA",
        last_active_role="teacherB",
        cross_case_session=True,
        role_already_has_context=True,
    )


def test_role_reentry_no_refresh_same_role():
    assert not role_reentry_needs_refresh(
        role="teacherA",
        last_active_role="teacherA",
        cross_case_session=True,
        role_already_has_context=True,
    )


def test_role_reentry_no_refresh_first_case():
    assert not role_reentry_needs_refresh(
        role="teacherA",
        last_active_role=None,
        cross_case_session=True,
        role_already_has_context=True,
    )


def test_role_reentry_no_refresh_new_role_context():
    assert not role_reentry_needs_refresh(
        role="teacherA",
        last_active_role="teacherB",
        cross_case_session=True,
        role_already_has_context=False,
    )


def test_refresh_page_on_role_reentry_calls_reload():
    pg = _Page()
    console = MagicMock()
    ok = refresh_page_on_role_reentry(
        pg,
        role="teacherA",
        last_active_role="teacherB",
        cross_case_session=True,
        role_already_has_context=True,
        console=console,
    )
    assert ok is True
    pg.reload.assert_called_once()
    console.print.assert_called()


def test_refresh_page_skipped_when_not_needed():
    pg = _Page()
    ok = refresh_page_on_role_reentry(
        pg,
        role="teacherA",
        last_active_role="teacherA",
        cross_case_session=True,
        role_already_has_context=True,
    )
    assert ok is False
    pg.reload.assert_not_called()

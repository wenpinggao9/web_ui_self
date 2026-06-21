"""retry_hint: 裸 CSS 选择器提取."""
from __future__ import annotations

from core.execution.retry_hint import (
    extract_selector_from_resolve_hint,
    resolve_force_selector_from_hint,
)


def test_extract_bare_input_id_selector():
    assert extract_selector_from_resolve_hint("input#searchText") == "input#searchText"
    assert extract_selector_from_resolve_hint("#searchText") == "#searchText"


def test_extract_wrapped_selector_still_works():
    assert extract_selector_from_resolve_hint("选择器如 'button:has-text(\"提交\")'") == (
        'button:has-text("提交")'
    )


def test_resolve_force_from_bare_hint_without_dom():
    assert resolve_force_selector_from_hint("input#searchText") == "input#searchText"

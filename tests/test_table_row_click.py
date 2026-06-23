"""表格行内按钮: 文案空格与 ant-table 固定列."""
from __future__ import annotations

from core.execution.script_helpers import (
    _button_label_variants,
    _normalize_btn_label,
    _row_text_contains_key,
    parse_table_row_click,
)


def test_button_variants_for_spaced_log():
    variants = _button_label_variants("日志")
    assert "日 志" in variants
    assert "日志" in variants


def test_row_text_contains_numeric_key():
    row_text = "146487944\t大学\t数学\t待前审\t日 志\t详情"
    assert _row_text_contains_key(row_text, "146487944")
    assert not _row_text_contains_key(row_text, "46487944")


def test_parse_table_row_click_extracts_order_id():
    intent = "点击工单ID为146487944的'日志'"
    parsed = parse_table_row_click(intent, {})
    assert parsed is not None
    button, row_hint, status = parsed
    assert button == "日志"
    assert row_hint == "146487944"
    assert status is None


def test_normalize_btn_label():
    assert _normalize_btn_label("日 志") == "日志"
    assert _normalize_btn_label("查\u00a0看") == "查看"


def test_parse_recovery_first_task_with_id():
    intent = "点击第一条任务(146550684)的查看按钮，进入详情页"
    parsed = parse_table_row_click(intent, {})
    assert parsed is not None
    button, row_hint, status = parsed
    assert button == "查看"
    assert row_hint == "146550684"
    assert status is None


def test_parse_recovery_first_task_quoted_button():
    intent = "点击第一条任务(146550684)的'查看'按钮，进入详情页"
    parsed = parse_table_row_click(intent, {})
    assert parsed is not None
    assert parsed[0] == "查看"
    assert parsed[1] == "146550684"


def test_parse_list_any_row_view_button():
    intent = "点击待前审列表中任意一条任务的'查看'按钮"
    parsed = parse_table_row_click(intent, {})
    assert parsed == ("查看", "__first_row__", None)

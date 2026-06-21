"""codegen: 行内表格点击不得把 row_note 当 CSS."""
from __future__ import annotations

from core.codegen import _gen_table_row_click_step, _is_table_row_click
from core.planning.action_schema import PlannedAction


def test_is_table_row_click_from_locator_info():
    a = PlannedAction(
        type="click",
        intent="点击工单ID为146487944的'日志'",
        locator_info={"method": "table_row", "selector": "ant_table_row[146487944].日志"},
        selector="ant_table_row[146487944].日志 (extras.row_key)",
        extras={"row_key": "146487944", "button": "日志", "row_key_column": "工单ID"},
    )
    assert _is_table_row_click(a)


def test_gen_table_row_click_uses_locate_helper():
    a = PlannedAction(
        type="click",
        intent="点击工单ID为146487944的'日志'",
        locator_info={"method": "table_row", "selector": "ant_table_row[146487944].日志"},
        selector="ant_table_row[146487944].日志 (extras.row_key)",
        extras={"row_key": "146487944", "button": "日志", "row_key_column": "工单ID"},
    )
    lines = _gen_table_row_click_step(a, {}, False)
    text = "\n".join(lines)
    assert "wait_for_table_row_button" in text
    assert "locate_button_in_table_row" not in text
    assert "ant_table_row" not in text
    assert "146487944" in text

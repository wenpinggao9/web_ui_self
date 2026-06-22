"""codegen: API 变量在生成脚本中应使用 api_ctx 而非写死字面量."""
from __future__ import annotations

from core.codegen import (
    _gen_assert_table_lines,
    _gen_codegen_assert_lines,
    _gen_step_log_line,
    _intent_log_expr,
    _value_expr,
)
from core.execution.assert_codegen import record_literal
from core.planning.action_schema import PlannedAction
from core.variable_substitution import find_api_var_for_value


def test_intent_log_expr_uses_api_ctx_at_runtime():
    expr = _intent_log_expr(
        "验证工单ID为146533814的生产状态为待前审",
        {"orderId1": "146533814"},
        runtime_api=True,
    )
    assert "api_ctx['orderId1']" in expr
    assert "146533814" not in expr


def test_gen_step_log_line_runtime():
    action = PlannedAction(
        type="assert_table",
        intent="验证工单ID为146533814的生产状态为待前审",
    )
    line = _gen_step_log_line(
        11, action, api_context={"orderId1": "146533814"}, runtime_api=True,
    )
    assert "api_ctx['orderId1']" in line
    assert "146533814" not in line


def test_find_api_var_for_value_prefers_order_id():
    ctx = {"orderId1": "146532421", "other": "146532421"}
    assert find_api_var_for_value("146532421", ctx) == "orderId1"


def test_value_expr_runtime_maps_literal_to_api_ctx():
    ctx = {"orderId1": "146530204"}
    assert _value_expr("146530204", ctx, runtime_api=True) == "api_ctx['orderId1']"
    assert _value_expr("${orderId1}", ctx, runtime_api=True) == "api_ctx['orderId1']"


def test_value_expr_runtime_keeps_unrelated_literal():
    ctx = {"orderId1": "146530204"}
    assert _value_expr("待前审", ctx, runtime_api=True) == "'待前审'"


def test_gen_assert_table_uses_api_ctx_for_row_key():
    action = PlannedAction(
        type="assert_table",
        intent="验证工单ID为146530204的生产状态为待前审",
        value="146530204",
        extras={"column": "生产状态", "expected": "待前审", "row_key_column": "工单ID"},
    )
    lines = _gen_assert_table_lines(
        action, {"orderId1": "146530204"}, runtime_api=True,
    )
    text = "\n".join(lines)
    assert "api_ctx['orderId1']" in text
    assert "146530204" not in text


def test_record_literal_stores_placeholder_when_matches_api_context():
    action = PlannedAction(type="assert_text", intent="验证列表", value="146530204")
    record_literal(action, "146530204", api_context={"orderId1": "146530204"})
    assert action.extras["codegen_assert"]["text"] == "${orderId1}"
    lines = _gen_codegen_assert_lines(
        action.extras["codegen_assert"],
        {"orderId1": "146530204"},
        runtime_api=True,
    )
    assert "api_ctx['orderId1']" in "\n".join(lines)

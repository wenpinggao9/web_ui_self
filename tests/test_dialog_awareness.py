"""弹窗感知辅助."""
from core.locating.dialog_awareness import (
    has_dialog_nodes,
    intent_may_trigger_dialog,
    intent_targets_dialog_button,
)


def test_intent_targets_dialog_button():
    assert intent_targets_dialog_button("点击弹窗中的确定按钮")
    assert not intent_targets_dialog_button("点击搜索按钮")


def test_has_dialog_nodes():
    assert has_dialog_nodes([{"role": "dialog", "text": "确认"}])
    assert has_dialog_nodes([{"in_dialog": True, "text": "确定"}])
    assert not has_dialog_nodes([{"tag": "button", "text": "确定"}])


def test_intent_may_trigger_dialog():
    assert intent_may_trigger_dialog("点击提交按钮")
    assert not intent_may_trigger_dialog("点击搜索")

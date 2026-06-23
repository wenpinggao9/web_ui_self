"""可选步骤跳过 (对齐 V3 optional_step)."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.optional_step import (
    is_optional_action,
    is_optional_step_text,
    should_skip_optional_step,
    tag_optional_actions_from_steps,
)
from core.planning import PlannedAction


def test_is_optional_step_text():
    assert is_optional_step_text("若出现广告弹窗则关闭")
    assert not is_optional_step_text("点击提交按钮")


def test_is_optional_action_by_intent():
    act = PlannedAction(type="click", intent="若出现「温馨提示」弹窗则点击关闭")
    assert is_optional_action(act)


def test_tag_optional_from_case_steps():
    actions = [
        PlannedAction(type="click", intent="若出现「广告」弹窗则关闭"),
        PlannedAction(type="click", intent="点击提交"),
    ]
    tagged = tag_optional_actions_from_steps(actions, ["若出现「广告」弹窗则关闭", "点击提交"])
    assert is_optional_action(tagged[0])
    assert not is_optional_action(tagged[1])


def test_should_skip_when_target_absent():
    page = MagicMock()
    page.locator.return_value.inner_text.return_value = "列表页内容"
    act = PlannedAction(type="click", intent="若出现「不存在Banner」则关闭", extras={"optional": True})
    assert should_skip_optional_step(page, act, False, "找不到元素: 不存在Banner") is True


def test_should_not_skip_when_dispatch_ok():
    page = MagicMock()
    act = PlannedAction(type="click", intent="若出现「广告」则关闭", extras={"optional": True})
    assert should_skip_optional_step(page, act, True, "ok") is False

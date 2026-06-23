"""role 规划后校验测试."""
from __future__ import annotations

from core.planning.action_schema import PlannedAction
from core.planning.role_sanitize import sanitize_planned_roles


def test_strip_invalid_role():
    actions = [PlannedAction(type="click", intent="点领取", role="preview_teacher")]
    out = sanitize_planned_roles(
        actions,
        ["teacherC_k12", "admin"],
        primary_role="teacherC_k12",
    )
    assert out[0].role is None


def test_strip_redundant_primary_role():
    actions = [PlannedAction(type="click", intent="点领取", role="teacherC_k12")]
    out = sanitize_planned_roles(
        actions,
        ["teacherC_k12"],
        primary_role="teacherC_k12",
    )
    assert out[0].role is None


def test_keep_different_valid_role():
    actions = [PlannedAction(type="click", intent="管理员操作", role="admin")]
    out = sanitize_planned_roles(
        actions,
        ["teacherC_k12", "admin"],
        primary_role="teacherC_k12",
    )
    assert out[0].role == "admin"

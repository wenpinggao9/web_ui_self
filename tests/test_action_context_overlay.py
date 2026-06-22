"""action_context 与 overlay_state 单元测试."""
from __future__ import annotations

from core.execution.action_context import (
    action_context_satisfied,
    filter_contradictory_recovery,
    infer_required_page_context,
    recovery_navigates_to_detail,
)
from core.execution.overlay_state import (
    assert_targets_overlay_content,
    overlay_cache_stale,
)
from core.planning import PlannedAction


def test_infer_detail_context_from_intent():
    assert infer_required_page_context("在详情页选择'多题'") == "detail"
    assert infer_required_page_context("点击提交") is None


def test_action_context_satisfied_on_detail_url():
    ok, note = action_context_satisfied(
        "https://host/video/detail/?uniqId=146518562",
        "在详情页选择'多题'",
    )
    assert ok is True
    assert "详情" in note


def test_action_context_not_satisfied_when_intent_pins_other_id():
    ok, _ = action_context_satisfied(
        "https://host/video/detail/?uniqId=146518562",
        "在详情页验证任务146518561的状态",
    )
    assert ok is False


def test_filter_recovery_when_already_on_detail():
    recovery = [
        PlannedAction(
            type="click",
            intent="点击任务ID为146518561的那一行的查看按钮，进入任务详情页",
        ),
    ]
    filtered = filter_contradictory_recovery(
        recovery,
        "在详情页选择'多题'",
        "https://host/video/detail/?uniqId=146518562",
    )
    assert filtered == []


def test_recovery_navigates_to_detail_pattern():
    assert recovery_navigates_to_detail("点击查看进入任务详情页") is True
    assert recovery_navigates_to_detail("在详情页选择多题") is False


def test_overlay_cache_stale_when_dialog_opens():
    cached = {"overlay": {"open": False, "fingerprint": "", "char_len": 0}}

    class _Page:
        def locator(self, _sel):
            return _Wrap()

    class _Wrap:
        def count(self):
            return 1

        @property
        def first(self):
            return _Loc()

    class _Loc:
        def inner_text(self, timeout=0):
            return "操作记录\n前审领取"

    assert overlay_cache_stale(_Page(), cached) is True


def test_assert_targets_overlay_from_intent():
    assert assert_targets_overlay_content("验证弹窗内包含提交成功") is True
    assert assert_targets_overlay_content("验证列表含工单") is False

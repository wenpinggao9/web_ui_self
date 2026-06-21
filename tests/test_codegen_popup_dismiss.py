"""codegen: 运行期关弹窗记录应落入生成脚本."""
from __future__ import annotations

from core.codegen import _gen_ui_steps
from core.planning.action_schema import PlannedAction


def test_click_before_intent_gets_popup_dismiss():
    actions = [
        PlannedAction(type="click", intent="点击'领取'按钮", value="领取题目"),
    ]
    code = _gen_ui_steps(
        actions,
        api_context={},
        runtime_api=False,
        popup_dismiss_used=False,
        popup_dismiss_before=["点击'领取'按钮"],
    )
    assert "_dismiss_blocking_dialog_if_present(page)" in code
    assert "领取题目" in code

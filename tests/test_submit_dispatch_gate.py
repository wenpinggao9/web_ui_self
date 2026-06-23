"""提交 dispatch 双门 + 列表页 finalize 不升级 (对齐 V3)."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.submit_post_verify import (
    evaluate_submit_post_check,
    finalize_submit_after_dispatch,
    submit_dispatch_should_succeed,
)

LIST_URL = "https://www-gwp11-bc.suanshubang.com/video/all-question"
DETAIL_URL = "https://www-gwp11-bc.suanshubang.com/video/detail/?uniqId=146482774"


def test_finalize_list_page_preserves_failed_dispatch():
    page = MagicMock()
    meta = {"url_before": LIST_URL}
    fin = finalize_submit_after_dispatch(page, meta, dispatch_ok=False)
    assert fin.dispatch_ok is False


def test_finalize_list_page_preserves_successful_dispatch():
    page = MagicMock()
    meta = {"url_before": LIST_URL}
    fin = finalize_submit_after_dispatch(page, meta, dispatch_ok=True)
    assert fin.dispatch_ok is True


def test_submit_dispatch_should_succeed_requires_click_ok():
    assert submit_dispatch_should_succeed({}) is False
    assert submit_dispatch_should_succeed({"left_detail_context": True}) is False
    assert submit_dispatch_should_succeed({
        "submit_click_ok": True,
        "left_detail_context": True,
        "url_before": DETAIL_URL,
    }) is True


def test_evaluate_submit_short_circuits_on_dispatch_failure():
    verdict = evaluate_submit_post_check(
        "点击「提交」按钮查看结果",
        dispatch_ok=False,
        dispatch_meta={"url_before": LIST_URL},
        page=MagicMock(),
        dom_summary="当前总数为:9669",
    )
    assert verdict.step_ok is False
    assert "未成功点击" in verdict.reason


def test_evaluate_submit_non_submit_intent_delegates_to_llm():
    verdict = evaluate_submit_post_check(
        "点击「全部题目」",
        dispatch_ok=False,
        dispatch_meta={},
        page=MagicMock(),
        dom_summary="menu",
    )
    assert verdict.step_ok is None

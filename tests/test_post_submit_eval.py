"""断言: 实时 DOM + SubmitLiveFacts (无固化快照)."""
from __future__ import annotations

from core.execution.post_submit_eval import (
    build_live_submit_facts,
    eval_submit_expect,
    infer_expect_from_text,
)
from core.execution.assert_or import try_or_branches


class _Page:
    def __init__(self, url: str, body: str = "") -> None:
        self.url = url
        self._body = body

    def is_closed(self) -> bool:
        return False

    def bring_to_front(self) -> None:
        pass

    def evaluate(self, *_: object, **__: object) -> bool:
        return True

    def inner_text(self, _: str) -> str:
        return self._body


def test_or_branch_literal_before_settled_meta():
    """有「详情」字面量时不应被 settled 快照挡住."""
    page = _Page(
        "https://x.com/detail/?uniqId=106",
        body="任务详情 审核原因 提交",
    )
    meta = {
        "submit_click_ok": True,
        "navigation_outcome": "settled",
        "url_before": "https://x.com/detail/?uniqId=103",
        "entity_id_before": "103",
        "entity_id_after": "106",
    }
    branches = [
        {"intent": "验证页面返回到待领取页面", "value": "待领取"},
        {"intent": "验证页面自动加载下一个任务详情页", "value": "详情"},
    ]
    body = "任务详情 审核原因 提交"
    facts = build_live_submit_facts(
        page=page, items=None, dispatch_meta=meta, api_context={},
    )
    hit = try_or_branches(
        page, branches, body,
        dispatch_meta=meta, live_facts=facts,
    )
    assert hit is not None
    assert hit[0] is True
    assert "详情" in hit[1]


def test_live_facts_entity_changed_from_url():
    meta = {
        "submit_click_ok": True,
        "navigation_outcome": "settled",
        "url_before": "https://x.com/detail/?uniqId=103",
        "entity_id_before": "103",
    }
    page = _Page("https://x.com/detail/?uniqId=107")
    facts = build_live_submit_facts(
        page=page, items=None, dispatch_meta=meta, api_context={},
    )
    assert facts is not None
    assert facts.entity_changed
    assert facts.navigation_outcome == "resource_id_changed"
    ok, _ = eval_submit_expect("entity_changed", facts, page.url)
    assert ok is True

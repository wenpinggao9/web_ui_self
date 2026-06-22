"""提交关详情 tab 后 handoff — 真实浏览器集成测试.

模拟 VIP 视频场景:
  列表 tab → 新开详情 tab → 提交后 window.close() → 断言应读到列表页 DOM.

运行:
  .venv/bin/python -m pytest tests/test_tab_handoff_integration.py -v -s
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from core.execution.dispatcher import ActionDispatcher
from core.execution.page_session import PageSession
from core.execution.script_helpers import (
    _page_alive,
    _page_handoff_usable,
    _page_usable,
    is_detail_submission_url,
    wait_after_detail_submit,
)
from core.planning import PlannedAction

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tab_handoff"
LIST_URL = (FIXTURE_DIR / "list.html").resolve().as_uri()
DETAIL_PATH = "detail.html?uniqId=146497238"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def list_and_detail_tabs(browser):
    """打开列表页 + 详情 popup, 与 dispatcher 打开详情 tab 行为一致."""
    ctx = browser.new_context()
    list_page = ctx.new_page()
    list_page.goto(LIST_URL, wait_until="domcontentloaded")

    with ctx.expect_page() as pinfo:
        list_page.click("#open-detail")
    detail_page = pinfo.value
    detail_page.wait_for_load_state("domcontentloaded")

    assert "详情" in detail_page.inner_text("body")
    assert len(ctx.pages) == 2

    yield ctx, list_page, detail_page

    ctx.close()


def _meta_after_settled_submit(url_before: str) -> dict[str, Any]:
    """复现日志: outcome=settled, left_detail_context=True, url 仍为详情."""
    return {
        "navigation_outcome": "settled",
        "url_before": url_before,
        "url_after": url_before,
        "submit_click_ok": True,
        "left_detail_context": True,
        "detail_tab_closed": True,
        "recovered": False,
    }


class TestTabCloseHandoffIntegration:
    """真实 Playwright: 提交后详情 tab 关闭."""

    def test_submit_closes_detail_tab(self, list_and_detail_tabs):
        ctx, list_page, detail_page = list_and_detail_tabs
        url_before = detail_page.url
        assert is_detail_submission_url(url_before)

        detail_page.click("#submit-btn")
        detail_page.wait_for_event("close", timeout=5000)

        assert detail_page.is_closed()
        assert _page_alive(list_page)
        assert "待领取" in list_page.inner_text("body")
        assert len([p for p in ctx.pages if not p.is_closed()]) == 1

    def test_page_session_force_recover_after_tab_close(self, list_and_detail_tabs):
        ctx, list_page, detail_page = list_and_detail_tabs
        url_before = detail_page.url

        detail_page.click("#submit-btn")
        detail_page.wait_for_event("close", timeout=5000)

        sess = PageSession(active=detail_page, list_anchor=list_page)
        ok = sess.force_recover_to_live_tab(url_before=url_before, reason="integration_test")[1]

        assert ok, "force_recover 应切到列表 tab"
        assert sess.active is list_page
        assert _page_handoff_usable(sess.active) or "待领取" in sess.active.inner_text("body")
        assert "待领取" in sess.active.inner_text("body")

    def test_finish_handoff_recaptures_list_dom(self, list_and_detail_tabs):
        ctx, list_page, detail_page = list_and_detail_tabs
        url_before = detail_page.url

        detail_page.click("#submit-btn")
        detail_page.wait_for_event("close", timeout=5000)

        sess = PageSession(active=detail_page, list_anchor=list_page)
        captured: list[str] = []

        def capture(**kwargs: Any) -> None:
            captured.append(str(kwargs.get("nav_outcome", "")))
            from core.dom import extract_items, compact_dom_lines

            items = extract_items(sess.active, profile="post_verify")
            sess.page_state = {
                "key": (sess.active.url or "").lower(),
                "semantic_items": items,
                "dom_summary": compact_dom_lines(items),
            }

        meta = _meta_after_settled_submit(url_before)
        sess.finish_detail_submit_handoff(
            meta,
            recapture=True,
            capture_fn=capture,
            should_reload_fn=lambda _m, _p: False,
        )

        assert sess.active is list_page
        assert captured, "应重抓列表页 DOM"
        assert sess.has_dom_cache()
        flat = sess.page_state.get("dom_summary") or ""
        assert "待领取" in flat

    def test_dispatcher_assert_after_submit_tab_close(self, list_and_detail_tabs):
        """端到端: 提交关 tab 后 page 仍指向 dead detail → Step4 assert 应 handoff 并 PASS."""
        _ctx, list_page, detail_page = list_and_detail_tabs
        url_before = detail_page.url

        resolver = MagicMock()
        resolver._framework_selectors = None
        d = ActionDispatcher(detail_page, resolver, console=MagicMock())
        d._session.list_anchor = list_page

        detail_page.click("#submit-btn")
        detail_page.wait_for_event("close", timeout=5000)

        # 复现线上 bug: active 仍指向已关闭的 detail tab
        assert not _page_alive(d.page)
        d.last_dispatch_meta = _meta_after_settled_submit(url_before)

        assert_action = PlannedAction(
            type="assert_text",
            intent="待领取",
            value="待领取",
        )
        ok, msg = d.dispatch(assert_action)
        assert ok, f"断言应成功: {msg}"
        assert d.page is list_page
        assert _page_handoff_usable(d.page) or "待领取" in list_page.inner_text("body")
        assert "待领取" in list_page.inner_text("body")

    def test_wait_after_detail_submit_then_tab_closes(self, list_and_detail_tabs):
        """复现日志时序: wait 返回 settled 后 tab 才关闭."""
        ctx, list_page, detail_page = list_and_detail_tabs
        url_before = detail_page.url

        detail_page.click("#submit-btn")
        page, outcome, recovered = wait_after_detail_submit(
            detail_page,
            list_anchor=list_page,
            url_before=url_before,
            budget_ms=3000,
        )
        # 等待 window.close 完成
        try:
            detail_page.wait_for_event("close", timeout=3000)
        except Exception:
            pass

        sess = PageSession(active=detail_page, list_anchor=list_page)
        meta = {
            "navigation_outcome": outcome,
            "url_before": url_before,
            "url_after": url_before,
            "submit_click_ok": True,
            "left_detail_context": not _page_alive(detail_page),
            "detail_tab_closed": not _page_alive(detail_page),
        }
        if not _page_alive(detail_page):
            meta["left_detail_context"] = True
            meta["detail_tab_closed"] = True

        sess.finish_detail_submit_handoff(meta, recapture=False)

        assert sess.active is list_page or _page_usable(sess.active)
        assert "待领取" in sess.active.inner_text("body")

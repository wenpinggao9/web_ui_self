"""提交后 tab/DOM handoff 回归 — 对应 VIP 视频「提交 → 下一任务 / 关 tab 回列表 → 断言」.

运行:
  .venv/bin/python -m pytest tests/test_submit_tab_handoff.py -v
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.execution.dispatcher import ActionDispatcher
from core.execution.page_session import PageSession

# 来自真实日志的 URL
LIST_URL = "https://www-gwp11-bc.suanshubang.com/video/all-question"
DETAIL_774 = "https://www-gwp11-bc.suanshubang.com/video/detail/?uniqId=146482774"
DETAIL_777 = "https://www-gwp11-bc.suanshubang.com/video/detail/?uniqId=146482777"
DOM_KEY_LOWER = "https://www-gwp11-bc.suanshubang.com/video/detail/?uniqid=146482777"


class _Page:
    """最小 Playwright Page 替身."""

    def __init__(self, url: str, *, closed: bool = False) -> None:
        self.url = url
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed

    def bring_to_front(self) -> None:
        pass

    def evaluate(self, *_: Any, **__: Any) -> bool:
        if self._closed:
            raise RuntimeError("closed")
        return True

    def inner_text(self, _: str, **__: Any) -> str:
        return ""

    def reload(self, **_: Any) -> None:
        pass

    def wait_for_timeout(self, _: int) -> None:
        pass


def _meta_resource_id_changed() -> dict[str, Any]:
    return {
        "navigation_outcome": "resource_id_changed",
        "url_before": DETAIL_774,
        "url_after": DETAIL_777,
        "submit_click_ok": True,
        "entity_id_before": "146482774",
        "entity_id_after": "146482777",
    }


def _meta_tab_closed_to_list() -> dict[str, Any]:
    return {
        "navigation_outcome": "returned_to_list",
        "url_before": DETAIL_774,
        "url_after": LIST_URL,
        "submit_click_ok": True,
        "left_detail_context": True,
        "detail_tab_closed": True,
    }


def _dom_after_submit_detail() -> dict[str, Any]:
    return {
        "key": DOM_KEY_LOWER,
        "semantic_items": [
            {"text": "详情"},
            {"text": "审核原因"},
            {"text": "提交"},
        ],
        "dom_summary": "详情 审核原因 提交",
    }


# ---------------------------------------------------------------------------
# PageSession: resource_id_changed 同 tab（日志 Step3→Step4 场景）
# ---------------------------------------------------------------------------


def test_same_tab_submit_nav_classification():
    meta = _meta_resource_id_changed()
    assert PageSession.is_same_tab_submit_nav(meta) is True
    assert PageSession.needs_list_tab_handoff(meta) is False


def test_finish_handoff_preserves_dom_on_resource_id_changed():
    """后校验 sync 不应 invalidate 已有 DOM (157 items 场景)."""
    sess = PageSession(active=_Page(DETAIL_777))
    sess.list_anchor = _Page(LIST_URL)
    sess.page_state = dict(_dom_after_submit_detail())

    capture_calls: list[dict] = []

    def capture(**kwargs: Any) -> None:
        capture_calls.append(kwargs)
        sess.page_state = {
            "key": sess.page_key(),
            "semantic_items": [{"text": "不应走到这"}],
            "dom_summary": "",
        }

    sess.finish_detail_submit_handoff(
        _meta_resource_id_changed(),
        recapture=True,
        capture_fn=capture,
        should_reload_fn=lambda _m, _p: False,
    )

    assert capture_calls == [], "同 tab 且缓存有效时不应重抓 DOM"
    assert sess.has_dom_cache()
    assert sess.active.url == DETAIL_777
    assert "详情" in sess.page_state.get("dom_summary", "")


def test_step3_to_step4_assert_reads_cached_dom():
    """模拟: Step3 提交 PASS → sync → Step4 assert_text('详情')."""
    sess = PageSession(active=_Page(DETAIL_777))
    sess.list_anchor = _Page(LIST_URL)
    sess.page_state = dict(_dom_after_submit_detail())

    # --- Step3 后校验 sync (same_tab_nav 分支) ---
    meta = _meta_resource_id_changed()
    if PageSession.is_same_tab_submit_nav(meta) and not PageSession.needs_list_tab_handoff(meta):
        if not sess.cache_matches_active():
            sess.invalidate_dom()

    assert sess.has_dom_cache(), "sync 后 DOM 缓存应仍在"

    # --- Step4 断言 ---
    ok, items, dom, err = sess.context_for_assert(
        capture_fn=lambda **_: pytest.fail("断言不应触发重抓"),
        ensure_tab_fn=lambda **_: True,
    )
    assert ok, err
    flat = dom + str(items)
    assert "详情" in flat


def test_uniqid_case_mismatch_still_matches_cache():
    sess = PageSession(active=_Page(DETAIL_777))
    sess.page_state = dict(_dom_after_submit_detail())
    assert sess.cache_matches_active() is True


# ---------------------------------------------------------------------------
# PageSession: 详情 tab 关闭 → 回列表
# ---------------------------------------------------------------------------


@patch("core.execution.page_session.pick_surviving_tab_after_detail_close")
def test_tab_closed_handoff_switches_to_list_and_recaptures(mock_pick):
    list_page = _Page(LIST_URL)
    dead_detail = _Page(DETAIL_774, closed=True)
    mock_pick.return_value = (list_page, True, LIST_URL, True)

    sess = PageSession(active=dead_detail, list_anchor=list_page)
    capture_calls: list[str] = []

    def capture(**kwargs: Any) -> None:
        capture_calls.append(str(kwargs.get("nav_outcome", "")))
        sess.page_state = {
            "key": LIST_URL.lower(),
            "semantic_items": [{"text": "待领取"}],
            "dom_summary": "待领取",
        }

    sess.finish_detail_submit_handoff(
        _meta_tab_closed_to_list(),
        recapture=True,
        capture_fn=capture,
        should_reload_fn=lambda _m, _p: False,
    )

    assert sess.active is list_page
    assert sess.has_dom_cache()
    assert capture_calls, "回列表后应重抓 DOM"
    assert "待领取" in sess.page_state.get("dom_summary", "")


def test_returned_to_list_needs_handoff():
    meta = _meta_tab_closed_to_list()
    assert PageSession.needs_list_tab_handoff(meta) is True
    assert PageSession.is_same_tab_submit_nav(meta) is False


# ---------------------------------------------------------------------------
# ActionDispatcher.sync_page_after_post_check 集成
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatcher_with_detail_dom() -> ActionDispatcher:
    resolver = MagicMock()
    resolver._framework_selectors = None
    d = ActionDispatcher(_Page(DETAIL_777), resolver, console=MagicMock())
    d._session.list_anchor = _Page(LIST_URL)
    d._session.page_state = dict(_dom_after_submit_detail())
    d.last_dispatch_meta = _meta_resource_id_changed()
    return d


def test_dispatcher_sync_same_tab_does_not_wipe_cache(dispatcher_with_detail_dom):
    d = dispatcher_with_detail_dom
    d.capture_page_state_after_operation = MagicMock()  # type: ignore[method-assign]

    d.sync_page_after_post_check(
        recovered_page=None,
        meta=_meta_resource_id_changed(),
        recapture=True,
    )

    d.capture_page_state_after_operation.assert_not_called()
    assert d._session.has_dom_cache()
    assert "详情" in d._session.page_state.get("dom_summary", "")


def test_dispatcher_get_page_state_for_assert_recaptures_when_entity_changed(
    dispatcher_with_detail_dom,
):
    """提交后 URL 实体变化时不能复用旧 DOM, 应 force_live 重抓."""
    d = dispatcher_with_detail_dom
    d.sync_page_after_post_check(
        recovered_page=None,
        meta=_meta_resource_id_changed(),
        recapture=True,
    )
    # 模拟仍指向旧实体 URL, 缓存已是新实体 DOM
    d.page = _Page(DETAIL_774)
    d._session.page_state = dict(_dom_after_submit_detail())

    captured: list[dict] = []

    def fake_capture(**kwargs: Any) -> None:
        captured.append(kwargs)
        d._session.page_state = dict(_dom_after_submit_detail())

    d.capture_page_state_after_operation = fake_capture  # type: ignore[method-assign]

    ok, items, dom, err = d._get_page_state_for_assert()
    assert ok, err
    assert captured, "实体 URL 与缓存 key 不一致时应重抓"
    assert "详情" in dom + str(items)


def test_dispatcher_assert_skips_heavy_prepare_when_cache_valid():
    """领取后连续 assert: 应复用操作后 DOM, 不走 9s tab 等待."""
    resolver = MagicMock()
    resolver._framework_selectors = None
    url = "https://www-gwp11-bc.suanshubang.com/video/wait-preview"
    d = ActionDispatcher(_Page(url), resolver, console=MagicMock())
    d._session.page_state = {
        "key": url.lower(),
        "semantic_items": [{"text": "当前总数为:3"}, {"text": "大学"}],
        "dom_summary": "当前总数为:3 大学",
    }
    d._ensure_assert_tab = MagicMock(return_value=True)  # type: ignore[method-assign]

    ok, items, dom, err = d._get_page_state_for_assert()
    assert ok, err
    d._ensure_assert_tab.assert_not_called()
    assert "当前总数为:3" in dom + str(items)
    """同 URL 连续断言 (如领取后多条 assert) 应复用操作后 DOM."""
    resolver = MagicMock()
    resolver._framework_selectors = None
    url = "https://www-gwp11-bc.suanshubang.com/video/wait-preview"
    d = ActionDispatcher(_Page(url), resolver, console=MagicMock())
    d._session.page_state = {
        "key": url.lower(),
        "semantic_items": [{"text": "当前总数为:3"}, {"text": "大学"}],
        "dom_summary": "当前总数为:3 大学",
    }
    d.capture_page_state_after_operation = MagicMock()  # type: ignore[method-assign]

    ok, items, dom, err = d._get_page_state_for_assert()
    assert ok, err
    d.capture_page_state_after_operation.assert_not_called()
    assert "当前总数为:3" in dom + str(items)


@patch("core.execution.page_session.pick_surviving_tab_after_detail_close")
def test_dispatcher_sync_tab_closed_recaptures_on_list(mock_pick, dispatcher_with_detail_dom):
    list_page = _Page(LIST_URL)
    mock_pick.return_value = (list_page, True, LIST_URL, True)

    d = dispatcher_with_detail_dom
    d.page = _Page(DETAIL_774, closed=True)

    captured: list[dict] = []

    def fake_capture(**kwargs: Any) -> None:
        captured.append(kwargs)
        d._session.page_state = {
            "key": LIST_URL.lower(),
            "semantic_items": [{"text": "列表"}],
            "dom_summary": "列表",
        }

    d.capture_page_state_after_operation = fake_capture  # type: ignore[method-assign]

    d.sync_page_after_post_check(
        recovered_page=list_page,
        meta=_meta_tab_closed_to_list(),
        recapture=True,
    )

    assert d.page is list_page
    assert captured, "关 tab 回列表应重抓 DOM"
    ok, _, dom, err = d._get_page_state_for_assert()
    assert ok, err
    assert "列表" in dom


def test_pick_surviving_timeout_same_detail_does_not_mark_left():
    """timeout 仍停同一详情 URL 时, 勿误标 left_detail / 勿切 list tab."""
    from core.execution.script_helpers import pick_surviving_tab_after_detail_close

    detail = _Page(DETAIL_774)
    list_page = _Page(LIST_URL)

    class _Ctx:
        pages = [detail, list_page]

    detail.context = _Ctx()  # type: ignore[attr-defined]
    list_page.context = _Ctx()  # type: ignore[attr-defined]

    page, rec, url, left = pick_surviving_tab_after_detail_close(
        detail,
        url_before=DETAIL_774,
        list_anchor=list_page,
    )
    assert page is detail
    assert left is False
    assert url == DETAIL_774
    assert rec is False


def test_pick_surviving_closed_detail_switches_to_list():
    from core.execution.script_helpers import pick_surviving_tab_after_detail_close

    detail = _Page(DETAIL_774, closed=True)
    list_page = _Page(LIST_URL)

    class _Ctx:
        pages = [detail, list_page]

    detail.context = _Ctx()  # type: ignore[attr-defined]
    list_page.context = _Ctx()  # type: ignore[attr-defined]

    page, _, url, left = pick_surviving_tab_after_detail_close(
        detail,
        url_before=DETAIL_774,
        list_anchor=list_page,
    )
    assert page is list_page
    assert left is True
    assert url == LIST_URL


def test_should_reload_false_on_same_tab_entity_change():
    meta = _meta_resource_id_changed()
    assert ActionDispatcher._should_reload_after_tab_handoff(meta, _Page(DETAIL_777)) is False


def test_should_reload_true_on_returned_to_list():
    meta = _meta_tab_closed_to_list()
    assert ActionDispatcher._should_reload_after_tab_handoff(meta, _Page(LIST_URL)) is True


def test_wait_after_detail_submit_instant_same_tab():
    """URL 已在 click 后变化时, 不应进入长轮询."""
    from core.execution.script_helpers import wait_after_detail_submit

    page = _Page(DETAIL_777)
    t0 = __import__("time").monotonic()
    out_page, outcome, _ = wait_after_detail_submit(
        page,
        url_before=DETAIL_774,
        max_polls=50,
    )
    elapsed = __import__("time").monotonic() - t0
    assert outcome == "resource_id_changed"
    assert out_page is page
    assert elapsed < 1.0, f"同 tab 实体切换应立即返回, 实际 {elapsed:.1f}s"


def test_is_same_tab_detail_entity_nav():
    from core.execution.script_helpers import is_same_tab_detail_entity_nav

    assert is_same_tab_detail_entity_nav(
        DETAIL_774, DETAIL_777, "resource_id_changed",
    ) is True
    assert is_same_tab_detail_entity_nav(
        DETAIL_774, LIST_URL, "returned_to_list",
    ) is False

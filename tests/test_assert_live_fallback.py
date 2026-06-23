"""断言 live 作用域文本 / 缓存刷新路径."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.execution.assert_scope import (
    AssertScope,
    try_live_scoped_text,
    try_scoped_literal_items,
)
from core.execution.dispatcher import ActionDispatcher
from core.planning import PlannedAction


def test_try_live_scoped_text_finds_in_page_regions():
    page = MagicMock()
    page.evaluate.return_value = {
        "form": "请选择审核原因: 非大学题 多题 提交",
        "main": "",
        "body": "",
    }
    scope = AssertScope(region_keys=["form", "main_right"], explicit_region=True)
    hit = try_live_scoped_text(page, scope, "非大学题")
    assert hit is not None
    assert hit[0] is True
    assert "非大学题" in hit[1]


def test_try_scoped_literal_miss_live_hit():
    scope = AssertScope(region_keys=["form"], explicit_region=True)
    items = [{"text": "请选择审核原因:", "in_form": True}]
    miss = try_scoped_literal_items(scope, items, "非大学题")
    assert miss == (False, "区域断言: 表单区 不含 '非大学题'")

    page = MagicMock()
    page.evaluate.return_value = {"form": "非大学题", "body": "非大学题"}
    live = try_live_scoped_text(page, scope, "非大学题")
    assert live is not None and live[0] is True


def test_assert_text_refreshes_after_cached_scoped_miss(monkeypatch):
    resolver = MagicMock()
    resolver._framework_selectors = None
    d = ActionDispatcher(MagicMock(), resolver, console=MagicMock())

    stale = [{"tag": "div", "text": "请选择审核原因:", "in_form": True}]
    fresh = [{"tag": "label", "text": "非大学题", "in_form": True}]

    call = {"n": 0}

    def fake_get_state(action=None, *, force_refresh=False):
        call["n"] += 1
        if force_refresh:
            items = fresh
        else:
            items = stale
        from core.dom.semantic_dom import compact_dom_lines
        return True, items, compact_dom_lines(items), ""

    monkeypatch.setattr(d, "_get_page_state_for_assert", fake_get_state)
    monkeypatch.setattr(d, "_can_fast_assert", lambda: True)
    d._session.has_dom_cache = lambda: True  # type: ignore[method-assign]
    d._session.page_state = {"key": "x", "semantic_items": stale, "settled": False}
    monkeypatch.setattr(
        "core.execution.dispatcher.try_live_scoped_text",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        d,
        "_semantic_assert",
        lambda *a, **k: (False, "semantic fail"),
    )
    monkeypatch.setattr(d, "_should_semantic_fallback", lambda _s: True)

    action = PlannedAction(
        type="assert_text",
        intent="验证右侧展示审核原因选项包含'非大学题'",
        value="非大学题",
    )
    ok, msg = d._assert_text(action)
    assert ok is True
    assert "非大学题" in msg
    assert call["n"] >= 2


def test_assert_text_live_fallback_when_force_refresh_capture_fails(monkeypatch):
    """活页 DOM 重抓失败时, 不应直接 FAIL, 应继续走 live/字面量兜底."""
    resolver = MagicMock()
    resolver._framework_selectors = None
    page = MagicMock()
    page.url = "https://x/video/detail/?uniqId=1"
    page.is_closed.return_value = False
    d = ActionDispatcher(page, resolver, console=MagicMock())

    stale = [{"tag": "div", "text": "部分文案"}]

    def fake_get_state(action=None, *, force_refresh=False):
        if force_refresh:
            return False, [], "", "断言失败: 无法抓取当前 tab 的实时 DOM"
        return True, stale, "部分文案", ""

    monkeypatch.setattr(d, "_get_page_state_for_assert", fake_get_state)
    monkeypatch.setattr(d, "_can_fast_assert", lambda: True)
    d._session.has_dom_cache = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(
        "core.execution.dispatcher.try_live_scoped_text",
        lambda *a, **k: (True, "live 命中: 详情"),
    )
    monkeypatch.setattr(d, "_semantic_assert", lambda *a, **k: (False, "no"))

    action = PlannedAction(
        type="assert_text",
        intent="验证进入任务详情页",
        value="详情",
    )
    ok, msg = d._assert_text(action)
    assert ok is True
    assert "详情" in msg


def test_assert_text_skips_force_refresh_when_cache_settled(monkeypatch):
    """settle 后的完整缓存未命中目标时, 直接走 live/语义, 不重抓 DOM."""
    resolver = MagicMock()
    resolver._framework_selectors = None
    page = MagicMock()
    page.url = "https://x/app/item/1"
    d = ActionDispatcher(page, resolver, console=MagicMock())

    items = [{"tag": "div", "text": "任务信息"}] * 130
    calls: list[bool] = []

    def fake_get_state(action=None, *, force_refresh=False):
        calls.append(force_refresh)
        from core.dom.semantic_dom import compact_dom_lines
        return True, items, compact_dom_lines(items), ""

    monkeypatch.setattr(d, "_get_page_state_for_assert", fake_get_state)
    monkeypatch.setattr(d, "_can_fast_assert", lambda: True)
    d._session.has_dom_cache = lambda: True  # type: ignore[method-assign]
    d._session.page_state = {
        "key": "https://x/app/item/1",
        "semantic_items": items,
        "settled": True,
    }
    monkeypatch.setattr(
        "core.execution.dispatcher.try_live_scoped_text",
        lambda *a, **k: (True, "live页面搜索: 含 '详情'"),
    )
    monkeypatch.setattr(d, "_semantic_assert", lambda *a, **k: (False, "no"))

    action = PlannedAction(
        type="assert_text",
        intent="验证进入任务详情页",
        value="详情",
    )
    ok, msg = d._assert_text(action)
    assert ok is True
    assert calls == [False]

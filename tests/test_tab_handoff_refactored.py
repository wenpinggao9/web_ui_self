"""Tab Handoff 重构后测试 — 验证单一恢复入口的正确性.

使用 importlib 直接加载模块, 绕过 core.execution.__init__ 的 pydantic 兼容问题.
测试场景:
  1. 提交后详情 tab 关闭 → 回到列表页 (核心 bug 场景)
  2. 提交后 auto-load 到下一任务 (不同实体)
  3. 提交后仍在同一详情 (超时)
  4. needs_list_tab_handoff 决策
  5. finalize_submit_after_dispatch 只做读取, 不恢复 tab
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BASE = Path(__file__).parent.parent


def _load(name: str) -> object:
    """直接加载模块文件, 绕过 __init__."""
    path = BASE / "core" / "execution" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_mod_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_mod_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# 按需加载模块
def _get_script_helpers():
    if "_mod_script_helpers" not in sys.modules:
        _load("script_helpers")
    return sys.modules["_mod_script_helpers"]


def _get_tab_follow():
    if "_mod_tab_follow" not in sys.modules:
        _load("tab_follow")
    return sys.modules["_mod_tab_follow"]


def _get_submit_post_verify():
    if "_mod_submit_post_verify" not in sys.modules:
        _load("submit_post_verify")
    return sys.modules["_mod_submit_post_verify"]


def _make_page(url: str, closed: bool = False, usable: bool = True) -> MagicMock:
    """创建 mock page."""
    p = MagicMock()
    p.url = url
    p.is_closed.return_value = closed
    if closed:
        p.evaluate.side_effect = Exception("closed")
        p.wait_for_timeout.side_effect = Exception("closed")
    elif not usable:
        p.evaluate.side_effect = TimeoutError("evaluate timeout")
    else:
        p.evaluate.return_value = True
    return p


def _make_context(pages: list[MagicMock]) -> MagicMock:
    """创建 mock browser context."""
    ctx = MagicMock()
    ctx.pages = pages
    return ctx


# =====================================================================
# 场景 1: 提交后详情 tab 关闭 → 回到列表页 (核心 bug 场景)
# =====================================================================
class TestDetailTabClosedReturnToList:
    """提交后详情 tab 被关, 只剩列表 tab."""

    def test_scan_tabs_returns_list_when_detail_closed(self):
        """_scan_tabs_for_outcome 应该返回列表 tab."""
        mod = _get_tab_follow()
        list_page = _make_page("https://example.com/video/list")
        ctx = _make_context([list_page])
        with patch.object(list_page, "context", ctx):
            out, hit = mod._scan_tabs_for_outcome(
                "https://example.com/video/detail/?uniqId=123",
                list_url="https://example.com/video/list",
                hints=(list_page,),
            )
            assert out == "returned_to_list", f"Expected returned_to_list, got {out}"
            assert hit is list_page

    def test_submit_left_detail_returns_true(self):
        """submit_left_detail_context: 回到列表页应返回 True."""
        mod = _get_script_helpers()
        result = mod.submit_left_detail_context(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/list",
        )
        assert result is True

    def test_still_on_same_detail_returns_false(self):
        """still_on_same_detail_after_submit: 回到列表应返回 False."""
        mod = _get_script_helpers()
        result = mod.still_on_same_detail_after_submit(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/list",
        )
        assert result is False

    def test_finalize_is_readonly(self):
        """finalize_submit_after_dispatch 只做读取, 不调用恢复函数."""
        mod = _get_submit_post_verify()
        dead_page = _make_page(
            "https://example.com/video/detail/?uniqId=123", closed=True,
        )
        result = mod.finalize_submit_after_dispatch(
            dead_page,
            {
                "navigation_outcome": "settled",
                "url_before": "https://example.com/video/detail/?uniqId=123",
                "submit_click_ok": True,
            },
        )
        assert result is not None
        assert result.meta is not None
        assert result.page is dead_page


# =====================================================================
# 场景 2: 提交后 auto-load 到下一任务 (不同实体)
# =====================================================================
class TestAutoLoadNextTask:
    """提交后自动加载到下一个任务详情."""

    def test_still_on_same_detail_different_entity(self):
        """不同 uniqId → 不算同一个详情."""
        mod = _get_script_helpers()
        result = mod.still_on_same_detail_after_submit(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/detail/?uniqId=456",
        )
        assert result is False

    def test_submit_left_detail_different_entity(self):
        """换到另一个详情 → 不算"离开详情上下文"."""
        mod = _get_script_helpers()
        result = mod.submit_left_detail_context(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/detail/?uniqId=456",
        )
        assert result is False

    def test_is_detail_url_still_true(self):
        """新 URL 仍然是详情页."""
        mod = _get_script_helpers()
        assert mod.is_detail_submission_url(
            "https://example.com/video/detail/?uniqId=456"
        ) is True


# =====================================================================
# 场景 3: 提交后仍在同一详情 (超时/未生效)
# =====================================================================
class TestStillOnSameDetail:
    """提交后仍在同一详情实体."""

    def test_still_on_same_detail_same_entity(self):
        """相同 uniqId → 同一个详情."""
        mod = _get_script_helpers()
        result = mod.still_on_same_detail_after_submit(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/detail/?uniqId=123",
        )
        assert result is True

    def test_submit_left_detail_same_entity(self):
        """仍在同一详情 → 不算离开."""
        mod = _get_script_helpers()
        result = mod.submit_left_detail_context(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/detail/?uniqId=123",
        )
        assert result is False


# =====================================================================
# 场景 4: needs_list_tab_handoff 决策
# =====================================================================
class TestNeedsListTabHandoff:
    """PageSession.needs_list_tab_handoff 决策测试."""

    def _needs_handoff(self, **kw):
        """加载 page_session 模块并调用 needs_list_tab_handoff."""
        if "_mod_page_session" not in sys.modules:
            _load("page_session")
        mod = sys.modules["_mod_page_session"]
        return mod.PageSession.needs_list_tab_handoff(kw)

    def test_left_detail_returns_true(self):
        assert self._needs_handoff(
            left_detail_context=True, submit_click_ok=True,
            url_before="https://x/detail/?id=1",
        ) is True

    def test_detail_closed_returns_true(self):
        assert self._needs_handoff(
            detail_tab_closed=True, submit_click_ok=True,
            url_before="https://x/detail/?id=1",
        ) is True

    def test_returned_to_list_returns_true(self):
        assert self._needs_handoff(
            navigation_outcome="returned_to_list", submit_click_ok=True,
            url_before="https://x/detail/?id=1",
        ) is True

    def test_resource_id_changed_no_list_handoff(self):
        assert self._needs_handoff(
            navigation_outcome="resource_id_changed",
            submit_click_ok=True,
            url_before="https://x/detail/?id=1",
            url_after="https://x/video/wait-preview",
        ) is False

    def test_settled_no_handoff(self):
        assert self._needs_handoff(
            navigation_outcome="settled", submit_click_ok=True,
            url_before="https://x/detail/?id=1",
            url_after="https://x/detail/?id=1",
        ) is False

    def test_settled_with_left_detail(self):
        assert self._needs_handoff(
            navigation_outcome="settled", left_detail_context=True,
            submit_click_ok=True, url_before="https://x/detail/?id=1",
        ) is True


# =====================================================================
# 场景 5: classify_navigation_outcome
# =====================================================================
class TestClassifyNavigationOutcome:
    """classify_navigation_outcome 分类测试."""

    def test_detail_to_list(self):
        mod = _get_script_helpers()
        result = mod.classify_navigation_outcome(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/list",
            list_url="https://example.com/video/list",
        )
        assert result == "returned_to_list"

    def test_entity_changed(self):
        mod = _get_script_helpers()
        result = mod.classify_navigation_outcome(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/detail/?uniqId=456",
        )
        assert result == "resource_id_changed"

    def test_same_detail_no_change(self):
        mod = _get_script_helpers()
        result = mod.classify_navigation_outcome(
            "https://example.com/video/detail/?uniqId=123",
            "https://example.com/video/detail/?uniqId=123",
        )
        assert result is None


# =====================================================================
# 场景 6: submit_dispatch_should_succeed
# =====================================================================
class TestSubmitDispatchShouldSucceed:
    """submit_dispatch_should_succeed 决策测试."""

    def _should_succeed(self, outcome: str, **kw) -> bool:
        mod = _get_submit_post_verify()
        meta = {
            "navigation_outcome": outcome,
            "url_before": kw.get("url_before", ""),
            "submit_click_ok": kw.get("submit_click_ok", True),
            "left_detail_context": kw.get("left_detail", False),
            "detail_tab_closed": kw.get("detail_closed", False),
            "recovered": kw.get("recovered", False),
        }
        return mod.submit_dispatch_should_succeed(meta)

    def test_returned_to_list(self):
        assert self._should_succeed("returned_to_list") is True

    def test_resource_id_changed(self):
        assert self._should_succeed("resource_id_changed") is True

    def test_route_changed(self):
        assert self._should_succeed("route_changed") is True

    def test_settled_with_left_detail(self):
        assert self._should_succeed(
            "settled", left_detail=True, url_before="https://x/detail/?id=1"
        ) is True

    def test_submit_error(self):
        assert self._should_succeed("submit_error") is False

    def test_settled_no_detail_leave(self):
        assert self._should_succeed(
            "settled", url_before="https://x/detail/?id=1"
        ) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

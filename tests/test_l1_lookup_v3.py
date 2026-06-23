"""L1 lookup: V3 自愈 + node_signature + URL 切换."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.locating.cache import SelectorCache


def _mock_page(valid_selectors: set[str]):
    page = MagicMock()

    def _locator(sel: str):
        loc = MagicMock()
        loc.count.return_value = 1 if sel in valid_selectors else 0
        loc.first.is_visible.return_value = sel in valid_selectors
        return loc

    page.locator.side_effect = _locator
    return page


def test_put_stores_node_signature():
    cache = SelectorCache(ttl_s=1800)
    cache.put(
        "https://x.com/p",
        "click",
        "点提交",
        {"selector": "#btn", "tag": "button", "text": "提交"},
        node={"tag": "button", "text": "提交", "class": "el-button primary"},
    )
    k = cache._key("https://x.com/p", "click", "点提交")
    entry = cache._data[k]
    assert entry["node_signature"]["tag"] == "button"
    assert entry["node_signature"]["text"] == "提交"
    assert "page_url" in entry


def test_lookup_self_heal_by_text_fuzzy(monkeypatch):
    cache = SelectorCache(ttl_s=1800, self_heal=True)
    cache.put(
        "https://x.com/p",
        "click",
        "点提交",
        {"selector": "#old-btn"},
        node={"tag": "button", "text": "提交按钮", "class": ""},
    )
    page = _mock_page({'button:has-text("提交按钮")'})

    monkeypatch.setattr(
        "core.locating.cache.validate_selector",
        lambda _p, info, timeout_ms=1500: False,
    )

    info = cache.lookup(page, "https://x.com/p", "click", "点提交")
    assert info is not None
    assert info.get("_from_cache_heal") is True
    assert 'has-text("提交按钮")' in info["selector"]


def test_url_change_only_evicts_expired_on_old_page():
    cache = SelectorCache(ttl_s=60)
    old_url = "https://x.com/page-a"
    new_url = "https://x.com/page-b"
    cache.put(old_url, "click", "按钮A", {"selector": "#a"})
    cache.put(new_url, "click", "按钮B", {"selector": "#b"})

    expired_key = "/page-a|click|过期项"
    cache._data[expired_key] = {
        "selector": "#expired",
        "page_url": old_url,
        "ts": time.time() - 120,
    }
    cache._current_page_url = "/page-a"

    cache._maybe_invalidate_on_url_change(new_url)

    assert cache.get(old_url, "click", "按钮A") is not None
    assert expired_key not in cache._data
    assert cache.get(new_url, "click", "按钮B") is not None

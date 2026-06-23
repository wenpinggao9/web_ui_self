"""L1 选择器缓存: TTL + 落盘 (对齐 V3)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from core.execution.dispatcher import ActionDispatcher  # noqa: F401

from core.locating.cache import SelectorCache


def test_load_legacy_flat_format(tmp_path: Path):
    legacy = {
        "/video/all-question\u0001click\u0001点击提交按钮": {
            "selector": "button:has-text(\"提 交\")",
            "nth": 0,
            "ts": time.time(),
        }
    }
    p = tmp_path / "选择器缓存.json"
    p.write_text(json.dumps(legacy), encoding="utf-8")

    cache = SelectorCache(ttl_s=1800, path=p)
    assert cache.size == 1
    info = cache.get("https://x.com/#/video/all-question", "click", "点击提交按钮")
    assert info is not None
    assert "提 交" in info["selector"]


def test_save_load_roundtrip_skips_expired(tmp_path: Path):
    p = tmp_path / "cache.json"
    cache = SelectorCache(ttl_s=60, path=None)
    cache.put("https://x.com/video/all-question", "click", "点击A", {"selector": "#a"})
    cache._data["/video/all-question|click|点击b"] = {
        "selector": "#b",
        "ts": time.time() - 120,
    }
    cache.path = p
    cache.save()

    cache2 = SelectorCache(ttl_s=60, path=p)
    assert cache2.size == 1
    assert cache2.get("https://x.com/video/all-question", "click", "点击A") is not None
    assert cache2.get("https://x.com/video/all-question", "click", "点击B") is None

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert "entries" in raw


def test_touch_refreshes_timestamp(tmp_path: Path):
    cache = SelectorCache(ttl_s=120, path=None)
    cache.put("https://x.com/p", "click", "点按钮", {"selector": "#btn"})
    k = cache._key("https://x.com/p", "click", "点按钮")
    old_ts = cache._data[k]["ts"]
    time.sleep(0.01)
    cache.touch("https://x.com/p", "click", "点按钮")
    assert cache._data[k]["ts"] > old_ts
    assert cache._data[k]["hit_count"] == 1

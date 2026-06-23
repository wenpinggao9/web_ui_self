"""智能加速 V3 目录布局与旧文件迁移."""
from __future__ import annotations

import json
from pathlib import Path

from core.locating.accel_paths import (
    migrate_legacy_accel_layout,
    selector_cache_path,
    selector_memory_path,
)


def test_migrate_legacy_flat_files(tmp_path):
    legacy_cache = tmp_path / "选择器缓存.json"
    legacy_memory = tmp_path / "选择器记忆库.json"
    legacy_cache.write_text(
        json.dumps({"version": 1, "entries": {"k": {"selector": "#a", "ts": 1}}}),
        encoding="utf-8",
    )
    legacy_memory.write_text(
        json.dumps({"version": 1, "page_entries": {}, "generic_entries": {}}),
        encoding="utf-8",
    )

    moved = migrate_legacy_accel_layout(tmp_path)
    assert len(moved) == 2
    assert not legacy_cache.exists()
    assert not legacy_memory.exists()
    assert selector_cache_path(tmp_path).is_file()
    assert selector_memory_path(tmp_path).is_file()
    assert json.loads(selector_cache_path(tmp_path).read_text())["entries"]["k"]["selector"] == "#a"


def test_migrate_skips_when_new_path_exists(tmp_path):
    legacy = tmp_path / "选择器缓存.json"
    legacy.write_text("{}", encoding="utf-8")
    target = selector_cache_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text('{"version":1,"entries":{}}', encoding="utf-8")

    moved = migrate_legacy_accel_layout(tmp_path)
    assert moved == []
    assert legacy.is_file()
    assert target.is_file()

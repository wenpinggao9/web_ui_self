"""智能加速层落盘路径 (V3 子目录布局) + 旧版平铺文件迁移."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

SELECTOR_CACHE_DIR = "selector_cache"
SELECTOR_CACHE_FILE = "selector_cache.json"
SELECTOR_MEMORY_DIR = "selector_memory"
SELECTOR_MEMORY_FILE = "selector_memory.json"
PAGE_STRUCTURE_DIR = "page_structure_learner"
PAGE_STRUCTURE_FILE = "page_structure_learner.json"

_LEGACY_SELECTOR_CACHE = "选择器缓存.json"
_LEGACY_SELECTOR_MEMORY = "选择器记忆库.json"


def selector_cache_path(accel_dir: str | Path) -> Path:
    root = Path(accel_dir)
    return root / SELECTOR_CACHE_DIR / SELECTOR_CACHE_FILE


def selector_memory_path(accel_dir: str | Path) -> Path:
    root = Path(accel_dir)
    return root / SELECTOR_MEMORY_DIR / SELECTOR_MEMORY_FILE


def page_structure_path(accel_dir: str | Path) -> Path:
    root = Path(accel_dir)
    return root / PAGE_STRUCTURE_DIR / PAGE_STRUCTURE_FILE


def migrate_legacy_accel_layout(accel_dir: str | Path) -> list[str]:
    """将 智能加速/ 根目录旧 JSON 迁入 V3 子目录 (内容原样保留)."""
    root = Path(accel_dir)
    moved: list[str] = []
    pairs = (
        (root / _LEGACY_SELECTOR_CACHE, selector_cache_path(root)),
        (root / _LEGACY_SELECTOR_MEMORY, selector_memory_path(root)),
    )
    for legacy, target in pairs:
        if not legacy.is_file():
            continue
        if target.is_file():
            logger.info("智能加速迁移跳过(新路径已存在): %s", target.name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
        moved.append(f"{legacy.name} → {target.relative_to(root)}")
        logger.info("智能加速已迁移: %s → %s", legacy, target)
    return moved

"""步骤⑨ 第2级 选择器记忆库 (L2).

持久化到文件, 跨批次复用. 成功 +1 / 失败 -1, 降到 0 删除.
向 V3 看齐: node_signature 节点签名、component_library 框架识别、
page_entries/generic_entries 双存储、压缩淘汰 stale 条目.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from .normalize import normalize_intent, normalize_url
from .playwright_api import info_key, normalize_info

# 压缩阈值: success_count <= 1 且 created_at 超过此秒数则删除
_COMPRESS_STALE_SECONDS = 14 * 24 * 3600  # 14 天
_FILE_VERSION = 1


def _build_node_signature(node: Optional[dict]) -> dict[str, str]:
    """从 semantic_dom 节点提取特征签名, 用于失效检测."""
    if not node:
        return {}
    tag = str(node.get("tag") or "").lower()
    text = str(node.get("text") or "").strip()[:80]
    raw_class = str(node.get("class") or "")
    class_parts = [
        c for c in raw_class.split()
        if c and not c.startswith("_") and len(c) > 1
    ]
    class_pattern = " ".join(class_parts[:2]) if class_parts else ""
    role = str(node.get("role") or "").strip()
    return {
        "tag": tag,
        "text": text,
        "role": role,
        "class_pattern": class_pattern,
    }


def _detect_component_library(items: list[dict]) -> str:
    """扫描 DOM 的 class 前缀识别组件库."""
    prefix_counts: dict[str, int] = {}
    for it in items:
        raw = str(it.get("class") or "")
        for cls in raw.split():
            if "-" in cls and len(cls) >= 3:
                prefix = cls.split("-")[0].lower()
                prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
    if not prefix_counts:
        return "generic"
    top = max(prefix_counts, key=prefix_counts.get)
    mapping = {
        "el": "element-ui", "elx": "element-plus", "ant": "ant-design",
        "van": "vant", "ivu": "iview", "iv": "iview",
        "mui": "material-ui", "chakra": "chakra-ui",
        "nb": "ng-bootstrap", "mat": "angular-material", "p": "prime-ng",
    }
    return mapping.get(top, "generic")


class SelectorMemory:
    """中期记忆库: 文件持久化 + 评分 + 压缩清理."""

    def __init__(self, path: str | Path, ttl_s: int = 0) -> None:
        del ttl_s  # 不再用 TTL, 改用压缩淘汰
        self.path = Path(path)
        self._store: dict[str, dict] = {}       # page_entries
        self._generic_store: dict[str, dict] = {}  # generic_entries
        self._load()
        self._compress()

    # ── Key 构建 ──────────────────────────────────────────────

    def _key(self, url: str, action_type: str, intent: str) -> str:
        return f"{normalize_url(url)}|{action_type}|{normalize_intent(intent)}"

    # ── 页面级查找 ─────────────────────────────────────────────

    def get(self, url: str, action_type: str, intent: str) -> Optional[dict]:
        """页面级查找. 返回规范化 info dict."""
        k = self._key(url, action_type, intent)
        e = self._store.get(k)
        if not e or e.get("success_count", 0) <= 0:
            return None
        return normalize_info(e)

    def lookup_validate(
        self,
        page: Any,
        url: str,
        action_type: str,
        intent: str,
    ) -> Optional[dict]:
        """原子操作: 查找 + 验证 + 加分.

        命中 → 验证 selector 在当前页面是否匹配可见元素:
          - 有效: success_count +1, 返回 info
          - 无效: success_count -1, 返回 None (降到 0 自动删除)
        """
        info = self.get(url, action_type, intent)
        if info is None:
            return None

        selector = info.get("selector", "")
        if not selector or not page:
            self._decrement(url, action_type, intent)
            return None

        if self._validate_selector(page, selector):
            # 验证通过 → 加分
            k = self._key(url, action_type, intent)
            e = self._store[k]
            e["success_count"] = min(e.get("success_count", 1) + 1, 100)
            e["updated_at"] = time.time()
            return normalize_info(e)
        else:
            # 验证失败 → 减分
            self._decrement(url, action_type, intent)
            return None

    # ── 页面级写入 ─────────────────────────────────────────────

    def record_success(
        self,
        url: str,
        action_type: str,
        intent: str,
        info: dict,
        *,
        node: Optional[dict] = None,
        component_library: str = "unknown",
    ) -> None:
        """写入或更新页面级条目 (兼容旧 API).

        如果 key 已存在且 selector 相同 → 不重复加分 (lookup 已加过).
        如果 key 已存在但 selector 不同 → 覆盖并重置 score=1.
        如果是新 key → 创建条目 score=1.
        """
        k = self._key(url, action_type, intent)
        spec = normalize_info(info)
        now = time.time()
        selector = spec.get("selector", "")
        e = self._store.get(k)

        if e and e.get("selector") == selector:
            # 同一 selector → lookup 已加分, 只更新时间
            e["updated_at"] = now
        else:
            # 新条目或 selector 不同 → 覆盖重置
            sig = _build_node_signature(node)
            self._store[k] = {
                **spec,
                "success_count": 1,
                "created_at": now,
                "updated_at": now,
                "component_library": component_library,
                "node_signature": sig,
            }

    def record_failure(
        self,
        url: str,
        action_type: str,
        intent: str,
        selector: Optional[str] = None,
    ) -> None:
        """success_count -1; 降到 0 则删除条目."""
        k = self._key(url, action_type, intent)
        e = self._store.get(k)
        if not e:
            return
        if selector and e.get("selector") != selector:
            return
        self._decrement(url, action_type, intent)

    # ── 组件级写入 ─────────────────────────────────────────────

    def put_generic(
        self,
        component_library: str,
        component_type: str,
        selector_template: str,
        selector_type: str = "css",
    ) -> None:
        """写入组件级通用模式 (如 ant-design 的 radio wrapper)."""
        if not selector_template or not component_library or not component_type:
            return
        k = f"{component_library}|{component_type}"
        entry = self._generic_store.get(k)
        now = time.time()
        if entry and entry.get("selector_template") == selector_template:
            entry["success_count"] = entry.get("success_count", 0) + 1
            entry["updated_at"] = now
        else:
            self._generic_store[k] = {
                "selector_template": selector_template,
                "selector_type": selector_type,
                "component_library": component_library,
                "component_type": component_type,
                "success_count": 1,
                "created_at": now,
                "updated_at": now,
            }

    def get_generic(
        self,
        component_library: str,
        component_type: str,
    ) -> Optional[dict]:
        """查找组件级通用模式."""
        k = f"{component_library}|{component_type}"
        return self._generic_store.get(k)

    # ── 内部方法 ───────────────────────────────────────────────

    def _decrement(self, url: str, action_type: str, intent: str) -> None:
        """success_count -1, 降到 0 删除."""
        k = self._key(url, action_type, intent)
        e = self._store.get(k)
        if not e:
            return
        e["success_count"] = e.get("success_count", 1) - 1
        e["updated_at"] = time.time()
        if e["success_count"] <= 0:
            self._store.pop(k, None)

    @staticmethod
    def _validate_selector(page: Any, selector: str) -> bool:
        """验证 selector 在当前页面是否匹配到至少 1 个可见元素."""
        if not page or not selector:
            return False
        try:
            loc = page.locator(selector)
            if loc.count() >= 1:
                return bool(loc.first.is_visible())
            return False
        except Exception:
            return False

    def _compress(self) -> None:
        """删除 success_count <= 1 且 created_at 超过 14 天的条目."""
        now = time.time()
        stale_threshold = now - _COMPRESS_STALE_SECONDS

        stale_keys = [
            k for k, v in self._store.items()
            if v.get("success_count", 0) <= 1
            and v.get("created_at", 0) < stale_threshold
        ]
        for k in stale_keys:
            del self._store[k]

        stale_generic = [
            k for k, v in self._generic_store.items()
            if v.get("success_count", 0) <= 1
            and v.get("created_at", 0) < stale_threshold
        ]
        for k in stale_generic:
            del self._generic_store[k]

    # ── 持久化 ─────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)

            # 新格式
            if "page_entries" in data:
                for k, v in data.get("page_entries", {}).items():
                    if v.get("success_count", 0) > 0:
                        self._store[k] = v
                for k, v in data.get("generic_entries", {}).items():
                    if v.get("success_count", 0) > 0:
                        self._generic_store[k] = v
            else:
                # 旧格式迁移: 平铺 → page_entries
                for k, v in data.items():
                    if not isinstance(v, dict):
                        continue
                    # 旧字段名 → 新字段名
                    if "score" in v and "success_count" not in v:
                        v["success_count"] = v.pop("score")
                    if "ts" in v and "created_at" not in v:
                        ts = v.pop("ts")
                        v["created_at"] = ts
                        v["updated_at"] = ts
                    if "component_library" not in v:
                        v["component_library"] = "unknown"
                    if "node_signature" not in v:
                        v["node_signature"] = {}
                    if v.get("success_count", 0) > 0:
                        self._store[k] = v
        except Exception:
            pass

    def save(self) -> None:
        self._compress()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": _FILE_VERSION,
            "saved_at": time.time(),
            "page_entries": self._store,
            "generic_entries": self._generic_store,
        }
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

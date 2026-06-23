"""步骤⑨ 第1级 选择器缓存 (L1).

短期缓存 (对齐 V3 selector_cache):
- 键 = 归一化URL | 动作类型 | 归一化意图 (cache 风格: 小写+单空格)
- TTL 默认 30 分钟; 落盘到 JSON; 命中后续期.
- lookup() 内建校验 + V3 自愈 (text fuzzy / signature / XPath) + 独立 heal() 兜底
- 存储 node_signature; URL 切换时仅清除旧页过期条目 (回退可复用)
- 兼容历史 \\x01 分隔与旧版 intent 归一化 key.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from .memory import _build_node_signature
from .normalize import (
    normalize_intent_cache,
    normalize_intent_legacy,
    normalize_url,
    normalize_url_legacy,
    validate_selector,
)
from .playwright_api import normalize_info

logger = logging.getLogger(__name__)

_DEFAULT_TTL_S = 30 * 60
_FILE_VERSION = 1
_KEY_SEP = "|"


class SelectorCache:
    """短期选择器缓存: 同批次/跨批次 (30min TTL 内) 快速复用已验证定位结果."""

    def __init__(
        self,
        ttl_s: int = _DEFAULT_TTL_S,
        path: str | Path | None = None,
        *,
        self_heal: bool = True,
    ) -> None:
        self.ttl_s = ttl_s
        self.path = Path(path) if path else None
        self.self_heal = self_heal
        self._data: dict[str, dict[str, Any]] = {}
        self._current_page_url: str = ""
        self._stats_lookups = 0
        self._stats_hits = 0
        self._stats_self_heals = 0
        if self.path:
            self.load()

    def _key(self, url: str, action_type: str, intent: str) -> str:
        return (
            f"{normalize_url(url)}{_KEY_SEP}{action_type}{_KEY_SEP}"
            f"{normalize_intent_cache(intent)}"
        )

    def _legacy_keys(self, url: str, action_type: str, intent: str) -> list[str]:
        legacy_intent = normalize_intent_legacy(intent)
        keys = [
            f"{normalize_url_legacy(url)}\u0001{action_type}\u0001{legacy_intent}",
            f"{normalize_url(url)}\u0001{action_type}\u0001{legacy_intent}",
        ]
        primary = self._key(url, action_type, intent)
        out: list[str] = [primary]
        seen = {primary}
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    def _expired(self, entry: dict) -> bool:
        ts = entry.get("ts") or entry.get("timestamp") or 0
        return time.time() - float(ts) > self.ttl_s

    def _prune_expired(self) -> None:
        stale = [k for k, v in self._data.items() if self._expired(v)]
        for k in stale:
            del self._data[k]

    def _find_entry_key(self, url: str, action_type: str, intent: str) -> Optional[str]:
        for k in self._legacy_keys(url, action_type, intent):
            entry = self._data.get(k)
            if entry and not self._expired(entry):
                return k
        return None

    def get(self, url: str, action_type: str, intent: str) -> Optional[dict]:
        self._prune_expired()
        k = self._find_entry_key(url, action_type, intent)
        if not k:
            return None
        return normalize_info(self._data[k])

    def lookup(
        self,
        page: Any,
        url: str,
        action_type: str,
        intent: str,
    ) -> Optional[dict]:
        """查询 L1: 校验 → V3 内建自愈 → heal() 兜底 → 失败则 evict."""
        self._stats_lookups += 1
        self._maybe_invalidate_on_url_change(url)
        self._prune_expired()
        k = self._find_entry_key(url, action_type, intent)
        if not k:
            return None

        entry = self._data[k]
        info = normalize_info(entry)

        if validate_selector(page, info):
            self._stats_hits += 1
            self.touch(url, action_type, intent)
            return info

        logger.info("L1 校验失败, 尝试自愈 | selector=%s", info.get("selector", "")[:120])

        if self.self_heal:
            healed_sel = self._try_self_heal(page, entry)
            if healed_sel:
                from .skill_resolver import info_from_recommended_selector

                healed_info = info_from_recommended_selector(healed_sel)
                if validate_selector(page, healed_info):
                    self._update_entry(k, url, action_type, intent, healed_info, entry)
                    healed_info["_from_cache_heal"] = True
                    self._stats_self_heals += 1
                    logger.info("L1 V3自愈命中 | selector=%s", healed_sel[:120])
                    return healed_info

        from .self_heal import heal

        healed = heal(page, info)
        if healed and validate_selector(page, healed):
            self.put(url, action_type, intent, healed, node=healed)
            healed["_from_cache_heal"] = True
            self._stats_self_heals += 1
            logger.info("L1 heal() 命中 | selector=%s", healed.get("selector", "")[:120])
            return healed

        self.evict(url, action_type, intent)
        logger.info("L1 自愈失败, 已 evict | intent=%s", intent[:60])
        return None

    def _update_entry(
        self,
        store_key: str,
        url: str,
        action_type: str,
        intent: str,
        info: dict,
        prev: dict,
    ) -> None:
        spec = normalize_info(info)
        primary = self._key(url, action_type, intent)
        self._data[primary] = {
            **spec,
            "node_signature": prev.get("node_signature") or {},
            "page_url": url,
            "ts": time.time(),
            "hit_count": int(prev.get("hit_count") or 0) + 1,
        }
        if store_key != primary:
            self._data.pop(store_key, None)

    def touch(self, url: str, action_type: str, intent: str) -> None:
        """L1 命中且页面校验通过后刷新时间戳 (对齐 V3 命中续期)."""
        k = self._find_entry_key(url, action_type, intent)
        if not k:
            return
        entry = self._data[k]
        entry["ts"] = time.time()
        entry["hit_count"] = int(entry.get("hit_count") or 0) + 1
        primary = self._key(url, action_type, intent)
        if k != primary:
            self._data[primary] = entry
            del self._data[k]

    def put(
        self,
        url: str,
        action_type: str,
        intent: str,
        info: dict,
        node: Optional[dict] = None,
    ) -> None:
        spec = normalize_info(info)
        primary = self._key(url, action_type, intent)
        prev = self._data.get(primary) or {}
        for alt in self._legacy_keys(url, action_type, intent):
            if alt != primary:
                prev = prev or self._data.get(alt) or {}
                self._data.pop(alt, None)
        sig = _build_node_signature(node) if node else {}
        if not sig:
            sig = prev.get("node_signature") or _build_node_signature(info)
        self._data[primary] = {
            **spec,
            "node_signature": sig,
            "page_url": url,
            "ts": time.time(),
            "hit_count": int(prev.get("hit_count") or 0),
        }
        self._current_page_url = normalize_url(url)
        self._prune_expired()

    def evict(self, url: str, action_type: str, intent: str) -> None:
        for k in self._legacy_keys(url, action_type, intent):
            self._data.pop(k, None)

    # ── V3 自愈策略 ──────────────────────────────────────────

    def _try_self_heal(self, page: Any, entry: dict) -> Optional[str]:
        selector = entry.get("selector") or ""
        sig = entry.get("node_signature") or {}

        healed = self._heal_by_text_fuzzy(page, selector, sig)
        if healed:
            return healed
        healed = self._heal_by_signature_search(page, sig)
        if healed:
            return healed
        return self._heal_by_xpath_fallback(page, selector, sig)

    def _heal_by_text_fuzzy(
        self, page: Any, selector: str, sig: dict[str, str],
    ) -> Optional[str]:
        text = sig.get("text", "")
        tag = sig.get("tag", "")
        if not text or len(text) < 2:
            return None
        candidates = [text]
        if len(text) > 4:
            candidates.append(text[:-2])
        if len(text) > 6:
            candidates.append(text[:-4])
        for t in candidates:
            if not t.strip():
                continue
            candidate = f'{tag}:has-text("{t}")' if tag else f':has-text("{t}")'
            if self._validate_selector_str(page, candidate):
                return candidate
        return None

    def _heal_by_signature_search(
        self, page: Any, sig: dict[str, str],
    ) -> Optional[str]:
        tag = sig.get("tag", "")
        text = sig.get("text", "")
        class_pattern = sig.get("class_pattern", "")
        if not tag and not text:
            return None
        if class_pattern and tag:
            for cls in class_pattern.split():
                if not cls or len(cls) < 2:
                    continue
                candidate = f"{tag}.{cls}"
                if self._validate_selector_str(page, candidate):
                    return candidate
        if tag and text and len(text) >= 2:
            short_text = text[: max(len(text) // 2, 4)]
            escaped = self._escape_xpath_string(short_text)
            candidate = f"//{tag}[contains(normalize-space(.), {escaped})]"
            if self._validate_selector_str(page, candidate):
                return candidate
        return None

    def _heal_by_xpath_fallback(
        self, page: Any, selector: str, sig: dict[str, str],
    ) -> Optional[str]:
        if not selector or selector.startswith(("/", "(")):
            return None
        text = sig.get("text", "")
        tag = sig.get("tag", "")
        if not text or not tag:
            return None
        escaped = self._escape_xpath_string(text)
        for candidate in (
            f"//{tag}[normalize-space(.)={escaped}]",
            f"//{tag}[contains(normalize-space(.), {escaped})]",
        ):
            if self._validate_selector_str(page, candidate):
                return candidate
        return None

    @staticmethod
    def _validate_selector_str(page: Any, selector: str) -> bool:
        if not page or not selector:
            return False
        try:
            loc = page.locator(selector)
            if loc.count() >= 1:
                return bool(loc.first.is_visible())
            return False
        except Exception:
            return False

    @staticmethod
    def _escape_xpath_string(s: str) -> str:
        if '"' not in s:
            return f'"{s}"'
        if "'" not in s:
            return f"'{s}'"
        parts = s.split('"')
        return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"

    def _maybe_invalidate_on_url_change(self, current_url: str) -> None:
        """URL 切换时仅清除旧页已过期条目, 保留其它页缓存供回退复用."""
        if not current_url:
            return
        current_path = normalize_url(current_url)
        if self._current_page_url and self._current_page_url != current_path:
            old_path = self._current_page_url
            now = time.time()
            keys_to_remove = [
                k for k, v in self._data.items()
                if v.get("page_url") and normalize_url(v["page_url"]) == old_path
                and now - float(v.get("ts") or v.get("timestamp") or 0) > self.ttl_s
            ]
            for k in keys_to_remove:
                self._data.pop(k, None)
            if keys_to_remove:
                logger.info(
                    "L1 URL切换 | old=%s | current=%s | evicted=%d",
                    old_path, current_path, len(keys_to_remove),
                )
        self._current_page_url = current_path

    @property
    def stats(self) -> dict[str, Any]:
        lookups = self._stats_lookups or 1
        return {
            "lookups": self._stats_lookups,
            "hits": self._stats_hits,
            "hit_rate": round(self._stats_hits / lookups * 100, 1),
            "self_heals": self._stats_self_heals,
            "entries": len(self._data),
        }

    @property
    def size(self) -> int:
        return len(self._data)

    def load(self, path: str | Path | None = None) -> int:
        """从 JSON 加载历史缓存, 跳过已过期条目. 返回加载条数."""
        p = Path(path) if path else self.path
        if not p or not p.exists():
            return 0
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("selector_cache 加载失败: %s", exc)
            return 0

        entries = self._parse_file_entries(raw)
        loaded = 0
        for k, v in entries.items():
            if not isinstance(v, dict):
                continue
            if self._expired(v):
                continue
            if "ts" not in v and "timestamp" in v:
                v = {**v, "ts": v["timestamp"]}
            self._data[k] = v
            loaded += 1
        logger.info("selector_cache 已加载 | file=%s | valid=%d", p, loaded)
        return loaded

    def save(self, path: str | Path | None = None) -> None:
        """持久化到 JSON (仅当配置了 path 时)."""
        p = Path(path) if path else self.path
        if not p:
            return
        self._prune_expired()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": _FILE_VERSION,
                "saved_at": time.time(),
                "entries": self._data,
            }
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("selector_cache 已保存 | file=%s | entries=%d", p, len(self._data))
        except Exception as exc:
            logger.warning("selector_cache 保存失败: %s", exc)

    @staticmethod
    def _parse_file_entries(raw: Any) -> dict[str, dict]:
        """支持新版 {version, entries} 与旧版平铺 dict."""
        if not isinstance(raw, dict):
            return {}
        if "entries" in raw and isinstance(raw["entries"], dict):
            return raw["entries"]
        out: dict[str, dict] = {}
        for k, v in raw.items():
            if k in ("version", "saved_at"):
                continue
            if isinstance(v, dict) and ("selector" in v or "method" in v):
                out[str(k)] = v
        return out

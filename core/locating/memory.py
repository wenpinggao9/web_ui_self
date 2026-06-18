"""步骤⑨ 第2级 选择器记忆库 (L2).

持久化到文件, 跨批次复用. 成功 +1 / 失败 -1, 分到 0 删除; 默认 TTL 10 天, 超时自动清理.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .normalize import normalize_intent, normalize_url
from .playwright_api import info_key, normalize_info

_DEFAULT_TTL_S = 10 * 24 * 3600


class SelectorMemory:
    """中期记忆库: 文件持久化 + 评分 + 过期清理."""

    def __init__(self, path: str | Path, ttl_s: int = _DEFAULT_TTL_S) -> None:
        self.path = Path(path)
        self.ttl_s = ttl_s
        self._data: dict[str, dict] = {}
        self._load()
        self._prune_expired()

    def _key(self, url: str, action_type: str, intent: str) -> str:
        return f"{normalize_url(url)}\u0001{action_type}\u0001{normalize_intent(intent)}"

    def _expired(self, entry: dict) -> bool:
        ts = entry.get("ts")
        if ts is None:
            return True
        return time.time() - float(ts) > self.ttl_s

    def _prune_expired(self) -> None:
        stale = [k for k, v in self._data.items() if self._expired(v) or v.get("score", 0) <= 0]
        for k in stale:
            del self._data[k]

    def get(self, url: str, action_type: str, intent: str) -> Optional[dict]:
        k = self._key(url, action_type, intent)
        e = self._data.get(k)
        if not e or e.get("score", 0) <= 0:
            return None
        if self._expired(e):
            del self._data[k]
            return None
        return normalize_info(e)

    def record_success(self, url: str, action_type: str, intent: str, info: dict) -> None:
        k = self._key(url, action_type, intent)
        spec = normalize_info(info)
        now = time.time()
        e = self._data.get(k)
        if e and info_key(e) != info_key(spec):
            e = None
        if not e:
            e = {**spec, "score": 0, "ts": now}
        e["score"] = min(e.get("score", 0) + 1, 100)
        e["ts"] = now
        self._data[k] = e

    def record_failure(self, url: str, action_type: str, intent: str, selector: Optional[str]) -> None:
        k = self._key(url, action_type, intent)
        e = self._data.get(k)
        if not e or (selector and e.get("selector") != selector):
            return
        e["score"] = e.get("score", 0) - 1
        if e["score"] <= 0:
            self._data.pop(k, None)
        else:
            self._data[k] = e

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def save(self) -> None:
        self._prune_expired()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

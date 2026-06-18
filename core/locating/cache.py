"""步骤⑨ 第1级 选择器缓存 (L1).

纯内存缓存, 进程结束即失效. 键 = (归一化URL, 动作类型, 归一化意图); 默认 TTL 30 分钟.
命中 → 校验可用 → 返回; 命中但失效 → 清除条目 → 降级.
"""
from __future__ import annotations

import time
from typing import Optional

from .normalize import normalize_intent, normalize_url
from .playwright_api import normalize_info

_DEFAULT_TTL_S = 30 * 60


class SelectorCache:
    """短期内存缓存: 同一次 python run 内快速复用刚验证过的定位结果."""

    def __init__(self, ttl_s: int = _DEFAULT_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._data: dict[str, dict] = {}

    def _key(self, url: str, action_type: str, intent: str) -> str:
        return f"{normalize_url(url)}\u0001{action_type}\u0001{normalize_intent(intent)}"

    def _expired(self, entry: dict) -> bool:
        return time.time() - entry.get("ts", 0) > self.ttl_s

    def _prune_expired(self) -> None:
        stale = [k for k, v in self._data.items() if self._expired(v)]
        for k in stale:
            del self._data[k]

    def get(self, url: str, action_type: str, intent: str) -> Optional[dict]:
        k = self._key(url, action_type, intent)
        entry = self._data.get(k)
        if not entry:
            return None
        if self._expired(entry):
            del self._data[k]
            return None
        return normalize_info(entry)

    def put(self, url: str, action_type: str, intent: str, info: dict) -> None:
        spec = normalize_info(info)
        self._data[self._key(url, action_type, intent)] = {**spec, "ts": time.time()}
        self._prune_expired()

    def evict(self, url: str, action_type: str, intent: str) -> None:
        self._data.pop(self._key(url, action_type, intent), None)

    def save(self) -> None:
        """兼容旧调用: L1 不落盘, 无操作."""

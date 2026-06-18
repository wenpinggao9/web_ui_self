"""步骤⑨ 第4级 页面结构学习 (L4).

从成功记录学习 (路由, 动作类型, 意图) → 选择器模板.
相似页面 (同路由+同动作类型) 下, 意图 Jaccard 相似度 > 0.6 可复用选择器.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .normalize import normalize_url, validate_selector

_THRESHOLD = 0.6
# 中文按单字切分, 英文/数字按词切分, 兼容中英文混写意图.
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+")


def _tokens(intent: str) -> set[str]:
    """把意图转成可比较的 token 集合, 用于相似度匹配."""
    s = re.sub(r"[\"'“”‘’\s]", "", intent or "")
    toks = set(_CJK.findall(s)) | set(w.lower() for w in _WORD.findall(s))
    return toks


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度: 交集越大, 两个意图越可能复用同类选择器."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class StructureLearner:
    """记录页面结构中的成功选择器, 在相似意图下尝试复用."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._records: list[dict] = []
        self._load()

    def learn(self, url: str, action_type: str, intent: str, info: dict) -> None:
        # 学习维度控制在 route + action_type + intent, 避免跨页面误复用.
        route = normalize_url(url)
        toks = list(_tokens(intent))
        # 覆盖同 route+type+完全同意图 的旧记录
        self._records = [r for r in self._records
                         if not (r["route"] == route and r["action_type"] == action_type
                                 and r.get("intent") == intent)]
        self._records.append({
            "route": route, "action_type": action_type, "intent": intent,
            "tokens": toks, "selector": info["selector"], "nth": info.get("nth", 0),
        })

    def resolve(self, page: Any, url: str, action_type: str, intent: str) -> Optional[dict]:
        route = normalize_url(url)
        q = _tokens(intent)
        best, best_sim = None, 0.0
        # 只在同路由、同动作类型内找最相似意图, 减少跨场景误匹配.
        for r in self._records:
            if r["route"] != route or r["action_type"] != action_type:
                continue
            sim = _jaccard(q, set(r.get("tokens", [])))
            if sim > best_sim:
                best, best_sim = r, sim
        # 相似度低于阈值时宁愿降级到 L5, 不冒险复用旧结构.
        if best is None or best_sim < _THRESHOLD:
            return None
        info = {"selector": best["selector"], "nth": best.get("nth", 0)}
        # 学习记录命中后仍要校验可见性, 防止页面结构已变化.
        return info if validate_selector(page, info) else None

    def record_failure(self, url: str, action_type: str, selector: Optional[str]) -> None:
        route = normalize_url(url)
        # 后校验失败时删除相同 route/type/selector 的学习记录.
        self._records = [r for r in self._records
                         if not (r["route"] == route and r["action_type"] == action_type
                                 and r.get("selector") == selector)]

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._records = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                # 学习文件损坏不阻断执行, 清空后重新学习.
                self._records = []

    def save(self) -> None:
        # 批次结束持久化页面结构经验.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._records, ensure_ascii=False, indent=2), encoding="utf-8")

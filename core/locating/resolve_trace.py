"""五级定位链追踪 —— 记录每一级命中/跳过/失败, 供 verbose_trace 打印."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ResolveChain:
    """单次 resolve 的定位链路快照."""

    intent: str = ""
    action_type: str = ""
    hint: Optional[str] = None
    exclude: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    llm_called: bool = False
    hit_level: Optional[str] = None
    hit_selector: Optional[str] = None

    def add(self, level: str, status: str, selector: Optional[str] = None, note: str = "") -> None:
        self.steps.append({
            "level": level,
            "status": status,
            "selector": selector,
            "note": note,
        })

    def mark_hit(self, level: str, selector: str) -> None:
        self.hit_level = level
        self.hit_selector = selector

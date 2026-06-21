"""L3 大模型元素决策结果."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DecideResult:
    """元素决策输出."""

    index: Optional[int] = None
    recommended_selector: Optional[str] = None
    skip_navigation: bool = False
    reason: str = ""
    confidence: float = 0.0
    skill_name: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.skip_navigation or self.recommended_selector is not None or (
            self.index is not None and self.index >= 0
        )

"""L4 页面结构学习: PageStructureLearner 封装."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .normalize import normalize_url, validate_selector
from .page_structure_learner import PageStructureLearner
from .skill_resolver import info_from_recommended_selector

logger = logging.getLogger(__name__)


class CompositeStructureLearner:
    """L4: 按 route + 组件类型 + DOM 指纹学习/复用选择器模板."""

    def __init__(
        self,
        accel_dir: str | Path,
        *,
        similarity_threshold: float = 0.6,
    ) -> None:
        self.accel_dir = Path(accel_dir)
        self.page = PageStructureLearner(similarity_threshold=similarity_threshold)
        self.page.load_from_file(self.accel_dir)

    def resolve(
        self,
        page: Any,
        url: str,
        action_type: str,
        intent: str,
        *,
        semantic_items: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        items = semantic_items or []
        route = normalize_url(url)
        sel = self.page.resolve_from_learned(route, intent, action_type, items)
        if not sel:
            return None
        info = info_from_recommended_selector(sel)
        if validate_selector(page, info):
            return info
        return None

    def learn(
        self,
        url: str,
        action_type: str,
        intent: str,
        info: dict,
        *,
        semantic_items: Optional[list[dict]] = None,
        component_library: str = "unknown",
    ) -> None:
        selector = info.get("selector") or ""
        if not selector:
            return
        sel_type = "xpath" if selector.startswith(("/", "xpath=")) else "css"
        if info.get("method") == "role":
            sel_type = "role"
        comp = component_library
        if comp in ("unknown", "generic") and semantic_items:
            comp = self.page._detect_component_library(semantic_items)
        self.page.learn(
            normalize_url(url),
            comp,
            semantic_items or [],
            action_type,
            intent,
            selector,
            selector_type=sel_type,
        )

    def record_failure(
        self,
        url: str,
        action_type: str,
        selector: Optional[str],
        *,
        intent: str = "",
        component_type: str = "generic",
    ) -> None:
        del intent, selector
        self.page.record_failure(
            normalize_url(url),
            action_type,
            component_type,
        )

    def save(self) -> None:
        self.page.save_to_file(self.accel_dir)

    @property
    def stats(self) -> dict[str, Any]:
        return self.page.stats

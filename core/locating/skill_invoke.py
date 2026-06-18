"""按 prompts/skill.md entrypoints 调用组件选择器脚本."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

_skill_path: Optional[Path] = None


def configure_skill_path(path: str | Path | None) -> None:
    global _skill_path
    _skill_path = Path(path) if path else None


def invoke_skill(name: str, /, *args: Any, **kwargs: Any) -> Any:
    """优先走 skill.md entrypoints; 未配置或失败时回退 skill_dom_helpers."""
    if _skill_path is not None:
        try:
            from ..skill_loader import invoke_entrypoint
            return invoke_entrypoint(_skill_path, name, *args, **kwargs)
        except Exception:
            pass
    from . import skill_dom_helpers as helpers
    fn = getattr(helpers, name, None)
    if not callable(fn):
        raise AttributeError(name)
    return fn(*args, **kwargs)

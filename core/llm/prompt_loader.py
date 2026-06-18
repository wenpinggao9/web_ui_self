"""【重点3】提示词加载器 —— 每个 LLM 环节的 system / user 提示词均可被用户修改.

优先级 (从高到低):
  1. config.yaml 的 llm.prompts.{stage}.system / user (非空时覆盖)
  2. prompts/{stage}.system.md / prompts/{stage}.user.md 文件
  3. 代码内置兜底 (传入的 default_system / default_user)

user 模板用 `{{占位符}}` 注入运行时变量 (双花括号, 避免与 JSON 示例里的单花括号冲突).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


class PromptLoader:
    def __init__(self, prompts_dir: str | Path, config_prompts: Optional[dict[str, Any]] = None) -> None:
        self.dir = Path(prompts_dir)
        self.cfg = config_prompts or {}

    # ---------- system ----------
    def system(self, stage: str, default: str = "") -> str:
        override = self._cfg_value(stage, "system")
        if override:
            return override
        f = self.dir / f"{stage}.system.md"
        if f.exists():
            return f.read_text(encoding="utf-8")
        return default

    # ---------- user ----------
    def user(self, stage: str, default: str = "", **kwargs: Any) -> str:
        override = self._cfg_value(stage, "user")
        if override:
            template = override
        else:
            f = self.dir / f"{stage}.user.md"
            template = f.read_text(encoding="utf-8") if f.exists() else default
        return _render(template, kwargs)

    def load(self, stage: str, default_system: str = "", default_user: str = "", **kwargs: Any) -> tuple[str, str]:
        return self.system(stage, default_system), self.user(stage, default_user, **kwargs)

    def _cfg_value(self, stage: str, key: str) -> str:
        node = self.cfg.get(stage) or {}
        if isinstance(node, dict):
            v = node.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return ""


def _render(template: str, kwargs: dict[str, Any]) -> str:
    out = template
    for k, v in kwargs.items():
        out = out.replace("{{" + k + "}}", "" if v is None else str(v))
    return out

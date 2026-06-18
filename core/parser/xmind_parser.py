"""步骤① 解析器 —— XMind 用例解析 (留桩, 阶段E 补全).

4 层 topic 结构: 根 → 一级模块 → 二级模块 → 用例编号 → 步骤/预期/依赖.
"""
from __future__ import annotations

from pathlib import Path

from .schema import ParsedCase


def parse_xmind(path: str | Path) -> list[ParsedCase]:
    raise NotImplementedError("XMind 解析将在阶段E 实现, 当前请使用 .md 用例")

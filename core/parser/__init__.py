"""步骤① 解析器统一入口.

parse_case(路径) 按后缀 .md / .xmind 自动分发, 不支持的后缀抛 ValueError.
"""
from __future__ import annotations

from pathlib import Path

from .schema import CaseResource, ExecutionBlock, ParsedCase


def parse_case(path: str | Path) -> list[ParsedCase]:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".md", ".markdown"):
        from .markdown_parser import parse_markdown
        return parse_markdown(p)
    if suffix == ".xmind":
        from .xmind_parser import parse_xmind
        return parse_xmind(p)
    raise ValueError(f"不支持的用例后缀: {suffix} (支持 .md / .xmind)")


__all__ = ["parse_case", "ParsedCase", "CaseResource", "ExecutionBlock"]

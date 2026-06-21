"""从后校验 resolve_hint 提取可强制执行的选择器."""
from __future__ import annotations

import re
from typing import Any, Optional

_CSS_HINT_PATTERNS = (
    re.compile(r"(button:has-text\('[^']+'\))", re.I),
    re.compile(r"(table\s+tbody\s+tr:first-child(?:\s+[\w:#.\[\]=\-()]+)*)", re.I),
    re.compile(r"`([^`]+)`"),
    re.compile(r"选择器\s*(?:如\s*)?['\"]([^'\"]+)['\"]"),
)

_INDEX_HINT_PATTERNS = (
    re.compile(r"\[(\d+)\]"),
    re.compile(r"index[=\s]*(\d+)", re.I),
    re.compile(r"索引[=\s]*(\d+)"),
)


def extract_selector_from_resolve_hint(hint: Optional[str]) -> Optional[str]:
    """从 resolve_hint 文本中提取 Playwright/CSS 选择器."""
    text = (hint or "").strip()
    if not text:
        return None
    for pat in _CSS_HINT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        sel = m.group(1).strip().strip("`").rstrip(",.;")
        if sel and _looks_like_selector(sel):
            return sel
    return None


def _looks_like_selector(sel: str) -> bool:
    low = sel.lower()
    return any(
        tok in low
        for tok in ("has-text", "tbody", "tr:first", "role=", "button", "a:", ".ant-", "#")
    )


def extract_dom_index_from_resolve_hint(hint: Optional[str]) -> Optional[int]:
    """从 resolve_hint 提取 DOM 摘要索引, 如 [89]."""
    text = hint or ""
    for pat in _INDEX_HINT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue
    return None


def resolve_force_selector_from_hint(
    hint: Optional[str],
    *,
    semantic_items: Optional[list[dict[str, Any]]] = None,
) -> Optional[str]:
    """优先 CSS 选择器; 其次用 DOM 索引映射为选择器."""
    sel = extract_selector_from_resolve_hint(hint)
    if sel:
        return sel
    idx = extract_dom_index_from_resolve_hint(hint)
    if idx is None or not semantic_items or idx < 0 or idx >= len(semantic_items):
        return None
    try:
        from ..dom.semantic_dom import build_locator_info

        info = build_locator_info(semantic_items[idx])
        return str(info.get("selector") or "").strip() or None
    except Exception:
        return None


__all__ = [
    "extract_dom_index_from_resolve_hint",
    "extract_selector_from_resolve_hint",
    "resolve_force_selector_from_hint",
]

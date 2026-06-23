"""从后校验 resolve_hint 提取选择器或 DOM 索引."""
from __future__ import annotations

import re
from typing import Any, Optional

_CSS_HINT_PATTERNS = (
    re.compile(r"(button:has-text\('[^']+'\))", re.I),
    re.compile(r"(table\s+tbody\s+tr:first-child(?:\s+[\w:#.\[\]=\-()]+)*)", re.I),
    re.compile(r"`([^`]+)`"),
    re.compile(r"选择器\s*(?:如\s*)?['\"]([^'\"]+)['\"]"),
)

# 裸 CSS 选择器 (后校验直接给出 input#searchText 等)
_BARE_SELECTOR_RE = re.compile(
    r"^(?:[a-z][a-z0-9-]*(?:\[[^\]]+\])?|[#.\\[])[\w#.,\[\]=\-\"':]*$",
    re.I,
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
    if _looks_like_bare_selector(text) and _looks_like_selector(text):
        return text
    for pat in _CSS_HINT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        sel = m.group(1).strip().strip("`").rstrip(",.;")
        if sel and _looks_like_selector(sel):
            return sel
    return None


def _looks_like_bare_selector(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 200 or " " in t:
        return False
    if t.startswith(("http://", "https://", "//")):
        return False
    return bool(_BARE_SELECTOR_RE.match(t))


def _looks_like_selector(sel: str) -> bool:
    low = sel.lower()
    return any(
        tok in low
        for tok in (
            "has-text", "tbody", "tr:first", "role=", "button", "a:",
            ".ant-", "#", "input", "textarea", "[placeholder", "[name=",
        )
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


def resolve_selector_from_hint(
    hint: Optional[str],
    *,
    semantic_items: Optional[list[dict[str, Any]]] = None,
) -> Optional[str]:
    """从 hint 提取 CSS 选择器; 或按 DOM 索引映射为选择器 (供 L5 等参考)."""
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
    "resolve_selector_from_hint",
]

"""步骤⑨ 自愈机制 —— 缓存/记忆命中但定位失败时的修复策略."""
from __future__ import annotations

import re
from typing import Any, Optional

from .playwright_api import infer_from_selector, try_candidates


def heal(page: Any, info: dict) -> Optional[dict]:
    """基于旧定位信息生成候选变体, 返回第一个当前页面可见的结果."""
    from .playwright_api import normalize_info

    spec = normalize_info(info)
    candidates: list[dict] = []

    if spec.get("method") == "css":
        sel = spec.get("selector") or ""
        for cand in _css_variants(sel):
            candidates.append(infer_from_selector(cand, spec.get("nth", 0), spec.get("in_dialog", False)))
    elif spec.get("method") == "text" and spec.get("name"):
        name = spec["name"]
        candidates.extend([
            {"method": "role", "role": "button", "name": name, "exact": False, "nth": 0},
            {"method": "text", "name": name, "exact": not spec.get("exact", False), "nth": 0},
            infer_from_selector(f'button:has-text("{name}")'),
        ])
    else:
        candidates.append(spec)

    return try_candidates(page, candidates)


def _css_variants(sel: str) -> list[str]:
    out: list[str] = []
    m = re.search(r':has-text\("([^"]+)"\)', sel)
    if m:
        text = m.group(1)
        out.append(f'text="{text}"')
        out.append(f'button:has-text("{text}")')
        out.append(f'a:has-text("{text}")')
    if "[" in sel and "]" in sel:
        base = sel.split("[")[0]
        if base:
            out.append(base)
    return out

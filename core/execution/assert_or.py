"""「或」语义断言 —— 预期多分支时任一支满足即通过."""
from __future__ import annotations

import re
from typing import Any, Optional

_OR_INTENT_RE = re.compile(r"或|否则|有的话|没有.{0,24}则|任一|任意一种")
_DETAIL_BRANCH_RE = re.compile(r"详情|下一个任务|下一.*任务|任务详情")
_LIST_BRANCH_RE = re.compile(r"待领取|待前审|领取页|列表页")


def is_or_assert(action: Any) -> bool:
    """是否为多分支「或」断言."""
    extras = getattr(action, "extras", None) or {}
    if extras.get("any_of") or extras.get("or_group"):
        return True
    if extras.get("branches"):
        return True
    intent = getattr(action, "intent", None) or ""
    return bool(_OR_INTENT_RE.search(intent))


def try_or_branches(
    page: Any, branches: list[Any], body_text: str,
) -> Optional[tuple[bool, str]]:
    """按 extras.branches 逐支尝试: 字面量 value 或启发式."""
    for i, raw in enumerate(branches):
        if not isinstance(raw, dict):
            continue
        branch_intent = str(raw.get("intent") or raw.get("desc") or "").strip()
        branch_value = str(raw.get("value") or "").strip()
        label = branch_intent or branch_value or f"分支{i + 1}"
        if branch_value and branch_value in body_text:
            return True, f"或断言(分支{i + 1}): 页面包含 {branch_value!r} ({label})"
        hit = try_or_heuristic(page, branch_intent)
        if hit is not None:
            return True, f"或断言(分支{i + 1}): {hit[1]}"
    return None


def combined_or_intent(action: Any) -> str:
    """合并 intent 与 extras.branches 供语义断言使用."""
    intent = getattr(action, "intent", None) or ""
    branches = (getattr(action, "extras", None) or {}).get("branches") or []
    parts = [intent] if intent else []
    for b in branches:
        if isinstance(b, dict):
            t = str(b.get("intent") or b.get("desc") or "").strip()
            if t:
                parts.append(t)
    return "；".join(parts) if parts else intent


def try_or_heuristic(page: Any, intent: str) -> Optional[tuple[bool, str]]:
    """用 URL/页面特征快速判定或断言的任一分支 (无需 LLM)."""
    if not intent:
        return None
    url = (page.url or "").lower()
    try:
        body = (page.inner_text("body") or "")[:4000]
    except Exception:
        body = ""

    want_detail = bool(_DETAIL_BRANCH_RE.search(intent))
    want_list = bool(_LIST_BRANCH_RE.search(intent))

    on_detail = "/detail" in url or "任务id" in body.lower() or "审核原因" in body
    on_list = "/wait-preview" in url or "待前审" in body

    if want_detail and on_detail:
        try:
            radios = page.locator("input[type=radio], .ant-radio-input").count()
        except Exception:
            radios = 0
        if radios >= 1 or "/detail" in url:
            return True, "或断言(启发式): 当前在任务详情页"

    if want_list and on_list:
        return True, "或断言(启发式): 当前在待领取/待前审页面"

    return None

"""「或」语义断言 —— 预期多分支时任一支满足即通过."""
from __future__ import annotations

import re
from typing import Any, Optional

from .post_submit_eval import (
    build_live_submit_facts,
    eval_submit_expect,
    infer_expect_from_text,
)
from .script_helpers import is_detail_submission_url

_OR_INTENT_RE = re.compile(r"否则|有的话|没有.{0,24}则|任一|任意一种")
_DETAIL_BRANCH_RE = re.compile(r"详情|任务详情")
_LIST_BRANCH_RE = re.compile(r"待领取|待前审|领取页|列表页")


def is_or_assert(action: Any) -> bool:
    """是否为多分支「或」断言.

    注意: 选项文案本身可含「或」(如「题目不完整或题干残缺」), 不是逻辑分支.
    """
    extras = getattr(action, "extras", None) or {}
    if extras.get("any_of") or extras.get("or_group"):
        return True
    if extras.get("branches"):
        return True
    value = (getattr(action, "value", None) or "").strip()
    intent = getattr(action, "intent", None) or ""
    if value and "或" in value:
        if value in intent or re.search(r"(包含|含)", intent):
            return False
    if _OR_INTENT_RE.search(intent):
        return True
    if "或" in intent:
        if re.search(r"(包含|含|选项包括|包括)", intent):
            return False
        return True
    return False


def _resolve_or_page_url(
    page: Any,
    page_url: str = "",
    dispatch_meta: Optional[dict[str, Any]] = None,
) -> str:
    try:
        live = (getattr(page, "url", None) or "").lower()
        if live:
            return live
    except Exception:
        pass
    for cand in (page_url, str((dispatch_meta or {}).get("url_after") or "")):
        if cand:
            return cand.lower()
    return ""


def _resolve_or_body_text(page: Any, body_text: str = "") -> str:
    if body_text:
        return body_text[:4000]
    try:
        return (page.inner_text("body") or "")[:4000]
    except Exception:
        return ""


def try_or_branches(
    page: Any,
    branches: list[Any],
    body_text: str,
    *,
    dispatch_meta: Optional[dict[str, Any]] = None,
    page_url: str = "",
    live_facts: Any = None,
) -> Optional[tuple[bool, str]]:
    """按 extras.branches 逐支尝试: 优先实时 DOM 字面量, 再用实时 URL/实体判定."""
    url = _resolve_or_page_url(page, page_url, dispatch_meta)
    facts = live_facts

    for i, raw in enumerate(branches):
        if not isinstance(raw, dict):
            continue
        branch_intent = str(raw.get("intent") or raw.get("desc") or "").strip()
        branch_value = str(raw.get("value") or "").strip()
        label = branch_intent or branch_value or f"分支{i + 1}"
        if branch_value and branch_value in body_text:
            return True, f"或断言(分支{i + 1}): 页面包含 {branch_value!r} ({label})"
        if facts is not None:
            exp = infer_expect_from_text(branch_intent)
            if exp:
                ok, msg = eval_submit_expect(exp, facts, url)
                if ok:
                    return True, f"或断言(分支{i + 1}): {msg} ({label})"
        hit = try_or_heuristic(
            page, branch_intent,
            dispatch_meta=dispatch_meta,
            page_url=url,
            body_text=body_text,
            live_facts=facts,
        )
        if hit is not None:
            detail = hit[1]
            if not detail.startswith("或断言"):
                detail = f"或断言(分支{i + 1}): {detail}"
            return hit[0], detail
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


def try_or_heuristic(
    page: Any,
    intent: str,
    *,
    dispatch_meta: Optional[dict[str, Any]] = None,
    page_url: str = "",
    body_text: str = "",
    live_facts: Any = None,
) -> Optional[tuple[bool, str]]:
    """用实时 URL/页面特征快速判定或断言分支 (无需 LLM)."""
    if not intent:
        return None
    url = _resolve_or_page_url(page, page_url, dispatch_meta)
    body = _resolve_or_body_text(page, body_text)
    facts = live_facts

    want_detail = bool(_DETAIL_BRANCH_RE.search(intent))
    want_list = bool(_LIST_BRANCH_RE.search(intent))

    on_detail = (
        is_detail_submission_url(url)
        or "请选择审核原因" in body
        or ("任务id" in body.lower() and "审核原因" in body)
    )
    on_list = not on_detail and not is_detail_submission_url(url)

    if want_detail and on_detail:
        if body_text or "/detail" in url:
            return True, "或断言(启发式): 当前在任务详情页"
        try:
            radios = page.locator("input[type=radio], .ant-radio-input").count()
        except Exception:
            radios = 0
        if radios >= 1:
            return True, "或断言(启发式): 当前在任务详情页"

    if want_list and on_list:
        return True, "或断言(启发式): 当前在待领取/待前审页面"

    if facts is not None:
        exp = infer_expect_from_text(intent)
        if exp:
            ok, msg = eval_submit_expect(exp, facts, url)
            if ok:
                return True, f"或断言(启发式): {msg}"

    return None

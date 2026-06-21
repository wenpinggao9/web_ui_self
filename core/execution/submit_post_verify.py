"""提交类步骤: tab 恢复 + 本地后校验.

click 已成功且页面已离开详情 / DOM 不可读时不误杀.
保留多 tab: 先 recover 到 list_anchor, 再判成功, 供后续断言/点击复用.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .script_helpers import (
    _body_has_submit_error,
    _page_alive,
    _page_usable,
    _read_body_safe,
    _url_safe,
    find_usable_context_pages,
    is_detail_submission_url,
    pick_surviving_tab_after_detail_close,
    submit_left_detail_context,
)

_SUCCESS_OUTCOMES = frozenset({
    "returned_to_list", "resource_id_changed", "route_changed",
})
_AMBIGUOUS_OUTCOMES = frozenset({"timeout", "settled"})


def is_submit_intent(intent: str) -> bool:
    return "提交" in (intent or "")


def submit_dispatch_should_succeed(meta: dict[str, Any]) -> bool:
    """提交 click 后 dispatch 是否应报成功 (含 tab 关闭但已回列表)."""
    outcome = str(meta.get("navigation_outcome") or "")
    url_before = str(meta.get("url_before") or "")
    if outcome == "submit_error":
        return False
    if outcome in _SUCCESS_OUTCOMES:
        return True
    if meta.get("left_detail_context") and is_detail_submission_url(url_before):
        return True
    if outcome in _AMBIGUOUS_OUTCOMES:
        if meta.get("left_detail_context"):
            return True
        # click 已成功且详情 tab 已关/有兄弟 tab → 视为提交推进 (宽松判定)
        if meta.get("submit_click_ok") and is_detail_submission_url(url_before):
            if meta.get("recovered") or meta.get("detail_tab_closed"):
                return True
    return True


@dataclass
class SubmitFinalizeResult:
    page: Any
    meta: dict[str, Any]
    dispatch_ok: bool
    message: Optional[str] = None


def finalize_submit_after_dispatch(
    page: Any,
    meta: Optional[dict[str, Any]],
    *,
    list_anchor: Any = None,
) -> SubmitFinalizeResult:
    """提交 dispatch 后: 切存活 tab, 升级 meta, 决定 dispatch 是否应成功."""
    meta = dict(meta or {})
    url_before = str(meta.get("url_before") or "")
    if not is_detail_submission_url(url_before):
        return SubmitFinalizeResult(page, meta, True)

    outcome = str(meta.get("navigation_outcome") or "")
    page, rec, url_now, left = pick_surviving_tab_after_detail_close(
        page,
        url_before=url_before,
        list_anchor=list_anchor,
    )
    if not _page_alive(page) and is_detail_submission_url(url_before):
        meta["detail_tab_closed"] = True
    if url_now or rec or left:
        meta["url_after"] = url_now or str(meta.get("url_after") or "")
        meta["recovered"] = bool(meta.get("recovered")) or rec or left

    if left:
        meta["left_detail_context"] = True
        if outcome in _AMBIGUOUS_OUTCOMES or outcome == "submit_error":
            meta["navigation_outcome"] = "returned_to_list"
    elif not meta.get("left_detail_context"):
        url_after = str(meta.get("url_after") or _url_safe(page))
        if submit_left_detail_context(
            url_before, url_after, list_anchor=list_anchor,
        ):
            meta["left_detail_context"] = True
            if outcome in _AMBIGUOUS_OUTCOMES:
                meta["navigation_outcome"] = "returned_to_list"
        elif not _page_alive(page):
            usable = find_usable_context_pages(page, list_anchor)
            if usable:
                pick = usable[0]
                for p in usable:
                    if not is_detail_submission_url(_url_safe(p)):
                        pick = p
                        break
                page = pick
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                meta["left_detail_context"] = True
                meta["navigation_outcome"] = "returned_to_list"
                meta["url_after"] = _url_safe(page)
                meta["detail_tab_closed"] = True
        elif _page_usable(page) and not is_detail_submission_url(_url_safe(page)):
            meta["left_detail_context"] = True
            if outcome in _AMBIGUOUS_OUTCOMES:
                meta["navigation_outcome"] = "returned_to_list"

    dispatch_ok = submit_dispatch_should_succeed(meta)
    message = None
    if dispatch_ok and outcome in _AMBIGUOUS_OUTCOMES.union({"submit_error"}):
        message = (
            f"提交后已离开详情上下文 "
            f"({str(meta.get('url_after') or '')[:80]})"
        )
    return SubmitFinalizeResult(page, meta, dispatch_ok, message)


def _detail_page_has_submit_error(page: Any, dom_summary: Optional[str]) -> bool:
    text = (dom_summary or "").strip()
    if not text and _page_usable(page):
        text = _read_body_safe(page)
    return bool(text) and _body_has_submit_error(text)


@dataclass
class SubmitPostVerdict:
    """本地提交后校验结论; step_ok=None 表示交 LLM."""

    step_ok: Optional[bool]
    reason: str = ""
    page: Any = None
    meta: Optional[dict[str, Any]] = None


def evaluate_submit_post_check(
    intent: str,
    dispatch_ok: bool,
    dispatch_meta: Optional[dict[str, Any]],
    page: Any,
    dom_summary: Optional[str],
    *,
    list_anchor: Any = None,
) -> SubmitPostVerdict:
    """提交类步骤本地后校验 (recover → 判成功/失败 → 否则 defer LLM)."""
    if not is_submit_intent(intent):
        return SubmitPostVerdict(step_ok=None)

    fin = finalize_submit_after_dispatch(
        page, dispatch_meta, list_anchor=list_anchor,
    )
    page, meta = fin.page, fin.meta
    outcome = str(meta.get("navigation_outcome") or "")
    url_before = str(meta.get("url_before") or "")

    if outcome == "submit_error":
        if meta.get("left_detail_context") and not _detail_page_has_submit_error(
            page, dom_summary,
        ):
            return SubmitPostVerdict(
                step_ok=True,
                reason="提交后已离开详情上下文 (原 submit_error 来自已关闭 tab)",
                page=page,
                meta=meta,
            )
        if _detail_page_has_submit_error(page, dom_summary):
            return SubmitPostVerdict(
                step_ok=False,
                reason="提交后页面提示不可重复提交或未生效",
                page=page,
                meta=meta,
            )
        return SubmitPostVerdict(
            step_ok=False,
            reason=f"提交后 navigation_outcome={outcome}",
            page=page,
            meta=meta,
        )

    if outcome in _SUCCESS_OUTCOMES:
        return SubmitPostVerdict(
            step_ok=True,
            reason=f"提交后页面已导航 ({outcome})",
            page=page,
            meta=meta,
        )

    if meta.get("left_detail_context"):
        return SubmitPostVerdict(
            step_ok=True,
            reason=(
                f"提交后已离开详情上下文 "
                f"(outcome={outcome}, url={str(meta.get('url_after') or '')[:80]})"
            ),
            page=page,
            meta=meta,
        )

    url_after = str(meta.get("url_after") or _url_safe(page))
    if url_before and submit_left_detail_context(
        url_before, url_after, list_anchor=list_anchor,
    ):
        return SubmitPostVerdict(
            step_ok=True,
            reason=f"提交后 URL 已离开详情上下文 ({url_after[:80]})",
            page=page,
            meta=meta,
        )

    id_before = str(meta.get("entity_id_before") or "")
    id_after = str(meta.get("entity_id_after") or "")
    if id_before and id_after and id_before != id_after:
        return SubmitPostVerdict(
            step_ok=True,
            reason=f"提交后实体已切换 ({id_before} → {id_after})",
            page=page,
            meta=meta,
        )

    if is_detail_submission_url(_url_safe(page)) or is_detail_submission_url(url_before):
        if _detail_page_has_submit_error(page, dom_summary):
            return SubmitPostVerdict(
                step_ok=False,
                reason="提交未生效: 仍在详情页且存在失败提示",
                page=page,
                meta=meta,
            )

    dom = (dom_summary or "").strip()
    if not dom and is_detail_submission_url(url_before):
        usable = find_usable_context_pages(page, list_anchor)
        if (
            meta.get("left_detail_context")
            or meta.get("detail_tab_closed")
            or not _page_alive(page)
            or usable
        ):
            if usable and _page_alive(page) is False:
                page = usable[0]
                for p in usable:
                    if not is_detail_submission_url(_url_safe(p)):
                        page = p
                        break
                meta["left_detail_context"] = True
                meta["url_after"] = _url_safe(page)
            return SubmitPostVerdict(
                step_ok=True,
                reason=(
                    "提交后详情 tab 已关闭, 已切至存活 tab "
                    "(DOM 不可读时不误杀)"
                ),
                page=page,
                meta=meta,
            )

    if meta.get("submit_click_ok") and is_detail_submission_url(url_before):
        if meta.get("left_detail_context") or meta.get("detail_tab_closed"):
            return SubmitPostVerdict(
                step_ok=True,
                reason=fin.message or "提交 click 已成功且已离开详情上下文",
                page=page,
                meta=meta,
            )
        if not _page_alive(page):
            usable = find_usable_context_pages(page, list_anchor)
            if usable:
                page = usable[0]
                meta["left_detail_context"] = True
                meta["detail_tab_closed"] = True
                meta["url_after"] = _url_safe(page)
                return SubmitPostVerdict(
                    step_ok=True,
                    reason="提交 click 已成功, 详情 tab 已关闭并切至兄弟 tab",
                    page=page,
                    meta=meta,
                )

    if outcome in _AMBIGUOUS_OUTCOMES and not meta.get("left_detail_context"):
        return SubmitPostVerdict(
            step_ok=False,
            reason=f"提交后 navigation_outcome={outcome}, 页面未离开详情",
            page=page,
            meta=meta,
        )

    if not dispatch_ok and not meta.get("left_detail_context"):
        return SubmitPostVerdict(
            step_ok=False,
            reason=f"提交 dispatch 未成功 (outcome={outcome or '无'})",
            page=page,
            meta=meta,
        )

    # V3 式: 提交 click 已成功即本步成功; 仅「仍在详情且页面有失败文案」时否
    if dispatch_ok and meta.get("submit_click_ok"):
        if (
            outcome == "submit_error"
            and not meta.get("left_detail_context")
            and _detail_page_has_submit_error(page, dom_summary)
        ):
            return SubmitPostVerdict(
                step_ok=False,
                reason="提交 click 已成功但页面仍提示提交失败",
                page=page,
                meta=meta,
            )
        return SubmitPostVerdict(
            step_ok=True,
            reason=f"提交 click 已成功 ({outcome or 'dispatch_ok'})",
            page=page,
            meta=meta,
        )

    return SubmitPostVerdict(step_ok=None, page=page, meta=meta)

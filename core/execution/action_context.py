"""从 PlannedAction intent 推断所需页面上下文 —— 就绪检查 / recovery 过滤."""
from __future__ import annotations

import re
from typing import Literal, Optional

from ..planning import PlannedAction
from .script_helpers import _url_query_id, is_detail_submission_url

PageContextKind = Literal["detail", "dialog", "filter", "table_row"]

_DETAIL_CTX_RE = re.compile(r"详情页|详情区|详情内")
_DIALOG_CTX_RE = re.compile(r"弹窗|对话框|抽屉|浮层")
_FILTER_CTX_RE = re.compile(r"筛选区|筛选条件")
_ROW_CTX_RE = re.compile(r"列表行|表格行|该行|行内")

_NAV_TO_DETAIL_RE = re.compile(
    r"进入.*详情|查看.*详情|打开.*详情|点击.*查看.*进入", re.I,
)
_ENTITY_ID_RE = re.compile(r"\d{6,}")


def infer_required_page_context(intent: str) -> Optional[PageContextKind]:
    if not intent:
        return None
    if _DETAIL_CTX_RE.search(intent):
        return "detail"
    if _DIALOG_CTX_RE.search(intent):
        return "dialog"
    if _FILTER_CTX_RE.search(intent):
        return "filter"
    if _ROW_CTX_RE.search(intent):
        return "table_row"
    return None


def pinned_entity_ids(intent: str) -> list[str]:
    return _ENTITY_ID_RE.findall(intent or "")


def action_context_satisfied(page_url: str, intent: str) -> tuple[bool, str]:
    """当前 URL 是否已满足动作 intent 声明的操作上下文 (不含侧栏导航类模块页)."""
    ctx = infer_required_page_context(intent)
    if ctx is None:
        return False, ""
    if ctx == "detail":
        if not is_detail_submission_url(page_url):
            return False, ""
        pinned = pinned_entity_ids(intent)
        if pinned:
            page_id = _url_query_id(page_url)
            if page_id and page_id not in pinned and not any(
                pid in page_url for pid in pinned
            ):
                return False, ""
        return True, "当前 URL 已满足详情操作上下文"
    return False, ""


def recovery_navigates_to_detail(intent: str) -> bool:
    return bool(_NAV_TO_DETAIL_RE.search(intent or ""))


def filter_contradictory_recovery(
    recovery: list[PlannedAction],
    action_intent: str,
    page_url: str,
) -> list[PlannedAction]:
    """已在目标上下文时, 去掉「再导航进去」类 recovery."""
    satisfied, _ = action_context_satisfied(page_url, action_intent)
    if not satisfied:
        return recovery
    required = infer_required_page_context(action_intent)
    if required != "detail":
        return recovery
    return [
        r for r in recovery
        if not recovery_navigates_to_detail(r.intent or "")
    ]

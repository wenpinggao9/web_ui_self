"""提交后断言 —— 框架自动理解: 步骤时序 + 实体ID对比 + 自然语言意图推断.

不依赖业务知识里的 page_capture / session_ops / extras.expect.
断言时刻从当前 tab + 实时 DOM 构造 SubmitLiveFacts (不固化快照).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .entity_discover import (
    discover_active_entity,
    discover_page_entity,
    url_entity_maps_differ,
)

_FAIL_OUTCOMES = frozenset({"timeout", "settled", "submit_error"})

_ENTITY_CHANGED_RE = re.compile(
    r"下一|切换|另一条|另一个|新(?:的)?(?:任务|工单|单)|next|继续审核",
    re.I,
)
_NAV_AWAY_RE = re.compile(
    r"列表|待领取|待前审|待审|回到|返回|离开|领取页",
    re.I,
)
_SUBMIT_FAIL_RE = re.compile(
    r"未生效|未提交|提交失败|失败|停留|不变|重复|卡住",
    re.I,
)
_SAME_ENTITY_RE = re.compile(r"仍在|仍是|同一|未变|没有切换", re.I)


@dataclass
class SubmitLiveFacts:
    """断言时刻从 live tab/DOM 临时构造的事实 (不持久化)."""

    navigation_outcome: str = ""
    url_before: str = ""
    url_after: str = ""
    entity_field: str = ""
    entity_id_before: str = ""
    entity_id_after: str = ""

    @property
    def entity_changed(self) -> bool:
        return bool(
            self.entity_id_before
            and self.entity_id_after
            and self.entity_id_before != self.entity_id_after,
        )

    def format_facts(self) -> str:
        field_name = self.entity_field or "entity"
        lines = [
            "【提交后结构化事实】(断言时刻实时提取, 优先据此判断)",
            f"- 提交前{field_name}: {self.entity_id_before or '(未记录)'}",
            f"- 提交后{field_name}: {self.entity_id_after or '(未解析)'}",
            f"- 实体是否切换: {'是' if self.entity_changed else '否'}",
            f"- navigation_outcome: {self.navigation_outcome or '(无)'}",
            f"- url_before: {(self.url_before or '')[:120]}",
            f"- url_after: {(self.url_after or '')[:120]}",
        ]
        return "\n".join(lines)


def infer_expect_from_text(text: str) -> str:
    """从断言自然语言推断通用期望 (非业务配置)."""
    t = (text or "").strip()
    if not t:
        return ""
    if _SUBMIT_FAIL_RE.search(t):
        return "submit_failed"
    if _SAME_ENTITY_RE.search(t):
        return "entity_unchanged"
    if _ENTITY_CHANGED_RE.search(t):
        return "entity_changed"
    if _NAV_AWAY_RE.search(t):
        return "navigated_away"
    if re.search(r"详情", t, re.I) and not _SAME_ENTITY_RE.search(t):
        return "entity_changed"
    return ""


def build_live_submit_facts(
    *,
    page: Any,
    items: Optional[list[dict]],
    dispatch_meta: Optional[dict[str, Any]],
    api_context: dict[str, Any],
    list_anchor: Any = None,
) -> Optional[SubmitLiveFacts]:
    """断言时刻从当前 tab + 实时 DOM 构造事实."""
    meta = dispatch_meta or {}
    if not meta.get("submit_click_ok"):
        return None
    from .assert_scope import items_flat_text
    from .tab_follow import follow_active_tab

    page, _, _ = follow_active_tab(page, list_anchor)
    try:
        url_live = page.url or ""
    except Exception:
        url_live = str(meta.get("url_after") or "")

    flat = items_flat_text(items) if items else ""
    if not flat:
        try:
            flat = (page.inner_text("body") or "")[:6000]
        except Exception:
            flat = ""

    url_before = str(meta.get("url_before") or "")
    id_before = str(meta.get("entity_id_before") or "")
    if not id_before:
        id_before, _ = discover_active_entity(api_context, url=url_before, flat_text="")
    id_after, field_after = discover_page_entity(
        api_context, url=url_live, flat_text=flat,
    )

    outcome = str(meta.get("navigation_outcome") or "")
    if id_before and id_after and id_before != id_after:
        outcome = "resource_id_changed"
    elif url_entity_maps_differ(url_before, url_live):
        outcome = "resource_id_changed"
    elif not _is_detail_url(url_live) and _is_detail_url(url_before):
        outcome = "returned_to_list"

    return SubmitLiveFacts(
        navigation_outcome=outcome,
        url_before=url_before,
        url_after=url_live,
        entity_field=field_after or str(meta.get("entity_field") or "entity"),
        entity_id_before=id_before,
        entity_id_after=id_after,
    )


def _is_detail_url(url: str) -> bool:
    u = (url or "").lower()
    return "/detail" in u or "detail" in u.split("?")[0].rstrip("/").split("/")[-1:]


def eval_submit_expect(
    expect: str, facts: SubmitLiveFacts, page_url: str,
) -> tuple[bool, str]:
    """按通用 expect 枚举判定 live 提交事实."""
    return _eval_expect(expect, facts, page_url)


def _eval_expect(expect: str, facts: SubmitLiveFacts, page_url: str) -> tuple[bool, str]:
    exp = (expect or "").strip().lower()
    if exp in ("entity_changed", "task_changed", "resource_changed"):
        if facts.entity_changed:
            return (
                True,
                f"实体已切换 ({facts.entity_id_before} → {facts.entity_id_after})",
            )
        if facts.navigation_outcome == "resource_id_changed" and facts.entity_id_after:
            return True, f"导航已切换资源 (当前={facts.entity_id_after})"
        if facts.navigation_outcome in _FAIL_OUTCOMES:
            return False, f"提交未生效 ({facts.navigation_outcome})"
        return False, (
            f"实体未切换 (前={facts.entity_id_before}, 后={facts.entity_id_after})"
        )
    if exp in ("entity_unchanged", "task_unchanged", "same_entity"):
        if facts.entity_id_before and facts.entity_id_after:
            ok = facts.entity_id_before == facts.entity_id_after
            return ok, (
                f"实体未变 ({facts.entity_id_before})"
                if ok
                else f"实体已变 ({facts.entity_id_before}→{facts.entity_id_after})"
            )
        return facts.navigation_outcome in _FAIL_OUTCOMES, f"outcome={facts.navigation_outcome}"
    if exp in ("navigated_away", "navigated_to_list", "left_detail", "list"):
        if facts.navigation_outcome == "returned_to_list":
            return True, "已回到列表页"
        if not _is_detail_url(page_url):
            return True, f"已离开详情页 ({page_url[:80]})"
        return False, "仍在详情页"
    if exp in ("submit_failed", "submit_not_effective"):
        ok = facts.navigation_outcome in _FAIL_OUTCOMES or (
            facts.entity_id_before
            and facts.entity_id_after
            and facts.entity_id_before == facts.entity_id_after
        )
        return ok, f"提交未推进 (outcome={facts.navigation_outcome})"
    if exp in ("submit_succeeded", "any_success", "advanced"):
        return _eval_submit_advanced(facts, page_url)
    return False, f"未知 expect={expect!r}"


def _eval_submit_advanced(
    facts: SubmitLiveFacts, page_url: str,
) -> tuple[bool, str]:
    """提交应推进: 实体切换 / 回列表 / 离开详情 均算成功."""
    if facts.entity_changed:
        return (
            True,
            f"提交后实体已切换 ({facts.entity_id_before} → {facts.entity_id_after})",
        )
    if facts.navigation_outcome == "returned_to_list":
        return True, "提交后已回到列表"
    if facts.navigation_outcome in ("resource_id_changed", "route_changed"):
        return True, f"提交后页面已导航 ({facts.navigation_outcome})"
    if facts.navigation_outcome in _FAIL_OUTCOMES:
        return False, f"提交未生效 ({facts.navigation_outcome})"
    if not _is_detail_url(page_url):
        return True, "提交后已离开详情页"
    if (
        facts.entity_id_before
        and facts.entity_id_after
        and facts.entity_id_before == facts.entity_id_after
    ):
        return False, f"提交未推进 (实体仍为 {facts.entity_id_before})"
    return False, f"提交后状态不明确 (outcome={facts.navigation_outcome})"

"""提交后断言 —— 框架自动理解: 步骤时序 + 实体ID对比 + 自然语言意图推断.

不依赖业务知识里的 page_capture / session_ops / extras.expect.
数据来自:
  - 上一步点击提交后固化的 SubmitSnapshot (awaiting_assert)
  - entity_discover 从 URL / DOM / ops 账本自动提取实体 ID
  - 断言 intent 的通用语义 (下一/列表/失败等) 推断期望结局
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .entity_discover import (
    discover_active_entity,
    discover_page_entity,
    summarize_recorded_context,
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
_OR_SPLIT_RE = re.compile(r"或|否则|没有.{0,20}则|任一|任意一种")


@dataclass
class SubmitSnapshot:
    """提交一次后固化的可比对上下文."""

    navigation_outcome: str = ""
    url_before: str = ""
    url_after: str = ""
    entity_field: str = ""
    entity_id_before: str = ""
    entity_id_after: str = ""
    recorded_context: dict[str, Any] = field(default_factory=dict)
    awaiting_assert: bool = True

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
            "【提交后结构化事实】(框架自动提取, 优先据此判断)",
            f"- 提交前{field_name}: {self.entity_id_before or '(未记录)'}",
            f"- 提交后{field_name}: {self.entity_id_after or '(未解析)'}",
            f"- 实体是否切换: {'是' if self.entity_changed else '否'}",
            f"- navigation_outcome: {self.navigation_outcome or '(无)'}",
            f"- url_before: {(self.url_before or '')[:120]}",
            f"- url_after: {(self.url_after or '')[:120]}",
        ]
        if self.recorded_context:
            import json
            lines.append(
                f"- 会话记录摘要: {json.dumps(self.recorded_context, ensure_ascii=False)[:500]}"
            )
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


def _split_or_intents(intent: str) -> list[str]:
    """把「A 或 B」拆成候选分支文案."""
    text = (intent or "").strip()
    if not text or not _OR_SPLIT_RE.search(text):
        return [text] if text else []
    parts = re.split(r"或|否则", text)
    return [p.strip(" ，,;；") for p in parts if p.strip()]


def resolve_assert_expects(
    intent: str,
    extras: Optional[dict[str, Any]] = None,
    branches: Optional[list[Any]] = None,
) -> list[tuple[str, str]]:
    """返回 [(expect, 来源描述), ...]. extras.expect 仅作可选覆盖."""
    ex = extras or {}
    expects: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(exp: str, label: str) -> None:
        if exp and exp not in seen:
            seen.add(exp)
            expects.append((exp, label))

    for raw in branches or ex.get("branches") or []:
        if not isinstance(raw, dict):
            continue
        branch_text = str(raw.get("intent") or raw.get("desc") or "").strip()
        exp = str(raw.get("expect") or "").strip() or infer_expect_from_text(branch_text)
        if exp:
            _add(exp, branch_text or exp)

    override = str(
        ex.get("post_submit_expect") or ex.get("expect") or ""
    ).strip()
    if override:
        _add(override, intent or override)
        return expects

    for part in _split_or_intents(intent):
        exp = infer_expect_from_text(part)
        if exp:
            _add(exp, part)

    if not expects:
        exp = infer_expect_from_text(intent)
        if exp:
            _add(exp, intent)

    return expects


def build_submit_snapshot(
    *,
    page: Any,
    url_before: str,
    url_after: str,
    outcome: str,
    api_context: dict[str, Any],
    session_ops_cfg: Optional[dict] = None,
    page_capture: Optional[dict] = None,
    items_after: Optional[list[dict]] = None,
    flat_after: str = "",
    recovered: bool = False,
) -> SubmitSnapshot:
    """提交等待结束后固化 before/after; session_ops_cfg/page_capture 已弃用, 仅保留签名兼容."""
    _ = session_ops_cfg, page_capture, recovered
    flat_before = ""
    id_before, field_before = discover_active_entity(
        api_context, url=url_before, flat_text=flat_before,
    )
    if not flat_after and items_after:
        from .assert_scope import items_flat_text
        flat_after = items_flat_text(items_after)
    elif not flat_after:
        try:
            flat_after = (page.inner_text("body") or "")[:6000]
        except Exception:
            flat_after = ""
    id_after, field_after = discover_page_entity(
        api_context, url=url_after, flat_text=flat_after,
    )
    use_field = field_before or field_after or "entity"
    rec = summarize_recorded_context(api_context, id_before)
    return SubmitSnapshot(
        navigation_outcome=outcome,
        url_before=url_before,
        url_after=url_after,
        entity_field=use_field,
        entity_id_before=id_before,
        entity_id_after=id_after,
        recorded_context=rec,
        awaiting_assert=True,
    )


def _is_detail_url(url: str) -> bool:
    u = (url or "").lower()
    return "/detail" in u or "detail" in u.split("?")[0].rstrip("/").split("/")[-1:]


def eval_submit_expect(
    expect: str, snap: SubmitSnapshot, page_url: str,
) -> tuple[bool, str]:
    """对外: 按通用 expect 枚举判定提交快照."""
    return _eval_expect(expect, snap, page_url)


def _eval_expect(expect: str, snap: SubmitSnapshot, page_url: str) -> tuple[bool, str]:
    exp = (expect or "").strip().lower()
    if exp in ("entity_changed", "task_changed", "resource_changed"):
        if snap.navigation_outcome in _FAIL_OUTCOMES:
            return False, f"提交未生效 ({snap.navigation_outcome})"
        if snap.entity_changed:
            return (
                True,
                f"实体已切换 ({snap.entity_id_before} → {snap.entity_id_after})",
            )
        if snap.navigation_outcome == "resource_id_changed" and snap.entity_id_after:
            return True, f"导航已切换资源 (当前={snap.entity_id_after})"
        return False, (
            f"实体未切换 (前={snap.entity_id_before}, 后={snap.entity_id_after})"
        )
    if exp in ("entity_unchanged", "task_unchanged", "same_entity"):
        if snap.entity_id_before and snap.entity_id_after:
            ok = snap.entity_id_before == snap.entity_id_after
            return ok, (
                f"实体未变 ({snap.entity_id_before})"
                if ok
                else f"实体已变 ({snap.entity_id_before}→{snap.entity_id_after})"
            )
        return snap.navigation_outcome in _FAIL_OUTCOMES, f"outcome={snap.navigation_outcome}"
    if exp in ("navigated_away", "navigated_to_list", "left_detail", "list"):
        if snap.navigation_outcome == "returned_to_list":
            return True, "已回到列表页"
        if not _is_detail_url(page_url):
            return True, f"已离开详情页 ({page_url[:80]})"
        return False, "仍在详情页"
    if exp in ("submit_failed", "submit_not_effective"):
        ok = snap.navigation_outcome in _FAIL_OUTCOMES or (
            snap.entity_id_before
            and snap.entity_id_after
            and snap.entity_id_before == snap.entity_id_after
        )
        return ok, f"提交未推进 (outcome={snap.navigation_outcome})"
    if exp in ("submit_succeeded", "any_success", "advanced"):
        return _eval_submit_advanced(snap, page_url)
    return False, f"未知 expect={expect!r}"


def _eval_submit_advanced(
    snap: SubmitSnapshot, page_url: str,
) -> tuple[bool, str]:
    """提交应推进: 实体切换 / 回列表 / 离开详情 均算成功."""
    if snap.navigation_outcome in _FAIL_OUTCOMES:
        return False, f"提交未生效 ({snap.navigation_outcome})"
    if snap.navigation_outcome == "returned_to_list":
        return True, "提交后已回到列表"
    if snap.entity_changed:
        return (
            True,
            f"提交后实体已切换 ({snap.entity_id_before} → {snap.entity_id_after})",
        )
    if snap.navigation_outcome in ("resource_id_changed", "route_changed"):
        return True, f"提交后页面已导航 ({snap.navigation_outcome})"
    if not _is_detail_url(page_url):
        return True, "提交后已离开详情页"
    if (
        snap.entity_id_before
        and snap.entity_id_after
        and snap.entity_id_before == snap.entity_id_after
    ):
        return False, f"提交未推进 (实体仍为 {snap.entity_id_before})"
    return False, f"提交后状态不明确 (outcome={snap.navigation_outcome})"


def _looks_like_content_assert(intent: str, value: str = "") -> bool:
    """字面量/文案类断言, 不走提交结局程序判定."""
    if (value or "").strip():
        return True
    return bool(re.search(r"包含|显示|出现|存在|等于|为|不含|没有", intent or ""))


def try_post_submit_eval(
    *,
    intent: str,
    extras: Optional[dict[str, Any]],
    snap: Optional[SubmitSnapshot],
    page_url: str,
    branches: Optional[list[Any]] = None,
) -> Optional[tuple[bool, str]]:
    """有提交快照且处于 awaiting_assert 时, 框架自动判定, 无需业务配置."""
    if snap is None or not snap.navigation_outcome:
        return None
    if not snap.awaiting_assert:
        return None

    ex = extras or {}
    branch_list = branches or ex.get("branches") or []
    expects = resolve_assert_expects(intent, ex, branch_list)

    if expects:
        last_msg = ""
        for exp, label in expects:
            ok, msg = _eval_expect(exp, snap, page_url)
            last_msg = msg
            if ok:
                return True, f"提交后断言: {msg} [{label}]"
        return False, f"提交后断言: 无期望满足 ({last_msg})"

    value = str(ex.get("value") or "").strip()
    if _looks_like_content_assert(intent, value):
        return None

    # 紧跟提交的断言, 意图未写明分支: 默认「提交应推进」
    ok, msg = _eval_submit_advanced(snap, page_url)
    prefix = "提交后断言" if ok else "提交后断言未满足"
    return ok, f"{prefix}: {msg}"

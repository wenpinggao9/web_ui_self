"""条件/可选步骤: 目标未出现时跳过, 不触发失败重试 (对齐 V3 optional_step)."""
from __future__ import annotations

import re
from typing import Any, Optional

from ..planning import PlannedAction

OPTIONAL_STEP_PREFIX_RE = re.compile(
    r"^(若|如果|假如|当.+时，|当.+时)",
)
_OPTIONAL_INTENT_RE = re.compile(
    r"^(若|如果|假如|当.*时)|若出现|如果出现|若无.*则跳过|否则跳过|不存在则跳过",
    re.I,
)
_QUOTED_RE = re.compile(r"[「『\"']([^」』\"']+)[」』\"']")
_DISPATCH_SKIP_HINTS = (
    "无法解析元素",
    "未找到匹配",
    "找不到元素",
    "找不到",
    "未找到",
    "resolver_failed",
    "无效的 node_index",
)


def is_optional_step_text(step: str) -> bool:
    return bool(OPTIONAL_STEP_PREFIX_RE.match((step or "").strip()))


def is_optional_action(action: PlannedAction) -> bool:
    extras = action.extras or {}
    if extras.get("optional") is True:
        return True
    intent = (action.intent or "").strip()
    return bool(intent and _OPTIONAL_INTENT_RE.search(intent))


def tag_optional_actions_from_steps(
    actions: list[PlannedAction],
    steps: list[str],
) -> list[PlannedAction]:
    """用例步骤含「若…」时, 为对应 action 标 extras.optional."""
    if not steps:
        return actions
    optional_steps = [s for s in steps if is_optional_step_text(s)]
    if not optional_steps:
        return actions

    step_signatures: list[tuple[str, list[str]]] = []
    for step in optional_steps:
        quoted = [m.group(1).strip() for m in _QUOTED_RE.finditer(step) if m.group(1).strip()]
        keywords = [
            k
            for k in re.findall(r"[\u4e00-\u9fff]{2,}", step)
            if k not in ("若出现", "若当前", "点击", "勾选", "按钮", "菜单", "进入", "页面")
        ]
        sig = list(dict.fromkeys(quoted + keywords[:6]))
        step_signatures.append((step, sig))

    op_actions = [a for a in actions if not a.is_assert()]
    sig_idx = 0
    for act in op_actions:
        if is_optional_action(act):
            continue
        intent = act.intent or ""
        matched = False
        while sig_idx < len(step_signatures) and not matched:
            _step, sigs = step_signatures[sig_idx]
            if not sigs:
                sig_idx += 1
                continue
            if any(s in intent for s in sigs):
                act.extras = {**(act.extras or {}), "optional": True}
                matched = True
            else:
                sig_idx += 1
        if not act.extras.get("optional"):
            for _step, sigs in step_signatures:
                if sigs and any(s in intent for s in sigs):
                    act.extras = {**(act.extras or {}), "optional": True}
                    break
    return actions


def _target_phrases(intent: str) -> list[str]:
    phrases = [m.group(1).strip() for m in _QUOTED_RE.finditer(intent) if m.group(1).strip()]
    if phrases:
        return phrases
    cleaned = re.sub(r"^(若出现|如果出现|若|当)", "", intent).strip()
    cleaned = re.sub(r"(复选框|按钮|菜单|链接|弹窗|对话框)$", "", cleaned).strip()
    if len(cleaned) >= 4:
        return [cleaned[:80]]
    return []


def optional_target_absent(page: Any, action: PlannedAction) -> bool:
    """页面上找不到 intent 所指目标文案 → 条件不满足, 可跳过."""
    phrases = _target_phrases(action.intent or "")
    if not phrases:
        return False
    try:
        body = page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        return False
    return not any(p in body for p in phrases)


def should_skip_optional_step(
    page: Any,
    action: PlannedAction,
    dispatch_ok: bool,
    dispatch_message: str = "",
) -> bool:
    if not is_optional_action(action):
        return False
    if dispatch_ok:
        return False
    msg = str(dispatch_message or "")
    if not any(h in msg for h in _DISPATCH_SKIP_HINTS):
        return False
    return optional_target_absent(page, action)

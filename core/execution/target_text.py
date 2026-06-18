"""从分发目标 HTML / 消息中提取真实文案, 回填 click 的 action.value 供 codegen 纠偏."""
from __future__ import annotations

import re
from typing import Optional

from ..planning import PlannedAction

_TARGET_HTML_IN_MSG = re.compile(r"\|\s*实际目标:\s*(.+)\s*$", re.S)


def extract_target_text(target_html: Optional[str]) -> Optional[str]:
    """从元素 outerHTML 片段提取可见文案 (优先 title / option-content)."""
    if not target_html:
        return None
    html = target_html.strip()
    m = re.search(r'\btitle=["\']([^"\']+)["\']', html, re.I)
    if m:
        t = m.group(1).strip()
        if t:
            return t[:80]
    m = re.search(r"option-content[^>]*>([^<]+)<", html, re.I)
    if m:
        t = m.group(1).strip()
        if t:
            return t[:80]
    m = re.search(r'\baria-label=["\']([^"\']+)["\']', html, re.I)
    if m:
        t = m.group(1).strip()
        if t:
            return t[:80]
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80] if text else None


def extract_target_text_from_message(message: Optional[str]) -> Optional[str]:
    """从 dispatch 消息 '... | 实际目标: <html>' 中提取文案."""
    if not message:
        return None
    m = _TARGET_HTML_IN_MSG.search(message)
    if not m:
        return None
    return extract_target_text(m.group(1))


def intent_click_hint(intent: str) -> Optional[str]:
    """从 click 意图中提取引号内的目标文案 (取最后一处)."""
    quotes = re.findall(
        r"['\"「」『』""'']([^'\"「」『』""'']+)['\"「」『』""'']",
        intent or "",
    )
    if quotes:
        return quotes[-1].strip()
    return None


def should_backfill_click_value(intent: str, actual: str) -> bool:
    """仅当页面真实文案比 intent 中的简称更具体时回填."""
    if not actual:
        return False
    hint = intent_click_hint(intent)
    if not hint:
        return False
    if hint == actual:
        return False
    return hint in actual and len(actual) > len(hint)


def backfill_click_value(
    action: PlannedAction,
    actual: Optional[str],
    *,
    force: bool = False,
) -> bool:
    """将更具体的页面文案写入 action.value; 返回是否已回填."""
    if action.type != "click" or not actual:
        return False
    if action.value and not force:
        return False
    if force or should_backfill_click_value(action.intent, actual):
        action.value = actual.strip()[:80]
        return True
    return False


def backfill_click_from_html(action: PlannedAction, target_html: Optional[str]) -> bool:
    return backfill_click_value(action, extract_target_text(target_html))


def backfill_click_from_dispatch(
    action: PlannedAction,
    message: Optional[str],
    suggested_value: Optional[str] = None,
) -> bool:
    """优先 dispatch 消息中的实际目标, 其次 post_check 建议值."""
    actual = extract_target_text_from_message(message) or (
        suggested_value.strip()[:80] if suggested_value and suggested_value.strip() else None
    )
    if not actual:
        return False
    if backfill_click_value(action, actual):
        return True
    if suggested_value and suggested_value.strip():
        return backfill_click_value(action, suggested_value.strip()[:80], force=True)
    return False

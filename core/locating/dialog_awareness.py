"""弹窗感知: intent 指向弹窗按钮但 DOM 无弹窗时重触发 (对齐 V3)."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_DIALOG_BTN_KW = ("确定", "取消", "确认", "关闭", "知道了", "ok", "cancel", "confirm", "close")
_TRIGGER_KW = (
    "支付", "提交", "删除", "移除", "确认", "保存", "发布",
    "pay", "submit", "delete", "remove", "confirm", "save", "publish",
)


def intent_targets_dialog_button(intent: str) -> bool:
    if not intent:
        return False
    low = intent.lower()
    return any(k in low for k in _DIALOG_BTN_KW)


def intent_may_trigger_dialog(intent: str) -> bool:
    if not intent:
        return False
    low = intent.lower()
    return any(k in low for k in _TRIGGER_KW)


def has_dialog_nodes(items: list[dict]) -> bool:
    return any(
        str(n.get("role") or "").lower() == "dialog" or bool(n.get("in_dialog"))
        for n in items
    )


def try_retrigger_dialog(
    page: Any,
    intent: str,
    items: list[dict],
    trigger_selector: Optional[str],
    *,
    extract_fn: Callable[[], list[dict]],
) -> tuple[list[dict], bool]:
    """若需弹窗但 DOM 无弹窗, 点击 trigger 并重抽 DOM. 返回 (items, retriggered)."""
    if not trigger_selector or not items:
        return items, False
    if has_dialog_nodes(items) or not intent_targets_dialog_button(intent):
        return items, False
    logger.info(
        "弹窗感知: intent 指向弹窗按钮但无弹窗, 重触发 | trigger=%s",
        trigger_selector[:120],
    )
    try:
        page.locator(trigger_selector).first.click(timeout=3000)
        page.wait_for_timeout(800)
        new_items = extract_fn() or items
        if has_dialog_nodes(new_items):
            logger.info("弹窗感知: 重触发成功 | nodes=%d", len(new_items))
            return new_items, True
        logger.warning("弹窗感知: 触发后仍未检测到弹窗")
    except Exception as exc:
        logger.warning("弹窗感知: 触发失败: %s", exc)
    return items, False

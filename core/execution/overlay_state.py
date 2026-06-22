"""Overlay (dialog/drawer) 生命周期 —— DOM 缓存代际与断言就绪."""
from __future__ import annotations

import re
import time
from typing import Any, Optional

_DIALOG_VISIBLE = (
    '.ant-modal-wrap:visible, .el-dialog:visible, [role="dialog"]:visible'
)
_OVERLAY_ASSERT_RE = re.compile(
    r"弹窗|对话框|抽屉|浮层|modal|dialog|overlay", re.I,
)


def snapshot_overlay(page: Any) -> dict[str, Any]:
    """当前可见 overlay 的开关与内容指纹."""
    try:
        wrap = page.locator(_DIALOG_VISIBLE)
        if wrap.count() == 0:
            return {"open": False, "fingerprint": "", "char_len": 0}
        text = wrap.first.inner_text(timeout=2000).strip()
        return {"open": True, "fingerprint": text[:800], "char_len": len(text)}
    except Exception:
        return {"open": False, "fingerprint": "", "char_len": 0}


def overlay_cache_stale(page: Any, cached_state: Optional[dict]) -> bool:
    """URL 未变但 overlay 开关或内容变化 → 缓存失效."""
    if not cached_state or "overlay" not in cached_state:
        return False
    prev = dict(cached_state.get("overlay") or {})
    cur = snapshot_overlay(page)
    if cur.get("open") != prev.get("open"):
        return True
    if cur.get("open") and cur.get("fingerprint") != prev.get("fingerprint"):
        return True
    return False


def assert_targets_overlay_content(intent: str) -> bool:
    """断言 intent 是否声明在 overlay 内校验."""
    return bool(_OVERLAY_ASSERT_RE.search(intent or ""))


def wait_for_overlay_content(
    page: Any,
    *,
    min_chars: int = 10,
    timeout_ms: int = 8000,
) -> bool:
    """等待可见 overlay 内出现足够文本 (异步加载)."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        snap = snapshot_overlay(page)
        if snap.get("open") and int(snap.get("char_len") or 0) >= min_chars:
            return True
        try:
            page.wait_for_timeout(250)
        except Exception:
            break
    snap = snapshot_overlay(page)
    return bool(snap.get("open")) and int(snap.get("char_len") or 0) >= min_chars

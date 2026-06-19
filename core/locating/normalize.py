"""定位链共用: URL / 意图 归一化, 选择器在页面上的校验."""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NUM = re.compile(r"^\d+$")
# 这些参数通常只影响鉴权/防重放, 不应进入页面结构级缓存 key.
_TRACK_PARAMS = ("token", "timestamp", "ts", "_t", "sign", "nonce")


def normalize_url(url: str) -> str:
    """hash 路由取 fragment; 纯数字段/UUID → {id}; 丢弃跟踪参数."""
    if not url:
        return ""
    parsed = urlparse(url)
    # 前端 SPA 通常把真实路由放在 hash fragment, 优先使用 fragment.
    frag = parsed.fragment or ""
    path = frag.split("?")[0] if frag else (parsed.path or "")
    segs = [s for s in path.split("/") if s]
    out = []
    for s in segs:
        # 详情页 ID 不参与定位经验区分, 同模板页面可以复用选择器.
        if _NUM.match(s) or _UUID.match(s):
            out.append("{id}")
        else:
            out.append(s)
    return "/" + "/".join(out)


def normalize_intent(intent: str) -> str:
    """去引号/多余空白, 便于做缓存键."""
    # 文案里的中英文引号和空格差异不影响用户真实意图.
    s = re.sub(r"[\"'“”‘’「」『』]", "", intent or "")
    s = re.sub(r"\s+", "", s)
    return s.strip()


def validate_selector(page: Any, info: dict, timeout_ms: int = 1500) -> bool:
    """校验定位信息能在页面找到可见元素 (语义 API 或 css)."""
    from .playwright_api import validate_locator

    return validate_locator(page, info, timeout_ms=timeout_ms)


def skip_locator_persistence(action_type: str) -> bool:
    """assert_text 走 DOM/语义断言, 不读写 L1/L2/L4 选择器经验."""
    return (action_type or "").strip() == "assert_text"

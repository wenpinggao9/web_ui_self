"""定位链共用: URL / 意图 归一化, 选择器在页面上的校验 (对齐 V3)."""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
)
_NUM = re.compile(r"^\d+$")
_LONG_HEX_ID = re.compile(
    r"^(?=[0-9a-f]*\d)[0-9a-f]{8,}(-[0-9a-f]+)*$",
    re.IGNORECASE,
)
_QUOTE_RE = re.compile(
    r"""["'""''\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f]""",
)
_INTENT_MAX_LEN = 60
_IGNORE_QUERY_PREFIXES = ("token", "session", "_t", "timestamp", "ts=", "utm_", "ref", "from=")


def _parametrize_path(path: str) -> str:
    if not path or path == "/":
        return "/"
    segments = path.strip("/").split("/")
    out: list[str] = []
    for seg in segments:
        if not seg:
            continue
        if _NUM.match(seg) or _UUID.match(seg) or _LONG_HEX_ID.match(seg):
            out.append("{id}")
        else:
            out.append(seg)
    return "/" + "/".join(out) if out else "/"


def normalize_url(url: str) -> str:
    """V3 对齐: hash 路由、路径参数化、过滤跟踪 query."""
    if not url:
        return "/"
    try:
        parsed = urlparse(url)
    except Exception:
        return "/"

    fragment = (parsed.fragment or "").strip()
    if fragment.startswith("/"):
        route = fragment
    elif fragment.startswith("#/"):
        route = fragment[1:]
    else:
        route = parsed.path or "/"

    route = route.strip()
    if not route or route in ("#", "/#", "/"):
        return "/"
    if not route.startswith("/"):
        route = "/" + route

    query_suffix = ""
    if "?" in route:
        path_part, query_part = route.split("?", 1)
        kept = [
            p for p in query_part.split("&")
            if p and not any(p.lower().startswith(pref) for pref in _IGNORE_QUERY_PREFIXES)
        ]
        route = path_part
        if kept:
            query_suffix = "?" + "&".join(kept)

    return _parametrize_path(route) + query_suffix


def normalize_url_legacy(url: str) -> str:
    """旧版 URL 归一化 (兼容历史 L1/L2 key)."""
    if not url:
        return ""
    parsed = urlparse(url)
    frag = parsed.fragment or ""
    path = frag.split("?")[0] if frag else (parsed.path or "")
    segs = [s for s in path.split("/") if s]
    out: list[str] = []
    for s in segs:
        if _NUM.match(s) or _UUID.match(s):
            out.append("{id}")
        else:
            out.append(s)
    return "/" + "/".join(out)


def normalize_intent_relaxed(intent: str) -> str:
    """V3 记忆库 key: 小写 + 去空格/引号 + 去末尾标点."""
    s = re.sub(r"\s+", "", (intent or "").lower())
    s = re.sub(r"[「」『』\u201c\u201d\u2018\u2019]", "", s)
    s = _QUOTE_RE.sub("", s)
    s = re.sub(r"[。，、；：！？.,;:!?]+$", "", s)
    return s.strip()[:_INTENT_MAX_LEN]


def normalize_intent_cache(intent: str) -> str:
    """V3 短期缓存 key: 小写 + 合并空白为单空格."""
    s = re.sub(r"\s+", " ", (intent or "").lower()).strip()
    return s[:_INTENT_MAX_LEN]


def normalize_intent(intent: str) -> str:
    """默认 intent 归一化 (= relaxed, 供 L2/L4 等)."""
    return normalize_intent_relaxed(intent)


def normalize_intent_legacy(intent: str) -> str:
    """旧版 intent 归一化 (兼容历史 key)."""
    s = _QUOTE_RE.sub("", intent or "")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[。，、；：！？.,;:!?]+$", "", s)
    return s.strip()[:_INTENT_MAX_LEN]


def validate_selector(page: Any, info: dict, timeout_ms: int = 1500) -> bool:
    """校验定位信息能在页面找到可见元素 (语义 API 或 css)."""
    from .playwright_api import validate_locator

    return validate_locator(page, info, timeout_ms=timeout_ms)


def skip_locator_persistence(action_type: str) -> bool:
    """assert_text 走 DOM/语义断言, 不读写 L1/L2 选择器经验."""
    return (action_type or "").strip() == "assert_text"

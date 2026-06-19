"""Playwright 语义定位 API —— 按场景选用 get_by_label/role/text/placeholder 或 css.

定位链统一产出 locator_info dict, 执行层通过 resolve_locator() 构建 Locator.
旧数据仅含 selector 字段时, 自动推断 method 并保持兼容.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_METHODS = frozenset({"css", "label", "role", "text", "placeholder", "testid"})


def normalize_info(info: dict) -> dict:
    """归一化定位信息, 补齐 method 与 selector(用作缓存键/排除键)."""
    if not info:
        return {"method": "css", "selector": "", "nth": 0}
    out = dict(info)
    out["nth"] = out.get("nth", 0) or 0
    method = str(out.get("method") or "").strip().lower()
    if method in _METHODS:
        out["method"] = method
        out["selector"] = out.get("selector") or _canonical_key(out)
        return out
    sel = str(out.get("selector") or "").strip()
    return infer_from_selector(sel, out.get("nth", 0), out.get("in_dialog", False))


def infer_from_selector(sel: str, nth: int = 0, in_dialog: bool = False) -> dict:
    """从旧版 selector 字符串推断更合适的 Playwright API."""
    sel = (sel or "").strip()
    base: dict = {"nth": nth, "in_dialog": in_dialog, "selector": sel}

    m = re.match(r'text="([^"]+)"$', sel)
    if m:
        return {**base, "method": "text", "name": m.group(1), "exact": True}

    m = re.search(r':has-text\("([^"]+)"\)', sel)
    if m:
        text = m.group(1)
        if "ant-select-item-option" in sel or "el-select-dropdown__item" in sel or '[role="option"]' in sel:
            return {**base, "method": "role", "role": "option", "name": text, "exact": False}
        if sel.startswith("button"):
            return {**base, "method": "role", "role": "button", "name": text, "exact": False}
        return {**base, "method": "text", "name": text, "exact": False}

    m = re.match(r"text=([^\"'\[\]=:]+)$", sel)
    if m:
        return {**base, "method": "text", "name": m.group(1), "exact": False}

    m = re.match(r'(?:input|textarea)\[placeholder="([^"]+)"\]', sel)
    if m:
        return {**base, "method": "placeholder", "name": m.group(1), "exact": False}

    m = re.match(r'\[data-testid="([^"]+)"\]', sel)
    if m:
        return {**base, "method": "testid", "name": m.group(1)}

    m = re.match(r'role=([^[\]]+)\[name="([^"]+)"\]', sel)
    if m:
        return {**base, "method": "role", "role": m.group(1), "name": m.group(2), "exact": False}

    m = re.match(r'\[role="([^"]+)"\]\[aria-label="([^"]+)"\]', sel)
    if m:
        return {**base, "method": "role", "role": m.group(1), "name": m.group(2), "exact": False}

    return {**base, "method": "css"}


def _canonical_key(info: dict) -> str:
    """生成用于缓存/排除/日志的稳定键."""
    method = info.get("method", "css")
    if method == "css":
        return str(info.get("selector") or "")
    if method == "label":
        return f'label:"{info.get("name", "")}"'
    if method == "role":
        return f'role={info.get("role", "")}[name="{info.get("name", "")}"]'
    if method == "text":
        exact = "exact" if info.get("exact") else "fuzzy"
        return f'text:{exact}:"{info.get("name", "")}"'
    if method == "placeholder":
        return f'placeholder:"{info.get("name", "")}"'
    if method == "testid":
        return f'testid:"{info.get("name", "")}"'
    return str(info.get("selector") or "")


def info_key(info: dict) -> str:
    return normalize_info(info).get("selector") or ""


def resolve_locator(page: Any, info: dict):
    """根据 locator_info 构建 Playwright Locator (语义 API 优先)."""
    spec = normalize_info(info)
    method = spec["method"]
    nth = spec.get("nth", 0) or 0
    root = page
    frame_loc = spec.get("_frame_locator") or spec.get("frame_locator")
    if frame_loc:
        root = page.frame_locator(frame_loc)

    if method == "label":
        loc = root.get_by_label(spec["name"], exact=bool(spec.get("exact", False)))
    elif method == "role":
        kwargs: dict = {}
        if spec.get("name"):
            kwargs["name"] = spec["name"]
        if "exact" in spec:
            kwargs["exact"] = bool(spec["exact"])
        loc = root.get_by_role(spec["role"], **kwargs)
    elif method == "text":
        loc = root.get_by_text(spec["name"], exact=bool(spec.get("exact", False)))
    elif method == "placeholder":
        loc = root.get_by_placeholder(spec["name"], exact=bool(spec.get("exact", False)))
    elif method == "testid":
        loc = root.get_by_test_id(spec["name"])
    else:
        loc = root.locator(spec.get("selector") or "body")

    scope = spec.get("filter")
    if scope and method != "css":
        loc = loc.filter(has=root.locator(scope))

    return loc.nth(nth) if nth else loc.first


def validate_locator(page: Any, info: dict, timeout_ms: int = 1500) -> bool:
    """校验定位信息能在页面上找到可见元素."""
    try:
        resolve_locator(page, info).wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def try_candidates(
    page: Any,
    candidates: list[dict],
    exclude: Optional[set[str]] = None,
) -> Optional[dict]:
    """按优先级尝试候选定位, 返回第一个可见的归一化 info."""
    excl = exclude or set()
    for raw in candidates:
        info = normalize_info(raw)
        if info_key(info) in excl:
            continue
        if validate_locator(page, info):
            return info
    return None


def info_to_python_expr(page_var: str, info: dict) -> str:
    """生成可独立运行的 Playwright Python 定位表达式."""
    spec = normalize_info(info)
    method = spec["method"]
    name = spec.get("name", "")
    py_name = repr(name)

    if method == "label":
        exact = ", exact=True" if spec.get("exact") else ""
        expr = f"{page_var}.get_by_label({py_name}{exact})"
    elif method == "role":
        role = repr(spec.get("role", ""))
        exact = ", exact=True" if spec.get("exact") else ""
        if name:
            expr = f"{page_var}.get_by_role({role}, name={py_name}{exact})"
        else:
            expr = f"{page_var}.get_by_role({role})"
    elif method == "text":
        exact = ", exact=True" if spec.get("exact") else ", exact=False"
        expr = f"{page_var}.get_by_text({py_name}{exact})"
    elif method == "placeholder":
        exact = ", exact=True" if spec.get("exact") else ", exact=False"
        expr = f"{page_var}.get_by_placeholder({py_name}{exact})"
    elif method == "testid":
        expr = f"{page_var}.get_by_test_id({py_name})"
    else:
        expr = f'{page_var}.locator({repr(spec.get("selector", ""))})'

    scope = spec.get("filter")
    if scope and method != "css":
        expr = f'{expr}.filter(has={page_var}.locator({repr(scope)}))'
    nth = spec.get("nth", 0) or 0
    if nth:
        return f"{expr}.nth({nth})"
    return f"{expr}.first"

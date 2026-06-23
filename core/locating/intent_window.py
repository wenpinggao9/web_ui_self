"""L3 意图窗口 —— 从完整 semantic_items 中抽取与 intent 最相关的 N 条给 element_decide LLM.

保留原始 [index] 编号, decide() 仍用完整 items 列表解析 LLM 返回的 index.
"""
from __future__ import annotations

import re

_DIALOG_RE = re.compile(r"弹窗|对话框|抽屉|modal|dialog", re.I)
_FORM_RE = re.compile(r"筛选区|筛选|搜索框|表单|form", re.I)
_TABLE_RE = re.compile(r"表格|列表|tbody|行内|该行|第一行|首行|row", re.I)
_DROPDOWN_RE = re.compile(r"下拉|选项|popup|listbox|combobox", re.I)
_MENU_RE = re.compile(r"侧栏|菜单|导航|menu", re.I)

_CLICK_TAGS = frozenset({
    "button", "a", "label", "input", "span", "option", "container", "dropdown",
})
_FILL_TAGS = frozenset({"input", "textarea"})


def extract_target_texts(intent: str) -> list[str]:
    """intent 中所有引号内文案 (去重保序)."""
    if not intent:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for pat in (r"[「\"'](.*?)[」\"']", r'"([^"]+)"', r"'([^']+)'"):
        for m in re.finditer(pat, intent):
            t = (m.group(1) or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    for pat in (
        r"(?:弹窗|对话框|抽屉)中的\s*[\"']?(.+?)[\"']?\s*下拉",
        r"(?:勾选|取消勾选|选中)\s*[\"']?(.+?)[\"']?\s*(?:复选框|checkbox)?",
    ):
        m = re.search(pat, intent)
        if m and (m.group(1) or "").strip():
            t = m.group(1).strip()
            if t not in seen:
                seen.add(t)
                out.insert(0, t)
    return out


def _node_blob(it: dict) -> str:
    parts = [
        str(it.get("text") or ""),
        str(it.get("placeholder") or ""),
        str(it.get("name") or ""),
        str(it.get("id") or ""),
        str(it.get("value") or ""),
        str(it.get("role") or ""),
        str(it.get("class") or ""),
    ]
    # 归一化: 去空格/空白, 使「提 交」和「提交」等价
    return re.sub(r"\s+", "", " ".join(parts)).lower()


def _score_item(
    it: dict,
    *,
    targets: list[str],
    intent: str,
    action_type: str,
) -> float:
    blob = _node_blob(it)
    tag = (str(it.get("tag") or "")).lower()
    typ = (str(it.get("type") or "")).lower()
    score = 0.0

    for t in targets:
        tl = re.sub(r"\s+", "", t.lower())  # 归一化: 去空格, 使「提 交」和「提交」等价
        if not tl:
            continue
        if tl in blob:
            score += 12.0
        elif any(part in blob for part in tl.split() if len(part) >= 2):
            score += 6.0

    if _DIALOG_RE.search(intent):
        if it.get("in_dialog"):
            score += 8.0
    if _FORM_RE.search(intent):
        if it.get("in_form"):
            score += 7.0
    if _TABLE_RE.search(intent):
        if tag in ("table", "th", "td", "tr"):
            score += 6.0
        if "table" in blob or "tbody" in blob:
            score += 4.0
    if _DROPDOWN_RE.search(intent):
        if tag in ("container", "dropdown", "option") or it.get("haspopup"):
            score += 6.0
    if _MENU_RE.search(intent):
        if tag in ("a", "span", "li") and it.get("role") in ("menuitem", "link", "tab", ""):
            score += 3.0

    act = (action_type or "").strip().lower()
    if act == "click":
        if tag in _CLICK_TAGS:
            score += 2.0
        if tag == "button" or it.get("role") == "button":
            score += 1.0
    elif act == "fill":
        if tag in _FILL_TAGS and not it.get("readOnly"):
            score += 4.0
        if it.get("id"):
            score += 3.0
        if tag in _CLICK_TAGS and typ == "radio":
            score -= 2.0

    if it.get("hidden"):
        score -= 5.0
    elif not it.get("in_viewport", True):
        score -= 1.0

    return score


def pick_intent_window_indices(
    items: list[dict],
    intent: str,
    action_type: str,
    *,
    limit: int = 80,
) -> list[int]:
    """按 intent 相关性选出最多 limit 个原始下标 (升序, 含邻近控件扩展)."""
    n = len(items)
    if n <= limit:
        return list(range(n))

    targets = extract_target_texts(intent)
    scored: list[tuple[float, int]] = []
    for i, it in enumerate(items):
        s = _score_item(it, targets=targets, intent=intent, action_type=action_type)
        if s > 0:
            scored.append((s, i))

    if not scored:
        return list(range(min(limit, n)))

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked: set[int] = set()
    ordered: list[int] = []

    def _take(idx: int) -> None:
        if 0 <= idx < n and idx not in picked:
            picked.add(idx)
            ordered.append(idx)

    for _, idx in scored:
        if len(picked) >= limit:
            break
        _take(idx)
        it = items[idx]
        tag = (str(it.get("tag") or "")).lower()
        if tag == "label" and targets:
            for j in range(idx + 1, min(idx + 6, n)):
                near = items[j]
                if (str(near.get("tag") or "")).lower() == "input":
                    _take(j)
                    if len(picked) >= limit:
                        break

    if len(picked) < limit:
        for i in range(n):
            if len(picked) >= limit:
                break
            _take(i)

    ordered.sort()
    return ordered[:limit]

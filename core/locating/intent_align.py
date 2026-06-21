"""元素↔意图校验与 climb 打分 (菜单导航 / fill 字段 / 弹窗优先)."""
from __future__ import annotations

import re
from typing import Any, Optional

from .action_type_filter import item_matches_action_type

_MENU_NAV_KEYWORDS = (
    "切换到", "切换至", "进入", "打开", "侧栏", "顶栏", "菜单", "模块", "导航",
)


def extract_feature_menu_target_from_intent(intent: str) -> Optional[str]:
    """从菜单导航 intent 提取目标模块名 (优先引号内文案)."""
    if not intent:
        return None
    for pattern in (r"「([^」]+)」", r'"([^"]+)"', r"'([^']+)'"):
        m = re.search(pattern, intent)
        if m and (m.group(1) or "").strip():
            return m.group(1).strip()
    for pattern in (
        r"切换到\s*([^\s，。]+?)(?:模块|页面|菜单|$)",
        r"进入\s*([^\s，。]+?)(?:模块|页面|菜单|$)",
        r"点击\s*([^\s，。]+?)(?:菜单|模块|入口)",
    ):
        m = re.search(pattern, intent)
        if m and (m.group(1) or "").strip():
            return m.group(1).strip()
    return None


def menu_node_matches_target(node: dict, target: str) -> bool:
    """菜单导航校验: 节点语义须包含目标模块名."""
    if not target:
        return True
    text = str(node.get("text") or "")
    name = str(node.get("name") or "")
    placeholder = str(node.get("placeholder") or "")
    role = str(node.get("role") or "")
    merged = f"{text} {name} {placeholder} {role}"
    return target in merged


def detect_feature_titles_menu_nav(
    intent: str,
    *,
    extras: Optional[dict] = None,
    feature_titles: Optional[list[str]] = None,
) -> bool:
    """判断本步是否为侧栏/顶栏模块菜单导航."""
    ex = extras or {}
    if ex.get("feature_titles_menu_nav") is True:
        return True
    text = intent or ""
    if not any(k in text for k in _MENU_NAV_KEYWORDS):
        return False
    if extract_feature_menu_target_from_intent(text):
        return True
    for title in feature_titles or []:
        t = (title or "").strip()
        if t and t in text:
            return True
    return False


def fill_field_match_score(fill_field: str, placeholder: str, aria_label: str = "") -> float:
    """字段名与 placeholder/aria 的匹配分."""
    fl = (fill_field or "").strip().lower()
    if not fl:
        return 0.0
    ph = (placeholder or "").lower()
    al = (aria_label or "").lower()
    score = 0.0
    if fl in ph:
        score += 5.0
    if fl in al:
        score += 3.0
    if len(fl) >= 4:
        half = len(fl) // 2
        a, b = fl[:half], fl[half:]
        if len(a) >= 2 and a in ph:
            score += 2.0
        if len(b) >= 2 and b in ph:
            score += 2.0
        if len(a) >= 2 and a in al:
            score += 1.5
        if len(b) >= 2 and b in al:
            score += 1.5
    return score


def extract_fill_field(intent: str) -> Optional[str]:
    """从 fill intent 提取字段名."""
    for pattern in (
        r"在\s*[\"']?(.+?)[\"']?\s*输入框中(?:填写|输入)",
        r"在\s*[\"']?(.+?)[\"']?\s*输入框",
        r"在\s*[\"']?(.+?)[\"']?\s*(?:填写|输入)",
    ):
        m = re.search(pattern, intent or "")
        if not m:
            continue
        lbl = (m.group(1) or "").strip().strip("'\"")
        if "的" in lbl:
            lbl = lbl.split("的")[-1].strip()
        lbl = re.sub(r"(输入框|输入栏|文本框|字段)$", "", lbl).strip()
        if lbl:
            return lbl
    return None


def _node_aria_label(node: dict) -> str:
    aria = node.get("aria")
    if isinstance(aria, dict):
        return str(aria.get("aria-label") or aria.get("label") or "")
    return str(node.get("aria-label") or "")


def _parent_chain_indices(items: list[dict], idx: int, max_depth: int = 8) -> set[int]:
    """沿 parent_index 收集祖先与后代邻近 index."""
    related: set[int] = {idx}
    current = idx
    depth = 0
    while depth < max_depth:
        node = items[current] if 0 <= current < len(items) else {}
        parent = node.get("parent_index")
        if not isinstance(parent, int) or parent < 0 or parent >= len(items):
            break
        related.add(parent)
        current = parent
        depth += 1
    children_map: dict[int, list[int]] = {}
    for i, node in enumerate(items):
        parent = node.get("parent_index")
        if isinstance(parent, int) and 0 <= parent < len(items):
            children_map.setdefault(parent, []).append(i)
    queue = [idx]
    visited = {idx}
    depth = 0
    while queue and depth < 2:
        nxt: list[int] = []
        for cur in queue:
            for child in children_map.get(cur, []):
                if child not in visited:
                    visited.add(child)
                    related.add(child)
                    nxt.append(child)
        queue = nxt
        depth += 1
    return related


def climb_to_matching_node(
    items: list[dict],
    anchor_idx: int,
    action_type: Optional[str],
    intent: str,
) -> Optional[int]:
    """二次 LLM 文本锚点后: 在邻近节点中找符合 action_type 且与 intent 最一致的目标."""
    if not items or not action_type or anchor_idx < 0 or anchor_idx >= len(items):
        return None
    if item_matches_action_type(items[anchor_idx], action_type):
        return anchor_idx

    fill_field = extract_fill_field(intent) if (action_type or "").lower() == "fill" else None
    anchor = items[anchor_idx]
    anchor_in_dialog = bool(anchor.get("in_dialog"))
    related = _parent_chain_indices(items, anchor_idx)
    intent_lower = (intent or "").lower()
    dialog_intent = any(k in intent_lower for k in ("弹窗", "对话框", "抽屉", "modal", "dialog"))

    best_idx: Optional[int] = None
    best_score = -1.0
    search_range = max(30, len(items) // 4)
    start = max(0, anchor_idx - search_range)
    end = min(len(items), anchor_idx + search_range + 1)

    for i in range(start, end):
        if i == anchor_idx:
            continue
        node = items[i]
        if not item_matches_action_type(node, action_type):
            continue
        score = 0.0
        if i in related:
            score += 3.0
        if fill_field:
            ph = (node.get("placeholder") or "")
            al = _node_aria_label(node)
            nm = (node.get("name") or "")
            fs = fill_field_match_score(fill_field, ph, al)
            fs = max(fs, fill_field_match_score(fill_field, nm, ""))
            score += fs
        if dialog_intent:
            if node.get("in_dialog"):
                score += 4.0
            if anchor_in_dialog and node.get("in_dialog"):
                score += 2.0
            cls = str(node.get("class") or "").lower()
            if any(k in cls for k in ("dialog", "modal", "drawer", "overlay")):
                score += 1.5
        score -= abs(i - anchor_idx) * 0.05
        if score > best_score:
            best_score = score
            best_idx = i

    if fill_field and best_idx is not None:
        bn = items[best_idx]
        fs = max(
            fill_field_match_score(fill_field, bn.get("placeholder") or "", _node_aria_label(bn)),
            fill_field_match_score(fill_field, bn.get("name") or "", ""),
        )
        if fs < 0.3:
            return None

    return best_idx if best_score > 0 else None


def resolve_menu_target(
    intent: str,
    feature_titles: Optional[list[str]] = None,
) -> Optional[str]:
    """菜单 intent 校验用的目标名."""
    target = extract_feature_menu_target_from_intent(intent)
    if target:
        return target
    titles = [t.strip() for t in (feature_titles or []) if (t or "").strip()]
    if titles:
        return titles[-1]
    return None


def validate_menu_node_index(
    items: list[dict],
    index: int,
    intent: str,
    *,
    feature_titles: Optional[list[str]] = None,
) -> bool:
    if index < 0 or index >= len(items):
        return False
    target = resolve_menu_target(intent, feature_titles)
    if not target:
        return True
    return menu_node_matches_target(items[index], target)


def detect_component_library_from_items(items: list[dict], class_features: dict[str, list[str]]) -> Optional[str]:
    """从 semantic_items 的 class 统计识别组件库."""
    if not class_features:
        return None
    hits: dict[str, int] = {}
    for node in items:
        cls = str(node.get("class") or node.get("className") or "").lower()
        if not cls:
            continue
        for lib_name, prefixes in class_features.items():
            for prefix in prefixes:
                if prefix and prefix.lower() in cls:
                    hits[lib_name] = hits.get(lib_name, 0) + 1
                    break
    if not hits:
        return None
    return max(hits, key=hits.get)


def append_element_decide_user_hints(
    base_user: str,
    *,
    action_type: str,
    intent: str,
    feature_titles_menu_nav: bool = False,
) -> str:
    """在 user prompt 末尾追加意图↔元素强约束与场景提示."""
    parts = [base_user.rstrip(), "", "【强约束】选择 index 时必须核对元素 text/placeholder/name/role 与 intent 的一致性."]
    intent_lower = (intent or "").lower()
    at = (action_type or "").lower()

    if feature_titles_menu_nav:
        parts.extend([
            "",
            "【本步为侧栏/顶栏模块导航】请在「点击菜单进入目标模块」与「不跳转」之间二选一:",
            '1. 若当前页面已是目标模块, 或侧栏无安全可点入口, 输出 {"skip_navigation": true, "reason": "..."}',
            "2. 若需点击菜单项, 返回有效 index; 命中节点的 text 须包含 intent 引号内的模块名.",
        ])

    if at == "click":
        button_keywords = ["搜索", "提交", "确认", "取消", "重置", "保存", "删除", "编辑", "添加", "上传", "下载"]
        matched = [kw for kw in button_keywords if kw in intent_lower]
        if matched:
            parts.extend([
                "",
                f"**click 精确匹配**: intent 提到 {', '.join(matched)} 时, 须选 text 与关键词完全一致的 button,",
                "不要选含关键词的长文案 (如「高级搜索」代替「搜索」). 优先 BUTTON 而非 LI/MENUITEM.",
            ])
        if any(k in intent_lower for k in ("弹窗", "对话框", "dialog", "modal", "抽屉")):
            parts.extend([
                "",
                "**弹窗 click**: 必须优先 [弹窗] 标记节点; 文本精确匹配优先于标签类型.",
            ])

    if at == "fill":
        parts.extend([
            "",
            "**fill 提示**: 多输入框时按 intent 区分表单内 vs 搜索栏; 弹窗 intent 优先 [弹窗] 且 placeholder 对齐字段名.",
        ])

    if at == "upload":
        parts.extend([
            "",
            "**upload**: 只选 input[type=file], 不要选触发上传的 button.",
        ])

    parts.append("")
    parts.append("请输出 JSON (格式一/格式二/菜单 skip_navigation).")
    return "\n".join(parts)

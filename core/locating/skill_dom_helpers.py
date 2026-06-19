"""组件 DOM 选择器构建库（L3 / 2B 层）。

基于语义 DOM 动态生成下拉、复选框、树节点、日期等组件的选择器候选，
并提供 L5 节点纠偏用的点击/输入/开关精炼函数。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 语义 DOM 单节点类型别名
SemanticNode = Dict[str, Any]


def _normalize_text(text: Optional[str]) -> str:
    if text is None:
        return ""
    return str(text).strip()


def _escape_xpath_literal(value: str) -> str:
    """将任意文本安全转为 XPath 字面量。"""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"


def _detect_ant_design_from_dom(semantic_dom: List[SemanticNode]) -> bool:
    for node in semantic_dom:
        cls = str(node.get("class") or node.get("className") or "").lower()
        if "ant-" in cls:
            return True
    return False


def _combobox_ids_near_label(semantic_dom: List[SemanticNode], label: str) -> List[str]:
    """按表单项 label 在语义 DOM 中查找邻近 combobox 的 id（如 Ant Design #source）。"""
    label = (label or "").strip()
    if not label:
        return []
    ids: List[str] = []
    for i, node in enumerate(semantic_dom):
        if str(node.get("tag") or "").lower() != "label":
            continue
        if label not in str(node.get("text") or ""):
            continue
        for j in range(i, min(i + 25, len(semantic_dom))):
            child = semantic_dom[j]
            cid = str(child.get("id") or "").strip()
            if str(child.get("role") or "").lower() == "combobox" and cid:
                ids.append(cid)
    return list(dict.fromkeys(ids))


# =============================================================================
# 5a. 下拉 / 级联选项 Selector
# =============================================================================

def _extract_dropdown_option_text_from_intent(intent: str) -> Optional[str]:
    if not intent:
        return None

    def _normalize_option_text(raw: str) -> Optional[str]:
        t = (raw or "").strip().strip('"\'').strip()
        if not t:
            return None
        for sep in ("中的", "中"):
            if sep in t:
                t = t.split(sep)[-1].strip()
        while True:
            nt = re.sub(r"(下拉选项|选项|下拉菜单|下拉框|下拉栏|下拉)$", "", t).strip()
            if nt == t:
                break
            t = nt
        t = t.strip("，。;、")
        if 1 <= len(t) <= 200:
            return t
        return None

    patterns = [
        r"在下拉选项中选择\s*[\"'\u300c]?(.+?)[\"\u300d]?\s*$",
        r"在下拉选项中点击\s*[\"'\u300c]?(.+?)[\"\u300d]?\s*$",
        r"点击.+?下拉(?:栏|框|菜单).*?中的\s*[\"']?(.+?)[\"']?\s*选项",
        r"选择.+?下拉(?:栏|框|菜单).*?中的\s*[\"']?(.+?)[\"']?\s*选项",
        r"点击.+?下拉(?:栏|框|菜单).*?中的\s*[\"']?(.+?)[\"']?\s*下拉选项",
        r"选择.+?下拉(?:栏|框|菜单).*?中的\s*[\"']?(.+?)[\"']?\s*下拉选项",
        r"点击.+?下拉(?:栏|框|菜单).*?中\s*[\"']?(.+?)[\"']?\s*选项",
        r"选择.+?下拉(?:栏|框|菜单).*?中\s*[\"']?(.+?)[\"']?\s*选项",
        r"点击.+?下拉(?:栏|框|菜单).*?中\s*[\"']?(.+?)[\"']?\s*下拉选项",
        r"选择.+?下拉(?:栏|框|菜单).*?中\s*[\"']?(.+?)[\"']?\s*下拉选项",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉选项",
        r"选择\s*[\"']?(.+?)[\"']?\s*下拉选项",
        r"点击\s*[\"']?(.+?)[\"']?\s*选项$",
        r"选择\s*[\"']?(.+?)[\"']?\s*选项$",
    ]
    for pattern in patterns:
        m = re.search(pattern, intent)
        if m:
            text = _normalize_option_text(m.group(1) or "")
            if text:
                return text
    return None


def _append_ant_option_candidates(candidates: List[str], option_text: str) -> None:
    name_escaped = option_text.replace("\\", "\\\\").replace('"', '\\"')
    text_escaped = _escape_xpath_literal(option_text)
    candidates.extend(
        [
            f'.ant-select-dropdown:visible .ant-select-item-option-content:has-text("{name_escaped}")',
            f'.ant-select-dropdown:visible .ant-select-item-option:has-text("{name_escaped}")',
            f'role=listbox >> .ant-select-item-option-content:has-text("{name_escaped}")',
            f"(//*[contains(@class,'ant-select-dropdown') and not(contains(@style,'display: none'))]"
            f"//*[contains(@class,'ant-select-item-option') and contains(normalize-space(.), {text_escaped})])[1]",
        ]
    )


def build_dropdown_option_selector(
    semantic_dom: List[SemanticNode],
    intent: str,
) -> Dict[str, Any]:
    """基于 intent + 语义 DOM 构建下拉/级联选项 selector 候选。"""
    option_text = _extract_dropdown_option_text_from_intent(intent)
    if not option_text:
        return {"selector": None, "candidates": [], "option_text": ""}

    option_text = option_text.strip()[:200]
    name_escaped = option_text.replace("\\", "\\\\").replace('"', '\\"')
    text_escaped = _escape_xpath_literal(option_text)

    roles = {
        (str(node.get("role")).lower())
        for node in semantic_dom
        if node.get("role") is not None
    }
    tags = {
        (str(node.get("tag")).lower())
        for node in semantic_dom
        if node.get("tag") is not None
    }

    candidates: List[str] = []

    has_ant = any(
        "ant-" in str(node.get("class") or node.get("className") or "").lower()
        for node in semantic_dom
    ) or _detect_ant_design_from_dom(semantic_dom)

    if has_ant:
        _append_ant_option_candidates(candidates, option_text)

    if "option" in roles:
        candidates.append(f'role=option[name="{name_escaped}"]')
    if "menuitem" in roles:
        candidates.append(f'role=menuitem[name="{name_escaped}"]')
    if "menuitemcheckbox" in roles:
        candidates.append(f'role=menuitemcheckbox[name="{name_escaped}"]')
    if "treeitem" in roles:
        candidates.append(f'role=treeitem[name="{name_escaped}"]')

    candidates.extend(
        [
            f"(//*[@role='option' and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[@role='menuitem' and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[@role='menuitemcheckbox' and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[@role='treeitem' and contains(normalize-space(.), {text_escaped})])[1]",
        ]
    )

    if "listbox" in roles:
        candidates.append(
            f"(//*[@role='listbox']//*[(@role='option' or @role='menuitem' or @role='menuitemcheckbox' or @role='treeitem') and contains(normalize-space(.), {text_escaped})])[1]"
        )
    if "menu" in roles or "menubar" in roles:
        candidates.append(
            f"(//*[@role='menu' or @role='menubar']//*[(@role='menuitem' or @role='menuitemcheckbox') and contains(normalize-space(.), {text_escaped})])[1]"
        )
    if "tree" in roles:
        candidates.append(
            f"(//*[@role='tree']//*[@role='treeitem' and contains(normalize-space(.), {text_escaped})])[1]"
        )

    if "option" in tags:
        candidates.append(f"(//option[contains(normalize-space(.), {text_escaped})])[1]")

    candidates.extend(
        [
            f"(//*[contains(@class,'option') and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[contains(@class,'menu-item') and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[contains(@class,'dropdown-item') and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[contains(@class,'select-option') and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//li[contains(@class,'el-select-dropdown__item') and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[contains(@class,'el-select-dropdown')]//li[contains(normalize-space(.), {text_escaped})])[1]",
            f"(//li[contains(@class,'el-cascader-node') and contains(normalize-space(.), {text_escaped})])[1]",
        ]
    )

    deduped: List[str] = []
    for s in candidates:
        if s and s not in deduped:
            deduped.append(s)

    return {
        "selector": deduped[0] if deduped else None,
        "candidates": deduped,
        "option_text": option_text,
    }


# =============================================================================
# 5b. 复选框 Selector（非树场景）
# =============================================================================

def _extract_checkbox_target_text_from_intent(intent: str) -> Optional[str]:
    if not intent:
        return None
    patterns = [
        r"(?:勾选|取消勾选|选中|取消选中|点击)\s*[\"']?(.+?)[\"']?\s*(?:树节点|节点|复选框|checkbox)?$",
        r"(?:勾选|取消勾选|选中|取消选中)\s*[\"']?(.+?)[\"']?\s*这一行",
    ]
    for pattern in patterns:
        m = re.search(pattern, intent)
        if m:
            text = (m.group(1) or "").strip().strip('"\'')
            if 1 <= len(text) <= 200:
                return text
    return None


def build_checkbox_selector(
    semantic_dom: List[SemanticNode],
    intent: str,
    target_text: str = "",
) -> Dict[str, Any]:
    """基于 intent + 语义 DOM 构建非树场景的 checkbox selector 候选。"""
    text = (target_text or _extract_checkbox_target_text_from_intent(intent) or "").strip()[:200]
    text_escaped = _escape_xpath_literal(text) if text else None
    name_escaped = text.replace("\\", "\\\\").replace('"', '\\"') if text else ""

    roles = {
        (str(node.get("role")).lower())
        for node in semantic_dom
        if node.get("role") is not None
    }
    tags = {
        (str(node.get("tag")).lower())
        for node in semantic_dom
        if node.get("tag") is not None
    }

    candidates: List[str] = []

    if text:
        if "checkbox" in roles:
            candidates.append(f'role=checkbox[name="{name_escaped}"]')
        if "menuitemcheckbox" in roles:
            candidates.append(f'role=menuitemcheckbox[name="{name_escaped}"]')

        candidates.extend(
            [
                f"(//*[@role='checkbox' and contains(normalize-space(.), {text_escaped})])[1]",
                f"(//*[@role='menuitemcheckbox' and contains(normalize-space(.), {text_escaped})])[1]",
                f"(//*[@role='listbox']//*[(@role='checkbox' or @role='menuitemcheckbox') and contains(normalize-space(.), {text_escaped})])[1]",
                f"(//*[contains(@class,'checkbox') and not(ancestor::*[@role='tree' or @role='treeitem' or contains(@class,'tree')]) and contains(normalize-space(.), {text_escaped})])[1]",
            ]
        )

    if "checkbox" in roles:
        candidates.append("role=checkbox")
    if "menuitemcheckbox" in roles:
        candidates.append("role=menuitemcheckbox")
    if "input" in tags:
        candidates.append("(//input[@type='checkbox'])[1]")
    candidates.extend(
        [
            "(//*[@role='checkbox'])[1]",
            "(//*[@role='menuitemcheckbox'])[1]",
            "(//*[contains(@class,'checkbox') and not(ancestor::*[@role='tree' or @role='treeitem' or contains(@class,'tree')])])[1]",
        ]
    )

    deduped: List[str] = []
    for s in candidates:
        if s and s not in deduped:
            deduped.append(s)

    return {
        "selector": deduped[0] if deduped else None,
        "candidates": deduped,
        "target_text": text,
    }


# =============================================================================
# 5c. 树勾选 Selector
# =============================================================================

def build_tree_checkbox_selector(
    semantic_dom: List[SemanticNode],
    intent: str,
    target_text: str = "",
) -> Dict[str, Any]:
    """基于 intent + 语义 DOM 构建树勾选场景 selector 候选。"""
    text = (target_text or _extract_checkbox_target_text_from_intent(intent) or "").strip()[:200]
    text_escaped = _escape_xpath_literal(text) if text else None
    name_escaped = text.replace("\\", "\\\\").replace('"', '\\"') if text else ""

    roles = {
        (str(node.get("role")).lower())
        for node in semantic_dom
        if node.get("role") is not None
    }
    tags = {
        (str(node.get("tag")).lower())
        for node in semantic_dom
        if node.get("tag") is not None
    }

    candidates: List[str] = []

    if text:
        if "checkbox" in roles:
            candidates.append(f'role=checkbox[name="{name_escaped}"]')
        if "menuitemcheckbox" in roles:
            candidates.append(f'role=menuitemcheckbox[name="{name_escaped}"]')

        candidates.extend(
            [
                f"(//*[contains(normalize-space(.), {text_escaped})]/ancestor::*[@role='treeitem'][1]//*[(@role='checkbox' or @role='menuitemcheckbox') or ((self::label or self::span or self::button) and contains(@class,'checkbox'))])[1]",
                f"(//*[@role='treeitem'][.//*[contains(normalize-space(.), {text_escaped})]]//*[@role='checkbox'])[1]",
                f"(//*[@role='treeitem'][.//*[contains(normalize-space(.), {text_escaped})]]//*[@role='menuitemcheckbox'])[1]",
                f"(//*[@role='treeitem'][.//*[contains(normalize-space(.), {text_escaped})]]//*[self::label or self::span or self::button][contains(@class,'checkbox')])[1]",
                f"(//*[@role='tree']//*[@role='treeitem'][.//*[contains(normalize-space(.), {text_escaped})]]//*[@role='checkbox'])[1]",
                f"(//*[@role='tree']//*[@role='treeitem'][.//*[contains(normalize-space(.), {text_escaped})]]//*[self::label or self::span or self::button][contains(@class,'checkbox')])[1]",
                f"(//*[contains(@class,'tree') and .//*[contains(normalize-space(.), {text_escaped})]]//*[(@role='checkbox') or (@role='menuitemcheckbox')])[1]",
                f"(//*[contains(normalize-space(.), {text_escaped})]/ancestor::*[.//*[contains(normalize-space(.), {text_escaped})] and .//*[contains(@class,'checkbox')]][1]//*[self::label or self::span or self::button][contains(@class,'checkbox')])[1]",
                f"(//*[contains(normalize-space(.), {text_escaped})]/ancestor::*[@role='treeitem' or contains(@class,'tree')][1]//span[contains(@class,'el-checkbox__input')])[1]",
                f"(//*[contains(normalize-space(.), {text_escaped})]/ancestor::*[@role='treeitem' or contains(@class,'tree')][1]//span[contains(@class,'ant-tree-checkbox')])[1]",
                f"(//*[contains(@class,'tree-node') or contains(@class,'treenode')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'checkbox')])[1]",
            ]
        )

    if "treeitem" in roles:
        candidates.append("(//*[@role='treeitem']//*[@role='checkbox'])[1]")
    if "tree" in roles:
        candidates.append("(//*[@role='tree']//*[@role='checkbox'])[1]")
    candidates.extend(
        [
            "(//*[@role='checkbox'])[1]",
            "(//*[@role='menuitemcheckbox'])[1]",
            "(//*[contains(@class,'checkbox')])[1]",
        ]
    )

    deduped: List[str] = []
    for s in candidates:
        if s and s not in deduped:
            deduped.append(s)

    return {
        "selector": deduped[0] if deduped else None,
        "candidates": deduped,
        "target_text": text,
    }


# =============================================================================
# 5d. 树节点展开 / 点击 Selector
# =============================================================================

def _extract_tree_node_click_text_from_intent(intent: str) -> Optional[str]:
    if not intent:
        return None
    patterns = [
        r"(?:点击|展开|收起|折叠)\s*[\"']?(.+?)[\"']?\s*树节点",
        r"(?:点击|展开|收起|折叠)\s*[\"']?(.+?)[\"']?\s*节点",
    ]
    for pattern in patterns:
        m = re.search(pattern, intent)
        if m:
            text = (m.group(1) or "").strip().strip('"\'')
            if 1 <= len(text) <= 200:
                return text
    return None


def build_tree_node_selector(
    semantic_dom: List[SemanticNode],
    intent: str,
    target_text: str = "",
) -> Dict[str, Any]:
    """基于 intent + 语义 DOM 构建树节点展开/点击 selector 候选。"""
    text = (target_text or _extract_tree_node_click_text_from_intent(intent) or "").strip()[:200]
    if not text:
        return {"selector": None, "candidates": [], "target_text": ""}

    text_escaped = _escape_xpath_literal(text)
    name_escaped = text.replace("\\", "\\\\").replace('"', '\\"')

    candidates = [
        f"(//*[contains(@class,'ant-tree-treenode')][.//*[contains(@class,'ant-tree-title') and normalize-space(.)={text_escaped}]]//*[contains(@class,'ant-tree-switcher'))[1]",
        f"(//*[contains(@class,'ant-tree-treenode')][.//*[contains(@class,'ant-tree-title') and normalize-space(.)={text_escaped}]]//*[contains(@class,'ant-tree-switcher')]//*[local-name()='svg'])[1]",
        f"(//div[contains(@class,'el-tree-node__content')][.//*[contains(@class,'custom-tree-node__name') and normalize-space(.)={text_escaped}]]//*[contains(@class,'el-tree-node__expand-icon'))[1]",
        f"(//div[contains(@class,'el-tree-node__content')][.//*[contains(@class,'el-tree-node__label') and normalize-space(.)={text_escaped}]]//*[contains(@class,'el-tree-node__expand-icon'))[1]",
        f"(//div[contains(@class,'el-tree-node__content')][.//p[normalize-space(.)={text_escaped}]]//*[contains(@class,'el-tree-node__expand-icon'))[1]",
        f"(//div[contains(@class,'el-tree-node__content')][.//*[contains(@class,'custom-tree-node__name') and normalize-space(.)={text_escaped}] or .//*[contains(@class,'el-tree-node__label') and normalize-space(.)={text_escaped}] or .//p[normalize-space(.)={text_escaped}]]//*[contains(@class,'el-tree-node__expand-icon')]//*[local-name()='svg'])[1]",
        f"(//*[@role='treeitem'][.//*[contains(@class,'tree-title') and normalize-space(.)={text_escaped}] or .//*[normalize-space(.)={text_escaped}]]//*[contains(@class,'switcher') or contains(@class,'expand-icon') or contains(@class,'caret'))[1]",
        f"(//*[@role='treeitem'][.//*[contains(@class,'tree-title') and normalize-space(.)={text_escaped}] or .//*[normalize-space(.)={text_escaped}]]//*[contains(@class,'switcher') or contains(@class,'expand-icon') or contains(@class,'caret')]//*[local-name()='svg'])[1]",
        f"(//*[@role='treeitem'][.//*[normalize-space(.)={text_escaped}]]//*[@aria-label='caret-down' or @aria-label='caret-right' or contains(@aria-label,'caret'))[1]",
        f'role=treeitem[name="{name_escaped}"]',
        f"(//*[@role='treeitem' and normalize-space(.)={text_escaped}])[1]",
    ]

    deduped: List[str] = []
    for s in candidates:
        if s and s not in deduped:
            deduped.append(s)

    return {
        "selector": deduped[0] if deduped else None,
        "candidates": deduped,
        "target_text": text,
    }


# =============================================================================
# 5e. el-select / ant-select 触发器 XPath
# =============================================================================

def _extract_el_select_trigger_field_from_intent(intent: str) -> str:
    if not intent:
        return ""
    for pattern in (
        r"(?:弹窗|对话框|抽屉|窗口)中的\s*[\"']?(.+?)[\"']?\s*下拉框",
        r"(?:弹窗|对话框|抽屉|窗口)中的\s*[\"']?(.+?)[\"']?\s*下拉菜单",
        r"(?:弹窗|对话框|抽屉|窗口)中的\s*[\"']?(.+?)[\"']?\s*下拉栏",
        r"中的\s*[\"']?(.+?)[\"']?\s*下拉框\s*展开",
        r"中的\s*[\"']?(.+?)[\"']?\s*下拉菜单\s*展开",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉(?:框|栏|菜单)?\s*(?:展开|展开按钮)",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉框",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉菜单",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉栏",
        r"展开\s*[\"']?(.+?)[\"']?\s*下拉框",
        r"展开\s*[\"']?(.+?)[\"']?\s*下拉菜单",
        r"展开\s*[\"']?(.+?)[\"']?\s*下拉栏",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉(?:栏|框|菜单).*?中",
    ):
        m = re.search(pattern, intent)
        if m:
            lab = (m.group(1) or "").strip().strip('"\'')
            if 1 <= len(lab) <= 50:
                return lab
    return ""


def build_el_select_trigger_selector(
    semantic_dom: List[SemanticNode],
    intent: str,
) -> Dict[str, Any]:
    """基于 intent 中的字段名生成下拉触发器 selector 候选（Ant Design + Element UI）。"""
    field = _extract_el_select_trigger_field_from_intent(intent or "")
    if not field:
        return {"selector": None, "candidates": [], "field_label": ""}

    te = _escape_xpath_literal(field.strip())
    candidates: List[str] = []

    if _detect_ant_design_from_dom(semantic_dom):
        for cid in _combobox_ids_near_label(semantic_dom, field):
            candidates.extend(
                [
                    f"#{cid}",
                    f"#{cid} >> xpath=ancestor::div[contains(@class,'ant-select'))[1]",
                ]
            )
        candidates.extend(
            [
                f"(//div[contains(@class,'ant-form-item')][.//label[contains(normalize-space(.), {te})]]//div[contains(@class,'ant-select'))[1]",
                f"(//div[contains(@class,'ant-form-item')][.//label[contains(normalize-space(.), {te})]]//*[@role='combobox'])[1]",
                f"(//div[contains(@class,'ant-form-item')][.//label[contains(normalize-space(.), {te})]]//span[contains(@class,'ant-select-arrow'))[1]",
            ]
        )

    candidates.extend(
        [
            f"(//div[@role='dialog']//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {te})]]//*[contains(@class,'el-select__wrapper'))[1]",
            f"(//div[contains(@class,'el-dialog')]//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {te})]]//*[contains(@class,'el-select__wrapper'))[1]",
            f"(//*[contains(@class,'el-overlay-dialog')]//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {te})]]//*[contains(@class,'el-select__wrapper'))[1]",
            f"(//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {te})]]//*[contains(@class,'el-select__wrapper'))[1]",
            f"(//div[@role='dialog']//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {te})]]//*[contains(@class,'el-select__wrapper')]//input[@role='combobox'])[1]",
            f"(//input[contains(@placeholder, {te})]/ancestor::*[contains(@class,'el-select')][1]//div[contains(@class,'el-select__wrapper'))[1]",
        ]
    )
    deduped: List[str] = []
    for s in candidates:
        if s and s not in deduped:
            deduped.append(s)
    return {
        "selector": deduped[0] if deduped else None,
        "candidates": deduped,
        "field_label": field,
    }


# =============================================================================
# 2A. 节点纠偏 (L5 之后): 在 LLM 初匹配 index 上二次打分
# =============================================================================

def find_nodes_by_text(
    semantic_dom: List[SemanticNode],
    text: str,
    case_sensitive: bool = False,
    exact: bool = False,
) -> List[int]:
    target = text if case_sensitive else text.lower()
    result: List[int] = []
    for idx, node in enumerate(semantic_dom):
        node_text = _normalize_text(node.get("text"))
        haystack = node_text if case_sensitive else node_text.lower()
        if not haystack:
            continue
        if exact and haystack == target:
            result.append(idx)
        elif not exact and target in haystack:
            result.append(idx)
    return result


def climb_ancestors(
    semantic_dom: List[SemanticNode],
    node_index: int,
    max_depth: int = 2,
) -> List[int]:
    ancestors: List[int] = []
    current = node_index
    depth = 0
    while depth < max_depth:
        node = semantic_dom[current]
        parent_index = node.get("parent_index")
        if parent_index is None or not isinstance(parent_index, int):
            break
        if parent_index < 0 or parent_index >= len(semantic_dom):
            break
        ancestors.append(parent_index)
        current = parent_index
        depth += 1
    return ancestors


def choose_best_click_target(
    semantic_dom: List[SemanticNode],
    base_node_index: int,
    intent: str = "",
) -> int:
    intent_lower = (intent or "").lower()
    depth = 4 if any(k in intent_lower for k in ("下拉", "dropdown", "select")) else 2
    ancestors = climb_ancestors(semantic_dom, base_node_index, max_depth=depth)
    candidates: List[int] = [base_node_index] + ancestors
    if not ancestors:
        lo = max(0, base_node_index - 10)
        hi = min(len(semantic_dom), base_node_index + 11)
        candidates.extend(range(lo, hi))
    candidates = list(dict.fromkeys(candidates))
    best_idx = base_node_index
    best_score = -1.0
    for idx in candidates:
        if idx < 0 or idx >= len(semantic_dom):
            continue
        node = semantic_dom[idx]
        tag = (node.get("tag") or "").lower()
        role = (node.get("role") or "").lower()
        class_name = (node.get("class") or "") or ""
        clickable_score = 0.0
        lc = class_name.lower()
        if idx == base_node_index:
            clickable_score += 0.5
        if "el-select__input-calculator" in lc:
            clickable_score -= 12.0
        if "el-select__wrapper" in lc:
            clickable_score += 6.0
        if "el-select" in lc and "el-select__dropdown" not in lc and "el-select-dropdown" not in lc:
            clickable_score += 1.5
        if tag == "input" and role == "combobox" and "el-select__input" in lc:
            clickable_score += 5.0
        if "ant-radio-wrapper" in lc or "ant-checkbox-wrapper" in lc:
            clickable_score += 4.0
        if tag == "button":
            clickable_score += 5.0
        if tag == "a":
            clickable_score += 3.0
        if tag == "input" and (node.get("type") in ("button", "submit", "reset")):
            clickable_score += 4.0
        if "button" in role:
            clickable_score += 4.0
        for kw in ("btn", "button", "clickable", "link", "action"):
            if kw in lc:
                clickable_score += 1.0
        text = _normalize_text(node.get("text"))
        if text and len(text) <= 10:
            for kw in ("按钮", "button", "查看", "详情", "确认", "取消", "保存"):
                if kw in intent_lower:
                    clickable_score += 0.5
                    break
        if clickable_score > best_score:
            best_score = clickable_score
            best_idx = idx
    return best_idx


def choose_best_input_target(
    semantic_dom: List[SemanticNode],
    base_node_index: int,
    intent: str = "",
    expected_text: str = "",
) -> int:
    children_map: Dict[int, List[int]] = {}
    for idx, node in enumerate(semantic_dom):
        parent_index = node.get("parent_index")
        if isinstance(parent_index, int) and 0 <= parent_index < len(semantic_dom):
            children_map.setdefault(parent_index, []).append(idx)

    intent_lower = (intent or "").lower()
    expected_lower = (expected_text or "").lower()

    if not children_map:
        base = semantic_dom[base_node_index] if 0 <= base_node_index < len(semantic_dom) else {}
        base_placeholder = _normalize_text(base.get("placeholder")).lower()
        base_name = _normalize_text(base.get("name")).lower()
        dialog_kw = ("弹窗", "对话框", "抽屉", "表单", "新建", "编辑", "创建", "新增", "修改")
        search_kw = ("搜索", "筛选", "过滤", "查询", "列表", "条件")
        prefer_dialog = any(k in intent_lower for k in dialog_kw)
        prefer_search = any(k in intent_lower for k in search_kw) and not prefer_dialog
        candidates: List[int] = []
        for idx, node in enumerate(semantic_dom):
            tag = (node.get("tag") or "").lower()
            if tag not in ("input", "textarea"):
                continue
            if tag == "input":
                if bool(node.get("readOnly")):
                    continue
                t = (node.get("type") or "text").lower()
                if t in ("checkbox", "radio"):
                    continue
            ph = _normalize_text(node.get("placeholder")).lower()
            nm = _normalize_text(node.get("name")).lower()
            if base_placeholder and ph == base_placeholder:
                candidates.append(idx)
            elif base_name and nm == base_name:
                candidates.append(idx)
            elif ph and any(tok for tok in intent_lower.split() if tok and tok in ph):
                candidates.append(idx)
            elif nm and any(tok for tok in intent_lower.split() if tok and tok in nm):
                candidates.append(idx)
        if not candidates:
            return base_node_index

        def _z(v: Any) -> float:
            try:
                return float(v)
            except Exception:
                return 0.0

        best_idx = base_node_index
        best_score = -1e9
        for idx in candidates:
            node = semantic_dom[idx]
            ph = _normalize_text(node.get("placeholder")).lower()
            nm = _normalize_text(node.get("name")).lower()
            in_dialog = bool(node.get("in_dialog"))
            z = _z(node.get("zIndex"))
            score = 0.5 if idx == base_node_index else 0.0
            if base_placeholder and ph == base_placeholder:
                score += 2.0
            if base_name and nm == base_name:
                score += 1.0
            if prefer_dialog:
                score += 1.0 if (in_dialog or z >= 1000) else -0.5
            elif prefer_search:
                score += 1.0 if (not in_dialog and z < 1000) else -0.5
            else:
                score += 0.3 if (not in_dialog and z < 1000) else -0.3
            score += min(max(z, 0.0), 9999.0) / 20000.0
            for key in (intent_lower, expected_lower):
                if key:
                    if ph and any(tok for tok in key.split() if tok and tok in ph):
                        score += 0.2
                    if nm and any(tok for tok in key.split() if tok and tok in nm):
                        score += 0.1
            if score > best_score:
                best_score = score
                best_idx = idx
        return best_idx

    anchor_nodes: List[int] = [base_node_index] + climb_ancestors(
        semantic_dom, base_node_index, max_depth=2,
    )
    candidates = []
    visited: set[int] = set()
    for anchor in anchor_nodes:
        if anchor < 0 or anchor >= len(semantic_dom):
            continue
        queue: List[tuple[int, int]] = [(anchor, 0)]
        while queue:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            candidates.append(current)
            if depth >= 2:
                continue
            for child in children_map.get(current, []):
                queue.append((child, depth + 1))
    if not candidates:
        return base_node_index
    best_idx = base_node_index
    best_score = -1.0
    for idx in candidates:
        node = semantic_dom[idx]
        tag = (node.get("tag") or "").lower()
        role = (node.get("role") or "").lower()
        placeholder = _normalize_text(node.get("placeholder"))
        name = _normalize_text(node.get("name"))
        class_name = (node.get("class") or "") or ""
        score = 0.5 if idx == base_node_index else 0.0
        if tag in ("input", "textarea"):
            score += 5.0
        if role == "textbox":
            score += 4.0
        if placeholder:
            score += 1.5
        if name:
            score += 1.0
        for key in (intent_lower, expected_lower):
            if key:
                for field in (placeholder.lower(), name.lower()):
                    if field and any(tok for tok in key.split() if tok and tok in field):
                        score += 0.5
                        break
        lc = class_name.lower()
        for kw in ("input", "field", "textbox", "editor"):
            if kw in lc:
                score += 0.5
        if score > best_score:
            best_score = score
            best_idx = idx
    return best_idx


def choose_best_checkbox_target(
    semantic_dom: List[SemanticNode],
    base_node_index: int,
    intent: str = "",
) -> int:
    result = base_node_index
    for idx, node in enumerate(semantic_dom):
        cls = (node.get("class") or "").lower()
        role = (node.get("role") or "").lower()
        if "checkbox" in cls or role == "checkbox":
            if abs(idx - base_node_index) < abs(result - base_node_index) or result == base_node_index:
                result = idx
    return result


def find_switch_in_row(
    semantic_dom: List[SemanticNode],
    intent: str,
) -> Optional[int]:
    intent_lower = (intent or "").lower()
    if not intent_lower:
        return None
    row_key = ""
    m = re.search(r"为\s*([^\s，。\"'、]+)", intent)
    if m:
        row_key = m.group(1).strip()
    if not row_key:
        tokens = [t for t in re.split(r"\s+", intent) if any(ch.isalnum() for ch in t)]
        if tokens:
            row_key = tokens[-1]
    row_key_lower = row_key.lower()
    row_candidate_indices: List[int] = []
    for idx, node in enumerate(semantic_dom):
        text = _normalize_text(node.get("text"))
        if text and row_key_lower in text.lower():
            row_candidate_indices.append(idx)
    switch_indices: List[int] = []
    for idx, node in enumerate(semantic_dom):
        class_name = (node.get("class") or "") or ""
        if isinstance(class_name, str) and "switch" in class_name.lower():
            switch_indices.append(idx)
    if not switch_indices:
        return None
    children_map: Dict[int, List[int]] = {}
    for idx, node in enumerate(semantic_dom):
        parent_index = node.get("parent_index")
        if isinstance(parent_index, int) and 0 <= parent_index < len(semantic_dom):
            children_map.setdefault(parent_index, []).append(idx)
    if children_map and row_candidate_indices:
        for row_idx in row_candidate_indices:
            anchors = [row_idx] + climb_ancestors(semantic_dom, row_idx, max_depth=1)
            visited: set[int] = set()
            queue: List[int] = list(anchors)
            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                node = semantic_dom[current]
                class_name = (node.get("class") or "") or ""
                if isinstance(class_name, str) and "switch" in class_name.lower():
                    return current
                for child in children_map.get(current, []):
                    if child not in visited:
                        queue.append(child)
    if row_candidate_indices:
        return min(switch_indices, key=lambda s: min(abs(s - r) for r in row_candidate_indices))
    return switch_indices[0]


def _extract_date_picker_field_from_intent(intent: str) -> str:
    """从 intent 中提取日期字段名."""
    if not intent:
        return ""

    def _clean(raw: str) -> str:
        t = (raw or "").strip().strip('"\'')
        for stop in ("选择", "点击", "填写", "输入", "操作", "进行"):
            if t.endswith(stop):
                t = t[:-len(stop)].strip()
        return t

    # 优先匹配完整短语
    for pattern in (
        r"在\s*[\"']?([^\"'\s，。]{1,20})[\"']?\s*日期范围",
        r"在\s*[\"']?([^\"'\s，。]{1,20})[\"']?\s*日期选择",
        r"在\s*[\"']?([^\"'\s，。]{1,20})[\"']?\s*日期框",
        r"(?:点击|选择|填写|输入)\s*[\"']?([^\"'\s，。]{1,20})[\"']?\s*日期选择",
        r"(?:点击|选择|填写|输入)\s*[\"']?([^\"'\s，。]{1,20})[\"']?\s*日期范围",
        r"在\s*[\"']?([^\"'\s，。]{1,20})[\"']?\s*时间选择",
        r"(?:点击|选择|填写|输入)\s*[\"']?([^\"'\s，。]{1,20})[\"']?\s*日期",
        r"(?:点击|选择|填写|输入)\s*[\"']?([^\"'\s，。]{1,20})[\"']?\s*时间",
        r"日期范围[:：]\s*[\"']?([^\"'\s，。]{1,20})[\"']?",
        r"日期[:：]\s*[\"']?([^\"'\s，。]{1,20})[\"']?",
        r"时间[:：]\s*[\"']?([^\"'\s，。]{1,20})[\"']?",
    ):
        m = re.search(pattern, intent)
        if m:
            lab = _clean(m.group(1))
            if 1 <= len(lab) <= 20:
                return lab
    return ""


def build_date_picker_selector(
    semantic_dom: List[SemanticNode],
    intent: str,
) -> Dict[str, Any]:
    """基于 intent + 语义 DOM 构建 Ant Design 日期选择器 selector 候选。

    覆盖两种场景:
    1. 点击触发器展开面板: ant-picker / ant-picker-range
    2. 在面板中选择日期: td.ant-picker-cell[title="YYYY-MM-DD"]
    """
    field = _extract_date_picker_field_from_intent(intent or "")

    is_ant = _detect_ant_design_from_dom(semantic_dom)

    # 检测是否已展开日期面板
    has_date_panel = any(
        cls in (
            "ant-picker-dropdown", "ant-picker-body", "ant-picker-content",
            "ant-picker-panel", "ant-picker-cell", "ant-picker-cell-inner",
        )
        for node in semantic_dom
        for cls in str(node.get("class") or "").lower().split()
    )

    candidates: List[str] = []

    if has_date_panel:
        # 场景2: 面板已展开，选择具体日期
        date_val = _extract_date_value_from_intent(intent)
        te = _escape_xpath_literal(date_val) if date_val else None

        if date_val and te:
            # td[title="YYYY-MM-DD"].ant-picker-cell-in-view — 最精确，仅匹配当前可见月的单元格
            candidates.extend([
                f"(//td[contains(@title, {te}) and contains(@class,'ant-picker-cell-in-view')])[1]",
                f"(//td[contains(@title, {te}) and contains(@class,'ant-picker-cell')])[1]",
                # 兜底: 通过 ant-picker-dropdown 限定范围（面板 teleported 到 body，外层可能被截断）
                f"(//div[contains(@class,'ant-picker-dropdown')]//td[contains(@title, {te}) and contains(@class,'ant-picker-cell')])[1]",
            ])

        # 无具体日期时的兜底：优先 in-view 非禁用单元格
        candidates.extend([
            "(//td[contains(@class,'ant-picker-cell-in-view') and not(contains(@class,'ant-picker-cell-disabled'))])[1]",
            "(//div[contains(@class,'ant-picker-dropdown')]//td[contains(@class,'ant-picker-cell-in-view')])[1]",
            "(//div[contains(@class,'ant-picker-cell-inner')])[1]",
        ])
    else:
        # 场景1: 点击触发器展开面板
        if not field:
            return {"selector": None, "candidates": [], "field_label": ""}

        te = _escape_xpath_literal(field.strip())

        if is_ant:
            candidates.extend([
                f"(//div[contains(@class,'ant-form-item')][.//label[contains(normalize-space(.), {te})]]//div[contains(@class,'ant-picker'))[1]",
                f"(//div[contains(@class,'ant-form-item')][.//label[contains(normalize-space(.), {te})]]//input[contains(@class,'ant-picker-input'))[1]",
                f"(//label[contains(normalize-space(.), {te})]/ancestor::div[contains(@class,'ant-form-item')]//div[contains(@class,'ant-picker'))[1]",
            ])
            candidates.extend([
                f"(//div[contains(@class,'ant-picker')]//input[contains(@placeholder, {te})])[1]",
            ])
            candidates.extend([
                "(//div[contains(@class,'ant-picker'))[1]",
                "(//div[contains(@class,'ant-picker-range'))[1]",
            ])
        else:
            candidates.extend([
                f"(//input[contains(@placeholder, {te}) and ancestor::div[contains(@class,'picker') or contains(@class,'date'))])[1]",
                f"(//label[contains(normalize-space(.), {te})]/following-sibling::*//input[contains(@class,'date') or contains(@class,'picker'))])[1]",
                "(//input[@type='date'])[1]",
            ])

    deduped: List[str] = []
    for s in candidates:
        if s and s not in deduped:
            deduped.append(s)

    return {
        "selector": deduped[0] if deduped else None,
        "candidates": deduped,
        "field_label": field or "",
    }


def _extract_date_value_from_intent(intent: str) -> Optional[str]:
    """从 intent 中提取具体日期值 (如 2026-06-01)."""
    if not intent:
        return None
    m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", intent)
    if m:
        return m.group(1).replace("/", "-")
    for word in ("今天", "昨天", "明天", "本周一", "本周末", "本月第一天", "本月最后一天"):
        if word in intent:
            return word
    return None


__all__ = [
    "find_nodes_by_text",
    "climb_ancestors",
    "choose_best_click_target",
    "choose_best_input_target",
    "choose_best_checkbox_target",
    "find_switch_in_row",
    "build_dropdown_option_selector",
    "build_checkbox_selector",
    "build_tree_checkbox_selector",
    "build_tree_node_selector",
    "build_el_select_trigger_selector",
    "build_date_picker_selector",
]

"""意图 → 组件类型路由.

供 L3 规则引擎在匹配前判定应走哪类组件选择器, 避免「展开下拉」与「选选项」混用.
"""
from __future__ import annotations

import re
from typing import Optional


def is_dropdown_option(intent: str) -> bool:
    if any(w in intent for w in ("下拉选项", "弹出选项", "选项中")):
        return True
    if re.search(r"(在下拉|弹出).{0,30}(选择|点击)", intent):
        return True
    if re.search(r"(选择|点击)\s*[「\"'\u300c]?[^」\"'\u300d]+[」\"'\u300d]?\s*选项", intent):
        return True
    if re.search(r"选项\s*[「\"']?[^」\"']+[」\"']?\s*$", intent) and "下拉" in intent:
        return True
    return False


def is_select_trigger(intent: str) -> bool:
    if is_dropdown_option(intent):
        return False
    if any(w in intent for w in ("下拉框", "下拉菜单", "下拉栏", "筛选器")):
        return True
    if "下拉" in intent and any(w in intent for w in ("展开", "点击", "选择", "筛选")):
        return True
    if re.search(r"(点击|展开)\s*.+下拉", intent):
        return True
    return False


def is_tree_checkbox(intent: str) -> bool:
    return any(a in intent for a in ("勾选", "复选框", "checkbox")) and any(
        b in intent for b in ("树", "tree", "treeitem", "节点")
    )


def is_checkbox(intent: str) -> bool:
    if is_tree_checkbox(intent):
        return False
    return any(w in intent for w in ("勾选", "取消勾选", "复选框", "checkbox", "选中", "取消选中"))


def is_tree_node(intent: str) -> bool:
    return any(w in intent for w in ("展开", "收起", "折叠")) and any(
        w in intent for w in ("树节点", "节点", "tree")
    )


def is_date_picker(intent: str) -> bool:
    if any(w in intent for w in ("日期选择", "日期范围", "日期控件", "时间选择", "日期框")):
        return True
    return any(w in intent for w in ("日期", "时间")) and any(
        w in intent for w in ("选择", "点击", "填写", "输入")
    )


def is_switch_in_row(intent: str) -> bool:
    return "状态开关" in intent or ("开关" in intent and "行" in intent)


def is_text_input_fill(intent: str, action_type: Optional[str] = None) -> bool:
    """fill 到文本输入框 (搜索/筛选/表单), 非下拉/日期/单选."""
    if (action_type or "").strip().lower() != "fill":
        return False
    if is_dropdown_option(intent) or is_select_trigger(intent) or is_date_picker(intent):
        return False
    if any(w in intent for w in ("搜索框", "搜索", "筛选区", "筛选", "输入框", "文本框")):
        return True
    if re.search(r"(?:输入|填写|填入)\s*.+(?:ID|id|编号|号码)", intent):
        return True
    return False


def is_ant_radio_option(intent: str) -> bool:
    if is_dropdown_option(intent) or is_select_trigger(intent):
        return False
    if any(w in intent for w in ("单选", "radio", "ant-radio")):
        return True
    if "选择" in intent and re.search(r"选择\s*[「\"'\u300c]", intent):
        return True
    if "选项" in intent and any(w in intent for w in ("选择", "点击")):
        return True
    return False

"""步骤⑨ 第3级 动态选择器引擎 (L3).

意图分类路由 → 组件选择器基于实际 DOM 动态生成 → 页面验证 → 返回.
不再使用硬编码规则, 选择器由脚本根据页面实际 DOM 结构即时构建.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .intent_route import (
    is_ant_radio_option,
    is_checkbox,
    is_date_picker,
    is_dropdown_option,
    is_select_trigger,
    is_switch_in_row,
    is_tree_checkbox,
    is_tree_node,
)
from .playwright_api import try_candidates
from .skill_invoke import invoke_skill


_ORDINAL_PREFIX = re.compile(r"^(?:第[一二三四五六七八九十百\d]+个?|首个|某个|那个|这个|第一|首)\s*")
_LOCATION_PREFIX = re.compile(
    r"^(?:页面|界面)?(?:顶部|顶端|底部|右上角|左上角|右下角|左下角|右上|左上|右下|左下|"
    r"右侧|左侧|顶端|中间|中部|上方|下方)的?\s*"
)


def _strip_ordinal(s: str) -> str:
    s = _ORDINAL_PREFIX.sub("", s).strip()
    s = _LOCATION_PREFIX.sub("", s).strip()
    return s


def _target(intent: str) -> str:
    """从意图文本中提取目标字段名/文本."""
    m = list(re.finditer(r"[\"'\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f]([^\"'\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f]+)[\"'\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f]", intent))
    if m:
        raw = _strip_ordinal(m[-1].group(1).strip())
    else:
        m2 = re.search(
            r"(?:点击|选择|输入框输入|输入|填写|勾选|悬停|悬浮在|关闭)\s*(.+?)"
            r"(?:下拉框|下拉菜单|下拉|按钮|链接|选项|输入框|框|卡片|图标|区域|$)",
            intent,
        )
        raw = _strip_ordinal(m2.group(1).strip()) if m2 else _strip_ordinal(intent.strip())
    if raw.endswith("筛选器"):
        raw = raw[: -len("筛选器")].strip()
    if raw.endswith("下拉框"):
        raw = raw[: -len("下拉框")].strip()
    return raw


def _esc(s: str) -> str:
    return s.replace('"', '\\"')


def _css(sel: str) -> dict:
    return {"method": "css", "selector": sel, "nth": 0}


# =============================================================================
# 意图路由: intent 关键词 → (组件选择器构建, 兜底候选)
# =============================================================================

def _has(*words: str):
    return lambda intent: any(w in intent for w in words)


def _has_all(group_a: tuple[str, ...], group_b: tuple[str, ...]):
    return lambda intent: any(a in intent for a in group_a) and any(b in intent for b in group_b)


def _build_input_candidates(field: str) -> list[dict]:
    """输入框: 语义 API 优先 + CSS 兜底."""
    return [
        {"method": "placeholder", "name": field, "exact": False, "nth": 0},
        _css(f'input[placeholder*="{_esc(field)}"]'),
        _css(f'[data-label*="{_esc(field)}"]'),
    ]


def _build_button_candidates(field: str) -> list[dict]:
    """按钮: 语义 API 优先."""
    return [
        {"method": "role", "role": "button", "name": field, "exact": False, "nth": 0},
        _css(f'button:has-text("{_esc(field)}")'),
    ]


def _build_text_click_candidates(field: str) -> list[dict]:
    """通用文本点击: 语义 API 优先."""
    return [
        {"method": "role", "role": "button", "name": field, "exact": False, "nth": 0},
        {"method": "role", "role": "link", "name": field, "exact": False, "nth": 0},
        {"method": "text", "name": field, "exact": True, "nth": 0},
        _css(f'button:has-text("{_esc(field)}")'),
        _css(f'a:has-text("{_esc(field)}")'),
    ]


def _build_hover_candidates(field: str) -> list[dict]:
    """悬停: role + CSS 兜底."""
    return [
        {"method": "role", "role": "button", "name": field, "exact": False, "nth": 0},
        _css(f'[role="button"][haspopup="menu"]:has-text("{_esc(field)}")'),
    ]


def _build_close_dialog_candidates() -> list[dict]:
    """关闭弹窗: 通用关闭按钮."""
    return [
        {"method": "role", "role": "button", "name": "Close", "exact": False, "nth": 0},
        _css('.ant-modal-close'),
    ]


@dataclass
class Rule:
    priority: int
    name: str
    pattern: Callable[[str], bool]
    build: Callable[[str, list[dict]], Optional[dict]]


def _route_dropdown_option(intent: str, dom: list[dict]) -> list[dict]:
    """下拉选项: build_dropdown_option_selector."""
    result = invoke_skill("build_dropdown_option_selector", dom, intent)
    return [{"method": "css", "selector": s, "nth": 0} for s in result.get("candidates", [])] if result.get("candidates") else []


def _route_el_select_trigger(intent: str, dom: list[dict]) -> list[dict]:
    """下拉触发器: build_el_select_trigger_selector."""
    result = invoke_skill("build_el_select_trigger_selector", dom, intent)
    return [{"method": "css", "selector": s, "nth": 0} for s in result.get("candidates", [])] if result.get("candidates") else []


def _route_checkbox(intent: str, dom: list[dict]) -> list[dict]:
    """复选框: build_checkbox_selector."""
    result = invoke_skill("build_checkbox_selector", dom, intent)
    return [{"method": "css", "selector": s, "nth": 0} for s in result.get("candidates", [])] if result.get("candidates") else []


def _route_tree_checkbox(intent: str, dom: list[dict]) -> list[dict]:
    """树形复选框: build_tree_checkbox_selector."""
    result = invoke_skill("build_tree_checkbox_selector", dom, intent)
    return [{"method": "css", "selector": s, "nth": 0} for s in result.get("candidates", [])] if result.get("candidates") else []


def _route_tree_node(intent: str, dom: list[dict]) -> list[dict]:
    """树节点: build_tree_node_selector."""
    result = invoke_skill("build_tree_node_selector", dom, intent)
    return [{"method": "css", "selector": s, "nth": 0} for s in result.get("candidates", [])] if result.get("candidates") else []


def _route_date_picker(intent: str, dom: list[dict]) -> list[dict]:
    """日期选择器: build_date_picker_selector."""
    result = invoke_skill("build_date_picker_selector", dom, intent)
    return [{"method": "css", "selector": s, "nth": 0} for s in result.get("candidates", [])] if result.get("candidates") else []


def _route_ant_radio_option(intent: str, dom: list[dict]) -> list[dict]:
    """Ant Design 单选: 优先点 radio-wrapper 而非内层 span."""
    text = _target(intent)
    if not text:
        return []
    esc = _esc(text)
    return [
        _css(f'label.ant-radio-wrapper:has-text("{esc}")'),
        _css(f'.ant-radio-wrapper:has-text("{esc}")'),
        {"method": "role", "role": "radio", "name": text, "exact": False, "nth": 0},
        _css(f'.ant-radio-group label:has-text("{esc}")'),
    ]


def _route_switch_in_row(intent: str, dom: list[dict]) -> list[dict]:
    """表格行内 switch: find_switch_in_row → 转 locator."""
    from ..dom.semantic_dom import build_locator_info

    idx = invoke_skill("find_switch_in_row", dom, intent)
    if idx is None or idx < 0 or idx >= len(dom):
        return []
    return [build_locator_info(dom[idx])]


_RULES: list[Rule] = [
    # 组件意图路由 (选项优先于触发器)
    Rule(0, "下拉选项", is_dropdown_option,
         lambda i, d: _route_dropdown_option(i, d)),
    Rule(5, "下拉触发器", is_select_trigger,
         lambda i, d: _route_el_select_trigger(i, d)),
    Rule(8, "单选选项", is_ant_radio_option,
         lambda i, d: _route_ant_radio_option(i, d)),
    Rule(15, "行内开关", is_switch_in_row,
         lambda i, d: _route_switch_in_row(i, d)),
    Rule(40, "树形复选框", is_tree_checkbox,
         lambda i, d: _route_tree_checkbox(i, d)),
    Rule(50, "复选框", is_checkbox,
         lambda i, d: _route_checkbox(i, d)),
    Rule(60, "树节点", is_tree_node,
         lambda i, d: _route_tree_node(i, d)),
    Rule(65, "日期选择", is_date_picker,
         lambda i, d: _route_date_picker(i, d)),

    # 轻量兜底规则 (不需要 DOM, 用 Playwright 语义 API)
    Rule(30, "按标签输入", _has("输入框", "填写", "输入"),
         lambda i, d: _build_input_candidates(_target(i))),
    Rule(70, "开关", _has("开关", "switch"),
         lambda i, d: [_css('.el-switch'), _css('.ant-switch')]),
    Rule(75, "关闭弹窗", _has_all(("关闭",), ("弹窗", "对话框", "弹框", "dialog", "窗口")),
         lambda i, d: _build_close_dialog_candidates()),
    Rule(80, "按钮点击", _has_all(("点击",), ("按钮",)),
         lambda i, d: _build_button_candidates(_target(i))),
    Rule(90, "通用文本点击",
         lambda i: not _has("下拉选项", "弹出选项", "选项中")(i) and _has("点击", "选择", "关闭")(i),
         lambda i, d: _build_text_click_candidates(_target(i))),
    Rule(100, "悬停", _has("悬浮", "悬停"),
         lambda i, d: _build_hover_candidates(_target(i))),
]


def _extract_dom_items(page: Any, selectors: Optional[dict] = None) -> list[dict]:
    """从页面提取语义 DOM 节点列表 (供组件选择器使用)."""
    from ..dom.semantic_dom import extract_items
    try:
        fw = None
        if selectors:
            mapped = {
                "container_sel": selectors.get("container_sel"),
                "dropdown_sel": selectors.get("dropdown_sel"),
                "option_sel": selectors.get("option_sel"),
                "dialog_sel": selectors.get("dialog_sel"),
                "form_sel": selectors.get("form_sel"),
            }
            fw = {k: v for k, v in mapped.items() if v} or None
        return extract_items(page, profile="locate", dialog_first=True, stable=False, selectors=fw)
    except Exception:
        return []


class RuleEngine:
    """基于组件选择器的动态选择器引擎, 位于 L3."""

    def __init__(self) -> None:
        self.rules = sorted(_RULES, key=lambda r: r.priority)

    def resolve(
        self,
        page: Any,
        intent: str,
        action_type: str,
        hint: Optional[str] = None,
        exclude: Optional[set[str]] = None,
        framework_selectors: Optional[dict[str, str]] = None,
        semantic_items: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        excl = exclude or set()

        # 优先复用共用 semantic_items; 否则从页面抽取
        dom = semantic_items if semantic_items is not None else _extract_dom_items(page, framework_selectors)

        for rule in self.rules:
            if not rule.pattern(intent):
                continue
            candidates = rule.build(intent, dom)
            if not candidates:
                continue
            hit = try_candidates(page, candidates, excl)
            if hit:
                return hit
        return None

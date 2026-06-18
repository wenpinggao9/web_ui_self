"""步骤⑨ 第3级 意图规则引擎 (L3).

正则匹配意图 → 构建语义/结构化定位候选 → 页面验证 → 返回.
优先 get_by_label / get_by_role / get_by_text, css 作兜底.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .playwright_api import try_candidates


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
    m = list(re.finditer(r"[\"'“”‘’「」『』]([^\"'“”‘’「」『』]+)[\"'“”‘’「」『』]", intent))
    if m:
        raw = _strip_ordinal(m[-1].group(1).strip())
    else:
        m2 = re.search(
            r"(?:点击|选择|输入框输入|输入|填写|勾选|悬停|悬浮在|关闭)\s*(.+?)"
            r"(?:下拉框|下拉菜单|下拉|筛选器|按钮|链接|选项|输入框|框|卡片|图标|区域|$)",
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


def _build_option_candidates(field: str) -> list[dict]:
    e = _esc(field)
    return [
        {"method": "role", "role": "option", "name": field, "exact": False, "nth": 0},
        _css(f'.ant-select-dropdown:visible .ant-select-item-option:has-text("{e}")'),
        _css(f'[role="listbox"] [role="option"]:has-text("{e}")'),
        _css(f'.el-select-dropdown:visible .el-select-dropdown__item:has-text("{e}")'),
        _css(f'li:has-text("{e}")'),
    ]


def _build_filter_trigger_candidates(field: str) -> list[dict]:
    e = _esc(field)
    return [
        {"method": "label", "name": field, "exact": True, "nth": 0},
        {"method": "role", "role": "combobox", "name": field, "exact": True, "nth": 0},
        _css(f'.ant-form-item:has(label:text-is("{e}")) .ant-select'),
        _css(f'.ant-select:has(label:text-is("{e}"))'),
        _css(f'.el-form-item:has-text("{e}") .el-select__wrapper'),
        _css(f'label:has-text("{e}") + .ant-select'),
    ]


def _build_button_candidates(field: str) -> list[dict]:
    return [
        {"method": "role", "role": "button", "name": field, "exact": False, "nth": 0},
        _css(f'button:has-text("{_esc(field)}")'),
        _css(f'.el-button:has-text("{_esc(field)}")'),
    ]


def _build_text_click_candidates(field: str) -> list[dict]:
    return [
        {"method": "role", "role": "button", "name": field, "exact": False, "nth": 0},
        {"method": "role", "role": "link", "name": field, "exact": False, "nth": 0},
        {"method": "text", "name": field, "exact": True, "nth": 0},
        _css(f'button:has-text("{_esc(field)}")'),
        _css(f'a:has-text("{_esc(field)}")'),
        _css(f'text="{_esc(field)}"'),
    ]


@dataclass
class Rule:
    priority: int
    name: str
    pattern: Callable[[str], bool]
    build: Callable[[str], list[dict]]


def _has(*words: str):
    return lambda intent: any(w in intent for w in words)


def _has_all(group_a: tuple[str, ...], group_b: tuple[str, ...]):
    return lambda intent: any(a in intent for a in group_a) and any(b in intent for b in group_b)


_REGION_INDEX = [
    (("省份", "省"), 1),
    (("城市", "市"), 2),
    (("区县", "区"), 3),
]


def _region_index(intent: str) -> int | None:
    for words, idx in _REGION_INDEX:
        if any(w in intent for w in words):
            return idx
    return None


def _is_menu_hover_intent(intent: str) -> bool:
    return _has("悬浮", "悬停")(intent) and _has(
        "用户", "用户名", "菜单", "下拉", "账户", "账号", "头像",
    )(intent)


def _hint_candidates(hint: Optional[str], action_type: str) -> list[dict]:
    if not hint or action_type != "hover":
        return []
    low = hint.lower()
    out: list[dict] = []
    if "haspopup" in low or "menu" in low or "下拉" in hint:
        out.extend([
            _css('[role="button"][haspopup="menu"]'),
            _css('[role="button"][aria-haspopup="menu"]'),
            _css('.el-dropdown [role="button"]'),
        ])
    elif "role=button" in low or 'role="button"' in low:
        out.append(_css('[role="button"]'))
    return out


_MENU_HOVER_SELECTORS = [
    _css('[role="button"][haspopup="menu"]'),
    _css('[role="button"][aria-haspopup="menu"]'),
    _css('.el-dropdown [role="button"]'),
    _css('[class*="dropdown"] [role="button"]'),
]

_RULES: list[Rule] = [
    Rule(0, "下拉选项", _has("下拉选项", "弹出选项", "选项中"),
         lambda i: _build_option_candidates(_target(i))),
    Rule(5, "地区级联触发器", lambda i: _has("下拉框", "下拉菜单", "下拉")(i) and _region_index(i) is not None,
         lambda i: [
             _css(f':nth-match(.el-select__wrapper, {_region_index(i)})'),
             _css(f':nth-match(.el-select .el-input__inner, {_region_index(i)})'),
         ]),
    Rule(8, "用户菜单悬停", _is_menu_hover_intent, lambda i: list(_MENU_HOVER_SELECTORS)),
    Rule(10, "下拉触发器", _has("下拉框", "下拉菜单", "筛选器"),
         lambda i: _build_filter_trigger_candidates(_target(i))),
    Rule(20, "可筛选下拉", _has("筛选下拉", "下拉框输入"),
         lambda i: [_css('.el-select__input'), _css('[role="combobox"]')]),
    Rule(30, "按标签输入", _has("输入框", "填写", "输入"),
         lambda i: [
             {"method": "placeholder", "name": _target(i), "exact": False, "nth": 0},
             _css(f'.el-form-item:has-text("{_esc(_target(i))}") input'),
             _css(f'input[placeholder*="{_esc(_target(i))}"]'),
         ]),
    Rule(40, "树形复选框", _has_all(("勾选", "复选框"), ("树", "节点")),
         lambda i: [
             _css(f'.el-tree-node:has-text("{_esc(_target(i))}") .el-checkbox'),
             _css(f'[role="treeitem"]:has-text("{_esc(_target(i))}") [role="checkbox"]'),
         ]),
    Rule(50, "复选框", _has("勾选", "复选框"),
         lambda i: [
             _css(f'[role="checkbox"]:near(:text("{_esc(_target(i))}"))'),
             _css(f'label:has-text("{_esc(_target(i))}") input[type="checkbox"]'),
         ]),
    Rule(60, "树节点", _has_all(("展开", "收起"), ("树节点", "节点")),
         lambda i: [_css(f'.el-tree-node:has-text("{_esc(_target(i))}") .el-tree-node__expand-icon')]),
    Rule(70, "开关", _has("开关", "switch"),
         lambda i: [_css('.el-switch'), _css('.ant-switch')]),
    Rule(75, "关闭弹窗", _has_all(("关闭",), ("弹窗", "对话框", "弹框", "dialog", "窗口")),
         lambda i: [
             {"method": "role", "role": "button", "name": "Close", "exact": False, "nth": 0},
             _css('.el-dialog__headerbtn'), _css('.ant-modal-close'),
         ]),
    Rule(80, "按钮点击", _has_all(("点击",), ("按钮",)),
         lambda i: _build_button_candidates(_target(i))),
    Rule(90, "通用文本点击",
         lambda i: not _has("下拉选项", "弹出选项", "选项中")(i) and _has("点击", "选择", "关闭")(i),
         lambda i: _build_text_click_candidates(_target(i))),
    Rule(100, "悬停", _has("悬浮", "悬停"),
         lambda i: [
             {"method": "role", "role": "button", "name": _target(i), "exact": False, "nth": 0},
             _css(f'[role="button"][haspopup="menu"]:has-text("{_esc(_target(i))}")'),
         ]),
]


class RuleEngine:
    """基于固定规则的定位候选生成器, 位于 L3."""

    def __init__(self) -> None:
        self.rules = sorted(_RULES, key=lambda r: r.priority)

    def resolve(
        self,
        page: Any,
        intent: str,
        action_type: str,
        hint: Optional[str] = None,
        exclude: Optional[set[str]] = None,
    ) -> Optional[dict]:
        excl = exclude or set()
        hit = try_candidates(page, _hint_candidates(hint, action_type), excl)
        if hit:
            return hit

        for rule in self.rules:
            if not rule.pattern(intent):
                continue
            hit = try_candidates(page, rule.build(intent), excl)
            if hit:
                return hit
        return None

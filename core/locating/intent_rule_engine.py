"""常见意图-规则映射引擎

在 DOM 抽取后、LLM 调用前拦截常见意图，用确定性规则直接构建 selector 并验证，
跳过 LLM 调用（每步节省 2-5 秒 + token 成本）。

查找链：
    cache → memory → DOM 抽取 → **规则引擎** → LLM 解析 → selector

实测数据：85.7% 的 intent 可被规则覆盖。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  XPath 工具
# ═══════════════════════════════════════════════════════════════════


def _escape_xpath_string(s: str) -> str:
    """转义 XPath 字符串，处理单引号/双引号"""
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    parts = s.split("'")
    return "concat('{}')".format("',\"'\",'".join(parts))


def _normalize_xpath_selector(selector: str) -> str:
    """规范化 XPath 选择器前缀"""
    s = selector.strip()
    if s.startswith("xpath="):
        s = s[len("xpath="):]
    if s.startswith("(") or s.startswith("//") or s.startswith("./"):
        return s
    return selector


# ═══════════════════════════════════════════════════════════════════
#  提取函数：从 intent 中提取目标文本
# ═══════════════════════════════════════════════════════════════════


def _extract_quoted_text(intent: str, match: Optional[re.Match] = None) -> Optional[str]:
    """提取引号内的文本："" '' 「」"""
    for pattern in [
        r'["\u201c](.+?)["\u201d]',   # "..." 或 "..."
        r"['\u2018](.+?)['\u2019]",   # '...' 或 '...'
        r"[\u300c](.+?)[\u300d]",      # 「...」
    ]:
        m = re.search(pattern, intent)
        if m:
            t = m.group(1).strip()
            if 1 <= len(t) <= 200:
                return t
    return None


def _extract_click_target(intent: str, match: Optional[re.Match] = None) -> Optional[str]:
    """从「点击XXX」类 intent 提取点击目标文本"""
    for pattern in [
        r"点击\s*[\"']?(.+?)[\"']?\s*(?:按钮|button|选项|菜单|链接|标签|图标)",
        r"点击\s*[\"']?(.+?)[\"']?\s*$",
        r"选择\s*[\"']?(.+?)[\"']?\s*(?:选项|$)",
    ]:
        m = re.search(pattern, intent)
        if m:
            t = m.group(1).strip().strip("'\"\u300c\u300d")
            if 1 <= len(t) <= 200:
                return t
    return None


def _extract_dropdown_option_text(intent: str, match: Optional[re.Match] = None) -> Optional[str]:
    """从「选择XXX」「点击XXX下拉选项」类 intent 提取选项文案"""

    def _normalize(raw: str) -> Optional[str]:
        t = (raw or "").strip().strip("\"'").strip()
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
        t = t.strip("，。；;、")
        if 1 <= len(t) <= 200:
            return t
        return None

    for pattern in [
        r"在下拉选项中选择\s*[\"'「]?(.+?)[\"'」]?\s*$",
        r"点击.+?下拉(?:栏|框|菜单).*?中的\s*[\"']?(.+?)[\"']?\s*选项",
        r"选择.+?下拉(?:栏|框|菜单).*?中的\s*[\"']?(.+?)[\"']?\s*选项",
        r"点击.+?下拉(?:栏|框|菜单).*?中\s*[\"']?(.+?)[\"']?\s*选项",
        r"选择.+?下拉(?:栏|框|菜单).*?中\s*[\"']?(.+?)[\"']?\s*选项",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉选项",
        r"选择\s*[\"']?(.+?)[\"']?\s*下拉选项",
        r"点击\s*[\"']?(.+?)[\"']?\s*选项$",
        r"选择\s*[\"']?(.+?)[\"']?\s*选项$",
        r"选择\s*[\"']?(.+?)[\"']?\s*$",
    ]:
        m = re.search(pattern, intent)
        if m:
            t = _normalize(m.group(1))
            if t:
                return t
    return None


def _extract_dropdown_trigger_label(intent: str, match: Optional[re.Match] = None) -> Optional[str]:
    """从「点击XXX下拉框」类 intent 提取字段名 XXX"""
    for pattern in [
        r"(?:弹窗|对话框|抽屉|窗口)中的\s*[\"']?(.+?)[\"']?\s*下拉框",
        r"(?:弹窗|对话框|抽屉|窗口)中的\s*[\"']?(.+?)[\"']?\s*下拉菜单",
        r"(?:弹窗|对话框|抽屉|窗口)中的\s*[\"']?(.+?)[\"']?\s*下拉栏",
        r"中的\s*[\"']?(.+?)[\"']?\s*下拉框\s*展开",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉(?:框|栏|菜单)?\s*(?:展开|展开按钮)",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉框",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉菜单",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉栏",
        r"展开\s*[\"']?(.+?)[\"']?\s*下拉框",
        r"展开\s*[\"']?(.+?)[\"']?\s*下拉菜单",
        r"点击\s*[\"']?(.+?)[\"']?\s*下拉(?:栏|框|菜单).*?中",
    ]:
        m = re.search(pattern, intent)
        if m:
            label = (m.group(1) or "").strip().strip("\"'")
            if 1 <= len(label) <= 50:
                return label
    return None


def _extract_fill_label(intent: str, match: Optional[re.Match] = None) -> Optional[str]:
    """从「在XXX输入框中输入/填写YYY」类 intent 提取字段标签 XXX"""
    for pattern in [
        r"在\s*[\"']?(.+?)[\"']?\s*(?:输入框|文本框|文本域|编辑框|字段|输入栏)",
        r"在\s*[\"']?(.+?)[\"']?\s*(?:中|里|内)\s*(?:输入|填写|填入|录入|输入内容)",
        r"[\"'](.+?)[\"']\s*(?:输入框|文本框|文本域|编辑框|字段)",
    ]:
        m = re.search(pattern, intent)
        if m:
            label = m.group(1).strip().strip("\"'")
            if 1 <= len(label) <= 100:
                return label
    return None


def _extract_tree_target_text(intent: str, match: Optional[re.Match] = None) -> Optional[str]:
    """从「勾选XXX树节点」类 intent 提取目标文本"""
    quoted = _extract_quoted_text(intent)
    if quoted:
        return quoted
    for pattern in [
        r"(?:勾选|取消勾选|选中|取消选中)\s*(.+?)(?:\s*树|\s*tree|\s*节点)",
        r"(?:勾选|取消勾选|选中|取消选中)\s*(.+)",
    ]:
        m = re.search(pattern, intent)
        if m:
            t = m.group(1).strip().strip("\"'")
            if 1 <= len(t) <= 200:
                return t
    return None


def _extract_tree_node_click_text(intent: str, match: Optional[re.Match] = None) -> Optional[str]:
    """从「展开/收起XXX树节点」类 intent 提取目标文本"""
    quoted = _extract_quoted_text(intent)
    if quoted:
        return quoted
    for pattern in [
        r"(?:展开|收起|折叠|点击)\s*(.+?)(?:\s*树节点|\s*tree\s*node|\s*目录节点)",
    ]:
        m = re.search(pattern, intent)
        if m:
            t = m.group(1).strip().strip("\"'")
            if 1 <= len(t) <= 200:
                return t
    return None


def _extract_switch_anchor(intent: str, match: Optional[re.Match] = None) -> Optional[str]:
    """从「启用/停用XXX开关」类 intent 提取行锚文本"""
    quoted = _extract_quoted_text(intent)
    if quoted:
        return quoted
    for pattern in [
        r"(?:启用|停用|开启|关闭|切换)\s*(.+?)(?:开关|switch)",
        r"(.+?)的?(?:开关|switch)",
    ]:
        m = re.search(pattern, intent)
        if m:
            t = m.group(1).strip().strip("\"'")
            if 1 <= len(t) <= 200:
                return t
    return None


# ═══════════════════════════════════════════════════════════════════
#  构建函数：从目标文本 + 语义 DOM 构建候选 selector 列表
# ═══════════════════════════════════════════════════════════════════


from .field_scope import combobox_ids_near_label


def _build_dropdown_option_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建下拉选项 selector 候选列表"""
    text_norm = target_text.strip()[:200]
    text_escaped = _escape_xpath_string(text_norm)
    name_escaped = text_norm.replace("\\", "\\\\").replace('"', '\\"')

    # 优先限定在已展开的下拉面板内，避免误点表格等同名文案
    candidates = [
        f'.ant-select-dropdown:visible .ant-select-item-option-content:has-text("{name_escaped}")',
        f'.ant-select-dropdown:visible >> role=option >> text="{name_escaped}"',
        f'role=listbox >> role=option >> text="{name_escaped}"',
        f'role=listbox >> role=menuitem >> text="{name_escaped}"',
        f"(//*[contains(@class,'ant-select-dropdown') and not(contains(@style,'display: none'))]"
        f"//*[contains(@class,'ant-select-item-option-content') and contains(normalize-space(.), {text_escaped})])[1]",
        f"(//*[@role='listbox']//*[(@role='option' or @role='menuitem') and contains(normalize-space(.), {text_escaped})])[1]",
        f'role=option[name="{name_escaped}"]',
        f'role=menuitem[name="{name_escaped}"]',
        f'role=menuitemcheckbox[name="{name_escaped}"]',
        f'role=treeitem[name="{name_escaped}"]',
        f"(//option[contains(normalize-space(.), {text_escaped})])[1]",
        f"(//*[contains(@class,'dropdown-item') and contains(normalize-space(.), {text_escaped})])[1]",
    ]
    return list(dict.fromkeys(c for c in candidates if c))


def _build_dropdown_trigger_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建下拉触发器 selector 候选列表"""
    label = target_text.strip()
    text_escaped = _escape_xpath_string(label)

    candidates: List[str] = []
    # ① 本字段门禁通过的 #id
    for cid in combobox_ids_near_label(semantic_dom, label):
        candidates.extend([
            f"#{cid}",
            f"#{cid} >> xpath=ancestor::div[contains(@class,'ant-select')][1]",
        ])
    # ② ant-form-item / el-form-item XPath
    candidates.extend([
        f"(//div[contains(@class,'ant-form-item')][.//label[contains(normalize-space(.), {text_escaped})]]//div[contains(@class,'ant-select')])[1]",
        f"(//div[contains(@class,'ant-form-item')][.//label[contains(normalize-space(.), {text_escaped})]]//*[@role='combobox'])[1]",
        f"(//div[contains(@class,'ant-form-item')][.//label[contains(normalize-space(.), {text_escaped})]]//span[contains(@class,'ant-select-arrow')])[1]",
        f"(//div[@role='dialog']//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'el-cascader')]//*[contains(@class,'el-input__wrapper')])[1]",
        f"(//div[contains(@class,'el-dialog')]//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'el-select__wrapper')])[1]",
        f"(//div[@role='dialog']//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'el-select__wrapper')])[1]",
        f"(//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'el-select__wrapper')])[1]",
        f"(//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'el-input__wrapper')])[1]",
    ])
    # ③ placeholder 兜底
    candidates.extend([
        f"(//input[contains(@placeholder, {text_escaped})]/ancestor::*[contains(@class,'el-select')][1]//div[contains(@class,'el-select__wrapper')])[1]",
        f"(//input[contains(@placeholder, {text_escaped})])[1]",
    ])
    return list(dict.fromkeys(c for c in candidates if c))


def _build_filterable_select_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建可筛选下拉输入框 selector 候选列表"""
    label = target_text.strip()
    text_escaped = _escape_xpath_string(label)

    base_inputs = [
        "//div[contains(@class,'el-select__wrapper') and contains(@class,'is-filterable')]//input[contains(@class,'el-select__input') or @role='combobox']",
        "//div[contains(@class,'el-select') and contains(@class,'is-filterable')]//input[contains(@class,'el-select__input') or @role='combobox']",
        "//div[contains(@class,'el-select__wrapper')]//input[contains(@class,'el-select__input') or @role='combobox']",
    ]
    candidates: List[str] = []
    if label:
        for base in base_inputs:
            candidates.extend([
                f"(//div[contains(@class,'el-dialog')]//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]{base})[1]",
                f"(//div[@role='dialog']//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]{base})[1]",
                f"(//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]{base})[1]",
            ])
    else:
        for base in base_inputs:
            candidates.extend([
                f"(//div[contains(@class,'el-dialog')]{base})[1]",
                f"(//div[@role='dialog']{base})[1]",
                f"({base})[1]",
            ])
    return list(dict.fromkeys(c for c in candidates if c))


def _build_fill_by_label_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建输入框 selector 候选列表（按字段标签）"""
    label = target_text.strip()
    text_escaped = _escape_xpath_string(label)

    candidates = [
        # 弹窗内 el-form-item
        f"(//div[contains(@class,'el-dialog')]//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//input[not(@type='hidden')])[1]",
        f"(//div[contains(@class,'el-overlay-dialog')]//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//input[not(@type='hidden')])[1]",
        f"(//div[@role='dialog']//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//input[not(@type='hidden')])[1]",
        # 全局 el-form-item
        f"(//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//input[not(@type='hidden')])[1]",
        # textarea
        f"(//div[contains(@class,'el-dialog')]//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//textarea)[1]",
        f"(//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//textarea)[1]",
        # role=textbox
        f"(//div[contains(@class,'el-dialog')]//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//*[@role='textbox'])[1]",
        f"(//div[contains(@class,'el-form-item')][.//*[contains(normalize-space(.), {text_escaped})]]//*[@role='textbox'])[1]",
        # placeholder 兜底
        f"//input[@placeholder='请输入{label}']",
        f"//input[@placeholder='{label}']",
        f"//textarea[@placeholder='请输入{label}']",
        f"//textarea[@placeholder='{label}']",
        f"role=textbox[name=\"{label}\"]",
    ]
    return list(dict.fromkeys(c for c in candidates if c))


def _build_tree_checkbox_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建树复选框 selector 候选列表"""
    text_escaped = _escape_xpath_string(target_text)
    candidates = [
        f"(//*[@role='treeitem'][.//text()[contains(normalize-space(.), {text_escaped})]]//*[@role='checkbox'])[1]",
        f"(//*[@role='treeitem'][contains(normalize-space(.), {text_escaped})]//*[@role='checkbox'])[1]",
        f"(//*[contains(@class,'el-tree-node')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'el-checkbox__input')])[1]",
        f"(//*[contains(@class,'ant-tree-treenode')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'ant-tree-checkbox')])[1]",
    ]
    return list(dict.fromkeys(c for c in candidates if c))


def _build_checkbox_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建复选框 selector 候选列表"""
    text_escaped = _escape_xpath_string(target_text) if target_text else None
    candidates: List[str] = []
    if text_escaped:
        candidates.extend([
            f"(//*[@role='checkbox' and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[@role='menuitemcheckbox' and contains(normalize-space(.), {text_escaped})])[1]",
            f"(//*[contains(@class,'checkbox') and not(ancestor::*[@role='tree' or contains(@class,'tree')]) and contains(normalize-space(.), {text_escaped})])[1]",
        ])
    candidates.extend([
        "(//*[@role='checkbox'])[1]",
        "(//*[@role='menuitemcheckbox'])[1]",
    ])
    return list(dict.fromkeys(c for c in candidates if c))


def _build_tree_node_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建树节点展开/收起 selector 候选列表"""
    text_escaped = _escape_xpath_string(target_text)
    candidates = [
        f"(//*[contains(@class,'ant-tree-treenode')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'ant-tree-switcher')])[1]",
        f"(//*[contains(@class,'el-tree-node')][.//*[contains(normalize-space(.), {text_escaped})]]//*[contains(@class,'el-tree-node__expand-icon')])[1]",
        f"(//*[@role='treeitem'][.//text()[contains(normalize-space(.), {text_escaped})]])[1]",
    ]
    return list(dict.fromkeys(c for c in candidates if c))


def _build_switch_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建开关 selector 候选列表"""
    text_escaped = _escape_xpath_string(target_text) if target_text else None
    candidates: List[str] = []
    if text_escaped:
        candidates.extend([
            f"(//*[contains(normalize-space(.), {text_escaped})]/following::*[contains(@class,'el-switch') or contains(@class,'ant-switch') or @role='switch'])[1]",
            f"(//*[contains(normalize-space(.), {text_escaped})]/ancestor::*[contains(@class,'el-table__row') or contains(@class,'ant-table-row') or @role='row'][1]//*[contains(@class,'el-switch') or contains(@class,'ant-switch') or @role='switch'])[1]",
        ])
    candidates.extend([
        "(//*[contains(@class,'el-switch') or contains(@class,'ant-switch') or @role='switch'])[1]",
    ])
    return list(dict.fromkeys(c for c in candidates if c))


def _build_button_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建按钮 selector 候选列表"""
    name_escaped = target_text.replace("\\", "\\\\").replace('"', '\\"')
    candidates = [
        f'button:has-text("{name_escaped}")',
        f'div[role="dialog"] >> button:has-text("{name_escaped}")',
        f'div[contains(@class,"el-dialog")] >> button:has-text("{name_escaped}")',
        f'role=button[name="{name_escaped}"]',
    ]
    return list(dict.fromkeys(c for c in candidates if c))


def _build_generic_click_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建通用点击 selector 候选列表"""
    name_escaped = target_text.replace("\\", "\\\\").replace('"', '\\"')
    candidates = [
        f'text="{name_escaped}"',
        f'role=button[name="{name_escaped}"]',
        f'a:has-text("{name_escaped}")',
        f'role=menuitem[name="{name_escaped}"]',
        f'role=link[name="{name_escaped}"]',
    ]
    return list(dict.fromkeys(c for c in candidates if c))


def _build_hover_candidates(
    target_text: str,
    semantic_dom: List[Dict[str, Any]],
    intent: str,
) -> List[str]:
    """构建悬浮 selector 候选列表"""
    name_escaped = target_text.replace("\\", "\\\\").replace('"', '\\"')
    candidates = [
        f'role=button >> text="{name_escaped}"',
        f'text="{name_escaped}"',
    ]
    return list(dict.fromkeys(c for c in candidates if c))


# ═══════════════════════════════════════════════════════════════════
#  规则定义 + 引擎
# ═══════════════════════════════════════════════════════════════════


@dataclass
class IntentRule:
    """单条意图→规则映射"""

    name: str                                    # 规则名称
    action_types: Set[str]                        # 适用动作类型
    intent_pattern: re.Pattern                    # intent 正则匹配
    extract_fn: Callable[[str, Optional[re.Match]], Optional[str]]  # 提取目标文本
    build_fn: Callable[[str, List[Dict[str, Any]], str], List[str]]  # 构建候选 selector
    priority: int = 0                             # 越小越先检查


class IntentRuleEngine:
    """常见意图→规则映射引擎"""

    def __init__(self) -> None:
        self._rules: List[IntentRule] = self._default_rules()
        self._rules.sort(key=lambda r: r.priority)
        self._hit_stats: Dict[str, int] = {}
        self._last_matched_rule: Optional[str] = None
        self._stats_lookups: int = 0
        self._stats_hits: int = 0

    def resolve(
        self,
        page: Any,
        intent: str,
        action_type: str,
        semantic_dom: List[Dict[str, Any]],
    ) -> Optional[str]:
        """
        按优先级遍历规则，匹配→提取→构建→页面验证→返回 selector；
        无匹配或验证失败则返回 None，回退到 LLM。
        """
        self._last_matched_rule = None
        self._stats_lookups += 1
        if not intent or not action_type or not semantic_dom:
            return None

        for rule in self._rules:
            if action_type not in rule.action_types:
                continue
            # 下拉选项/展开类 intent 禁止回退到全页 text= 点击（易误点表格等同名文案）
            if rule.name == "generic_text_click" and re.search(
                r"下拉选项|下拉框.*展开|展开.*下拉",
                intent,
            ):
                continue
            m = rule.intent_pattern.search(intent)
            if not m:
                continue

            # 提取目标文本
            target_text = rule.extract_fn(intent, m)
            if not target_text:
                continue

            # 构建候选 selector
            candidates = rule.build_fn(target_text, semantic_dom, intent)
            if not candidates:
                continue

            # 页面验证
            for sel in candidates:
                if self._validate_selector(page, sel):
                    self._hit_stats[rule.name] = self._hit_stats.get(rule.name, 0) + 1
                    self._stats_hits += 1
                    self._last_matched_rule = rule.name
                    logger.info(
                        "intent_rule_engine HIT | rule=%s | intent=%s | selector=%s",
                        rule.name, str(intent)[:80], sel[:120],
                    )
                    return sel

            logger.debug(
                "intent_rule_engine MATCH but INVALID | rule=%s | intent=%s | candidates=%d",
                rule.name, str(intent)[:80], len(candidates),
            )

        return None

    def last_matched_rule(self) -> Optional[str]:
        return self._last_matched_rule

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    @property
    def hit_stats(self) -> Dict[str, int]:
        return dict(self._hit_stats)

    @property
    def stats(self) -> Dict[str, Any]:
        """规则引擎命中率统计。"""
        lookups = self._stats_lookups or 1
        return {
            "lookups": self._stats_lookups,
            "hits": self._stats_hits,
            "misses": self._stats_lookups - self._stats_hits,
            "hit_rate": round(self._stats_hits / lookups * 100, 1),
            "rule_count": len(self._rules),
            "per_rule_hits": dict(self._hit_stats),
        }

    @staticmethod
    def _validate_selector(page: Any, selector: str) -> bool:
        """验证 selector 匹配到至少 1 个可见元素"""
        if not page or not selector:
            return False
        try:
            normalized = _normalize_xpath_selector(selector)
            count = page.locator(normalized).count()
            if count >= 1:
                return bool(page.locator(normalized).first.is_visible())
            return False
        except Exception:
            return False

    @staticmethod
    def _default_rules() -> List[IntentRule]:
        """默认规则集，按从最具体到最不具体的优先级排列"""
        return [
            # ── 下拉选项（priority 0）──
            IntentRule(
                name="dropdown_option",
                action_types={"click"},
                intent_pattern=re.compile(r"选择|下拉选项|选项.*下拉|下拉.*选项"),
                extract_fn=_extract_dropdown_option_text,
                build_fn=_build_dropdown_option_candidates,
                priority=0,
            ),
            # ── 下拉触发器（priority 10）──
            IntentRule(
                name="dropdown_trigger",
                action_types={"click"},
                intent_pattern=re.compile(r"下拉框|下拉菜单|下拉栏|下拉$"),
                extract_fn=_extract_dropdown_trigger_label,
                build_fn=_build_dropdown_trigger_candidates,
                priority=10,
            ),
            # ── 可筛选下拉输入（priority 20）──
            IntentRule(
                name="filterable_select",
                action_types={"fill"},
                intent_pattern=re.compile(r"筛选下拉|下拉栏填写|下拉框填写|可筛选下拉|下拉框输入|下拉栏输入|筛选框"),
                extract_fn=_extract_fill_label,
                build_fn=_build_filterable_select_candidates,
                priority=20,
            ),
            # ── 按标签填写输入框（priority 30）──
            IntentRule(
                name="fill_by_label",
                action_types={"fill"},
                intent_pattern=re.compile(r"输入框|文本框|填写|输入|填入|录入"),
                extract_fn=_extract_fill_label,
                build_fn=_build_fill_by_label_candidates,
                priority=30,
            ),
            # ── 树复选框（priority 40）──
            IntentRule(
                name="tree_checkbox",
                action_types={"click"},
                intent_pattern=re.compile(r"(勾选|取消勾选|复选框|checkbox|选中|取消选中).*(?:树|tree|treeitem|节点)"),
                extract_fn=_extract_tree_target_text,
                build_fn=_build_tree_checkbox_candidates,
                priority=40,
            ),
            # ── 普通复选框（priority 50）──
            IntentRule(
                name="checkbox",
                action_types={"click"},
                intent_pattern=re.compile(r"勾选|取消勾选|复选框|checkbox|选中|取消选中"),
                extract_fn=lambda i, m: _extract_quoted_text(i, m) or _extract_click_target(i, m),
                build_fn=_build_checkbox_candidates,
                priority=50,
            ),
            # ── 树节点展开/收起（priority 60）──
            IntentRule(
                name="tree_node",
                action_types={"click"},
                intent_pattern=re.compile(r"(?:展开|收起|折叠|点击).*(?:树节点|tree|treeitem|目录|节点)"),
                extract_fn=_extract_tree_node_click_text,
                build_fn=_build_tree_node_candidates,
                priority=60,
            ),
            # ── 开关切换（priority 70）──
            IntentRule(
                name="switch",
                action_types={"click"},
                intent_pattern=re.compile(r"状态开关|开关|switch|启用|停用|禁用"),
                extract_fn=_extract_switch_anchor,
                build_fn=_build_switch_candidates,
                priority=70,
            ),
            # ── 按钮点击（priority 80）──
            IntentRule(
                name="button_click",
                action_types={"click"},
                intent_pattern=re.compile(r"点击.*(?:按钮|button)|button"),
                extract_fn=lambda i, m: _extract_quoted_text(i, m) or _extract_click_target(i, m),
                build_fn=_build_button_candidates,
                priority=80,
            ),
            # ── 通用文本点击（priority 90）──
            IntentRule(
                name="generic_text_click",
                action_types={"click"},
                intent_pattern=re.compile(r"点击|选择"),
                extract_fn=lambda i, m: _extract_quoted_text(i, m) or _extract_click_target(i, m),
                build_fn=_build_generic_click_candidates,
                priority=90,
            ),
            # ── 悬浮（priority 100）──
            IntentRule(
                name="hover",
                action_types={"hover"},
                intent_pattern=re.compile(r"鼠标悬浮|hover|悬停"),
                extract_fn=lambda i, m: _extract_quoted_text(i, m) or _extract_click_target(i, m),
                build_fn=_build_hover_candidates,
                priority=100,
            ),
        ]
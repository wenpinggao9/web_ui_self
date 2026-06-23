"""页面结构学习能力（PageStructureLearner）

从成功解析中学习页面结构知识，在同页面二次执行和相似页面中复用。

核心数据流：
1. 学习：每次成功解析 selector 后，将 (route, action_type, component_type, selector_template) 记录到 PageStructure
2. 查找：规则引擎未命中后、LLM 之前，查结构学习记录尝试复用
3. 相似页面：同组件库 + DOM 结构指纹相似的页面可部分复用规则

查找链：
    cache → memory → DOM 抽取 → 规则引擎 → **结构学习查找** → LLM → selector
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 结构学习存储目录
_STRUCTURE_DIR_NAME = "page_structure_learner"

# DOM 签名相似度阈值：Jaccard > 0.6 视为相似
_SIMILARITY_THRESHOLD = 0.6


# ── 数据结构 ──────────────────────────────────────────────


@dataclass
class LearnedRule:
    """从成功解析中学习到的单条规则"""

    action_type: str  # click / fill / hover / assert_text
    component_type: str  # dropdown_trigger / dropdown_option / input / button / menu_item / generic
    selector_template: str  # 参数化模板：//input[@placeholder="请输入{label}"]
    selector_type: str  # css / xpath / role / rule
    anchor_type: str  # label / text / role / class / placeholder
    success_count: int = 1
    failure_count: int = 0


@dataclass
class PageStructure:
    """单页面结构学习记录"""

    route: str  # 归一化路由：/settings-page
    component_library: str  # element-ui / ant-design / generic
    dom_signature: str  # DOM 结构指纹
    rules: Dict[str, LearnedRule] = field(default_factory=dict)  # key = action_type|component_type
    learned_at: float = 0.0
    updated_at: float = 0.0
    hit_count: int = 0


# ── DOM 结构指纹 ─────────────────────────────────────────


def compute_dom_signature(semantic_dom: List[Dict[str, Any]]) -> str:
    """
    从 DOM 提取结构性特征，生成指纹字符串。
    只取组件级标签，忽略具体文案。

    指纹格式：<role 频率排序>||<class 前缀频率排序> 的 MD5

    示例：
        div_role=listbox:1, button_:3, input_role=textbox:2  ||  el-form-item:4, el-select:3
    """
    if not semantic_dom:
        return ""

    # 1. 统计 (tag, role) 对频率
    tag_role_counter: Counter = Counter()
    for node in semantic_dom:
        tag = str(node.get("tag") or "").lower()
        role = str(node.get("role") or "").lower()
        key = f"{tag}_role={role}" if role else f"{tag}_"
        tag_role_counter[key] += 1

    # 2. 统计组件库 class 前缀频率
    class_prefix_counter: Counter = Counter()
    for node in semantic_dom:
        cls = str(node.get("class") or "").lower()
        if not cls:
            attrs = node.get("attributes") or {}
            cls = str(attrs.get("class") or "").lower()
        if not cls:
            continue
        # 提取类名前缀（如 el-form-item → el-form-item, ant-btn → ant-btn）
        for part in cls.split():
            if not part or len(part) < 2:
                continue
            # 取第一段作为前缀（如 el-form-item → el-form）
            # 但也保留完整类名用于更精确匹配
            # 只取包含连字符的类名前缀（组件库特征）
            if "-" in part:
                # 取第一段连字符前缀：el-form-item → el-form
                segments = part.split("-")
                if len(segments) >= 2:
                    prefix = segments[0] + "-" + segments[1]
                    class_prefix_counter[prefix] += 1

    # 3. 拼接排序后的频率描述 → hash
    role_part = ", ".join(
        f"{k}:{v}" for k, v in sorted(tag_role_counter.items())
    )
    class_part = ", ".join(
        f"{k}:{v}" for k, v in sorted(class_prefix_counter.items())
    )
    raw = f"{role_part}||{class_part}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def extract_class_prefixes_from_signature(semantic_dom: List[Dict[str, Any]]) -> str:
    """从 DOM 中提取 class 前缀部分的排序字符串，用于组件库匹配"""
    class_prefix_counter: Counter = Counter()
    for node in semantic_dom:
        cls = str(node.get("class") or "").lower()
        if not cls:
            attrs = node.get("attributes") or {}
            cls = str(attrs.get("class") or "").lower()
        if not cls:
            continue
        for part in cls.split():
            if not part or len(part) < 2 or "-" not in part:
                continue
            segments = part.split("-")
            if len(segments) >= 2:
                prefix = segments[0] + "-" + segments[1]
                class_prefix_counter[prefix] += 1
    return ", ".join(f"{k}:{v}" for k, v in sorted(class_prefix_counter.items()))


def compute_role_jaccard(sig1_components: Dict, sig2_components: Dict) -> float:
    """计算两个 DOM 签名的 role 频率 Jaccard 相似度"""
    set1 = set(sig1_components.keys())
    set2 = set(sig2_components.keys())
    if not set1 and not set2:
        return 1.0
    if not set1 or not set2:
        return 0.0
    intersection = set1 & set2
    union = set1 | set2
    return len(intersection) / len(union)


def extract_role_components(semantic_dom: List[Dict[str, Any]]) -> Dict[str, int]:
    """提取 (tag, role) 对频率"""
    counter: Counter = Counter()
    for node in semantic_dom:
        tag = str(node.get("tag") or "").lower()
        role = str(node.get("role") or "").lower()
        key = f"{tag}_role={role}" if role else f"{tag}_"
        counter[key] += 1
    return dict(counter)


# ── selector 模板化 ──────────────────────────────────────


def templatize_selector(selector: str) -> Tuple[str, str, str]:
    """
    从成功 selector 中提取参数化模板、组件类型和锚类型。

    返回 (selector_template, component_type, anchor_type)

    模板化规则：将引号内非结构性文本替换为 {text} 或 {label} 占位符。

    Examples:
        role=option[name="广东省"]     → (role=option[name="{text}"], dropdown_option, text)
        //input[@placeholder="请输入联系人姓名"] → (//input[@placeholder="请输入{label}"], input, placeholder)
        button:has-text("保存设置")     → (button:has-text("{text}"), button, text)
        text="设置页"                 → (text="{text}", generic, text)
    """
    if not selector:
        return ("", "generic", "text")

    template = selector
    component_type = "generic"
    anchor_type = "text"

    # 检测 component_type 和 anchor_type 优先级：
    # 1. dialog 相关 → dialog_button
    # 2. dropdown 相关 → dropdown_option / dropdown_trigger
    # 3. input + placeholder → input
    # 4. button + text → button
    # 5. 其他 → generic

    lower_sel = selector.lower()

    if "role=dialog" in lower_sel or "role=\"dialog\"" in lower_sel or "[role=\"dialog\"]" in lower_sel:
        component_type = "dialog_button"
        if "has-text" in lower_sel:
            anchor_type = "text"
        elif "placeholder" in lower_sel:
            anchor_type = "placeholder"
    elif "role=option" in lower_sel or "role=\"option\"" in lower_sel:
        component_type = "dropdown_option"
        if "name=" in selector:
            anchor_type = "text"
        elif "has-text" in lower_sel:
            anchor_type = "text"
    elif "role=listbox" in lower_sel or "[role=\"listbox\"]" in lower_sel:
        component_type = "dropdown_trigger"
        anchor_type = "role"
    elif "menuitem" in lower_sel or "menu-item" in lower_sel:
        component_type = "menu_item"
        anchor_type = "text"
    elif re.search(r"input\[.*placeholder", lower_sel) or re.search(r"//input\[.*placeholder", lower_sel):
        component_type = "input"
        anchor_type = "placeholder"
    elif re.search(r"input\[.*name", lower_sel) or re.search(r"//input\[.*@name", lower_sel):
        component_type = "input"
        anchor_type = "label"
    elif "select" in lower_sel and ("option" in lower_sel or "dropdown" in lower_sel):
        component_type = "dropdown_trigger"
        anchor_type = "class"
    elif re.search(r"button", lower_sel) or re.search(r"btn", lower_sel):
        component_type = "button"
        if "has-text" in lower_sel:
            anchor_type = "text"
    elif "input" in lower_sel:
        component_type = "input"
        if "placeholder" in lower_sel:
            anchor_type = "placeholder"
        elif "name=" in selector:
            anchor_type = "label"

    # 模板化：直接替换所有双引号内的非结构性文本
    template = _simple_templatize(selector, anchor_type)

    return (template, component_type, anchor_type)


def _simple_templatize(selector: str, anchor_type: str) -> str:
    """
    简化模板化：将所有双引号内的非结构性文本替换为占位符。
    """
    structural_values = {
        "submit", "button", "input", "select", "option", "checkbox",
        "radio", "text", "password", "email", "number", "tel", "url",
        "search", "hidden", "dialog", "listbox", "menu", "menuitem",
        "tab", "tabpanel", "tree", "grid", "table", "row",
    }

    result = []
    i = 0
    while i < len(selector):
        if selector[i] == '"':
            # 找到引号内的内容
            end = selector.find('"', i + 1)
            if end == -1:
                result.append(selector[i:])
                break
            content = selector[i + 1:end]
            if content.lower() in structural_values:
                result.append(f'"{content}"')
            else:
                placeholder = "{text}"
                if anchor_type == "placeholder":
                    placeholder = "{label}"
                elif anchor_type == "label":
                    placeholder = "{label}"
                result.append(f'"{placeholder}"')
            i = end + 1
        else:
            result.append(selector[i])
            i += 1

    return "".join(result)


# ── PageStructureLearner ─────────────────────────────────


class PageStructureLearner:
    """页面结构学习器：从成功解析中学习页面结构知识"""

    def __init__(self, similarity_threshold: float = _SIMILARITY_THRESHOLD) -> None:
        self._store: Dict[str, PageStructure] = {}  # key = 归一化路由
        self.similarity_threshold = float(similarity_threshold)
        self._stats_lookups: int = 0
        self._stats_exact_hits: int = 0
        self._stats_similar_hits: int = 0

    # ── 学习 ──────────────────────────────────────────────

    def learn(
        self,
        route: str,
        component_library: str,
        semantic_dom: List[Dict[str, Any]],
        action_type: str,
        intent: str,
        selector: str,
        selector_type: str = "css",
    ) -> None:
        """
        从一次成功解析中学习，更新页面结构记录。

        Args:
            route: 归一化路由（由 normalize_page_url 生成）
            component_library: 组件库名称（element-ui / ant-design / generic）
            semantic_dom: 当前页面的语义 DOM
            action_type: 动作类型（click / fill / hover / assert_text）
            intent: 用户意图
            selector: 成功解析的 selector
            selector_type: selector 类型（css / xpath / role / rule）
        """
        if not route or not selector or not action_type:
            return

        # 跳过无意义 selector
        if selector in ("body", "TOAST_SEARCH", "NEGATIVE_ASSERT", "__SKIP_MENU_NAVIGATION__"):
            return

        # 模板化 selector
        template, comp_type, anchor_type = templatize_selector(selector)

        # 计算 DOM 签名
        dom_sig = compute_dom_signature(semantic_dom) if semantic_dom else ""

        # 获取或创建页面结构记录
        structure = self._store.get(route)
        now = time.time()

        if structure is None:
            structure = PageStructure(
                route=route,
                component_library=component_library or "generic",
                dom_signature=dom_sig,
                learned_at=now,
                updated_at=now,
            )
            self._store[route] = structure

        # 更新组件库（如果之前的 unknown，现在有了更准确的值）
        if component_library and component_library != "unknown" and structure.component_library in ("unknown", "generic"):
            structure.component_library = component_library

        # 更新 DOM 签名
        if dom_sig:
            structure.dom_signature = dom_sig

        # 构建 rule key
        rule_key = f"{action_type}|{comp_type}"

        existing_rule = structure.rules.get(rule_key)
        if existing_rule:
            # 相同模板 → success_count +1
            if existing_rule.selector_template == template:
                existing_rule.success_count += 1
            else:
                # 不同模板 → 保留成功率更高的
                existing_rule.success_count += 1
                # 如果新模板更通用（含更多占位符），替换旧模板
                old_placeholders = existing_rule.selector_template.count("{")
                new_placeholders = template.count("{")
                if new_placeholders >= old_placeholders:
                    existing_rule.selector_template = template
                    existing_rule.anchor_type = anchor_type
                    existing_rule.selector_type = selector_type
        else:
            # 新增规则
            structure.rules[rule_key] = LearnedRule(
                action_type=action_type,
                component_type=comp_type,
                selector_template=template,
                selector_type=selector_type,
                anchor_type=anchor_type,
                success_count=1,
                failure_count=0,
            )

        structure.updated_at = now
        logger.debug(
            "structure_learner LEARN | route=%s | rule_key=%s | template=%s | comp_type=%s",
            route, rule_key, template[:80], comp_type,
        )

    # ── 查找 ──────────────────────────────────────────────

    def lookup(
        self,
        route: str,
        component_library: str,
        dom_signature: str,
    ) -> Optional[PageStructure]:
        """查找当前页面的学习记录；精确匹配优先，相似匹配次之"""
        self._stats_lookups += 1
        # 1. 精确匹配
        structure = self._store.get(route)
        if structure:
            structure.hit_count += 1
            self._stats_exact_hits += 1
            return structure

        # 2. 相似匹配
        similar = self.lookup_similar(component_library, dom_signature)
        if similar:
            self._stats_similar_hits += 1
            return similar[0]

        return None

    def lookup_similar(
        self,
        component_library: str,
        dom_signature: str,
    ) -> List[PageStructure]:
        """查找相似页面的学习记录（同组件库 + 结构相似）"""
        if not component_library or component_library in ("unknown", "generic"):
            return []
        if not dom_signature:
            return []

        results = []
        for structure in self._store.values():
            if structure.component_library not in (component_library, "generic"):
                continue
            if not structure.dom_signature:
                continue
            # 简化相似度判断：同组件库 + dom_signature 一致或部分匹配
            # 这里用签名前12位做快速匹配（4.5 bytes → ~72 bits，远低于碰撞概率）
            if structure.dom_signature[:12] == dom_signature[:12]:
                results.append(structure)

        # 限制返回数量
        return results[:3]

    # ── 解析 ──────────────────────────────────────────────

    def resolve_from_learned(
        self,
        route: str,
        intent: str,
        action_type: str,
        semantic_dom: List[Dict[str, Any]],
    ) -> Optional[str]:
        """
        用学习到的规则尝试解析 selector。

        匹配策略：
        1. 精确匹配当前路由 → 尝试所有规则模板实例化
        2. 相似页面匹配 → 尝试相似页面的规则模板实例化
        """
        if not route or not intent or not action_type:
            return None

        dom_sig = compute_dom_signature(semantic_dom) if semantic_dom else ""

        # 检测当前页面的组件库
        comp_lib = self._detect_component_library(semantic_dom)

        # 1. 精确匹配
        structure = self._store.get(route)
        if structure:
            selector = self._try_instantiate_rules(
                structure, intent, action_type, semantic_dom
            )
            if selector:
                structure.hit_count += 1
                logger.info(
                    "structure_learner HIT (exact) | route=%s | intent=%s | selector=%s",
                    route, str(intent)[:80], selector[:120],
                )
                return selector

        # 2. 相似页面匹配
        similar_structures = self.lookup_similar(comp_lib, dom_sig)
        for sim_structure in similar_structures:
            selector = self._try_instantiate_rules(
                sim_structure, intent, action_type, semantic_dom
            )
            if selector:
                sim_structure.hit_count += 1
                logger.info(
                    "structure_learner HIT (similar) | route=%s | source_route=%s | intent=%s | selector=%s",
                    route, sim_structure.route, str(intent)[:80], selector[:120],
                )
                return selector

        return None

    def _try_instantiate_rules(
        self,
        structure: PageStructure,
        intent: str,
        action_type: str,
        semantic_dom: List[Dict[str, Any]],
    ) -> Optional[str]:
        """尝试用学习到的规则模板实例化 selector"""
        # 从 intent 中提取可能的目标文本
        target_texts = self._extract_target_texts(intent)
        target_labels = self._extract_target_labels(intent)

        for rule_key, rule in structure.rules.items():
            # 过滤 action_type 不匹配的规则
            if rule.action_type != action_type:
                continue

            # 跳过失败率过高的规则
            if rule.failure_count > rule.success_count * 2:
                continue

            # 尝试用模板生成 selector
            selector = self._instantiate_template(
                rule, target_texts, target_labels, semantic_dom
            )
            if selector:
                return selector

        return None

    def _instantiate_template(
        self,
        rule: LearnedRule,
        target_texts: List[str],
        target_labels: List[str],
        semantic_dom: List[Dict[str, Any]],
    ) -> Optional[str]:
        """将模板实例化为具体 selector"""
        template = rule.selector_template
        if not template:
            return None

        # 根据 anchor_type 选择替换源
        if rule.anchor_type in ("placeholder", "label"):
            # 用 label 替换 {label}，用 text 替换 {text}
            for label in target_labels:
                candidate = template.replace("{label}", label).replace("{text}", label)
                if self._validate_candidate(candidate, semantic_dom):
                    return candidate
                # 精确匹配未命中，若 XPath 属性匹配则 fallback 到 contains() 子串匹配
                contains_candidate = self._try_contains_fallback(template, label)
                if contains_candidate and self._validate_candidate(contains_candidate, semantic_dom):
                    logger.debug(
                        "structure_learner: 精确匹配失败，使用 contains() fallback | label=%s | candidate=%s",
                        label[:60], contains_candidate[:120],
                    )
                    return contains_candidate
            # label 未命中，尝试用 text 替换
            for text in target_texts:
                candidate = template.replace("{label}", text).replace("{text}", text)
                if self._validate_candidate(candidate, semantic_dom):
                    return candidate
                contains_candidate = self._try_contains_fallback(template, text)
                if contains_candidate and self._validate_candidate(contains_candidate, semantic_dom):
                    return contains_candidate
        else:
            # 用 text 替换 {text}，用 label 替换 {label}
            for text in target_texts:
                candidate = template.replace("{text}", text).replace("{label}", text)
                if self._validate_candidate(candidate, semantic_dom):
                    return candidate
                contains_candidate = self._try_contains_fallback(template, text)
                if contains_candidate and self._validate_candidate(contains_candidate, semantic_dom):
                    return contains_candidate
            # text 未命中，尝试用 label 替换
            for label in target_labels:
                candidate = template.replace("{text}", label).replace("{label}", label)
                if self._validate_candidate(candidate, semantic_dom):
                    return candidate
                contains_candidate = self._try_contains_fallback(template, label)
                if contains_candidate and self._validate_candidate(contains_candidate, semantic_dom):
                    return contains_candidate

        return None

    @staticmethod
    def _try_contains_fallback(template: str, value: str) -> Optional[str]:
        """将 XPath 精确属性匹配 `[@attr="val"]` 转换为 contains() 子串匹配。

        例如：//input[@placeholder="{label}"] → //input[contains(@placeholder, "联系人")]
        仅当模板是 XPath 且包含属性精确匹配时才转换。
        """
        # 只处理 XPath 属性精确匹配
        attr_exact_pattern = r'//(\w+)\[@(\w+)=["\']\{(\w+)\}["\']\]'
        match = re.search(attr_exact_pattern, template)
        if not match:
            return None
        tag = match.group(1)
        attr = match.group(2)
        placeholder = match.group(3)
        if placeholder not in ("label", "text"):
            return None
        escaped_value = value.replace('"', '&quot;').replace("'", "&apos;")
        return f"//{tag}[contains(@{attr}, '{escaped_value}')]"

    @staticmethod
    def _validate_candidate(selector: str, semantic_dom: List[Dict[str, Any]]) -> bool:
        """
        轻量验证：不调用 Playwright，而是从 semantic_dom 中检查是否存在匹配元素。
        对于 text="xxx"、has-text("xxx")，做文本匹配验证。
        对于 XPath（//input[@placeholder="xxx"]），做属性匹配验证。
        对于复杂 selector，保守返回 True（交给后续 Playwright 验证）。
        """
        if not selector:
            return False

        # text="xxx" 类型：检查 DOM 中是否有包含该文本的节点
        text_match = re.search(r'text="([^"]+)"', selector)
        if text_match and "has-text" not in selector:
            target_text = text_match.group(1)
            for node in semantic_dom:
                node_text = str(node.get("text") or "")
                if target_text in node_text:
                    return True
            return False

        # has-text("xxx") 类型：检查 DOM 中是否有包含该文本的节点
        has_text_match = re.search(r'has-text\("([^"]+)"\)', selector)
        if has_text_match and "role=" not in selector and "://" not in selector:
            target_text = has_text_match.group(1)
            tag_match = re.match(r"(\w+)", selector)
            target_tag = tag_match.group(1).lower() if tag_match else ""
            for node in semantic_dom:
                node_text = str(node.get("text") or "")
                if target_text not in node_text:
                    continue
                if target_tag and str(node.get("tag") or "").lower() != target_tag:
                    continue
                return True
            return False

        # XPath 类型：检查 DOM 中是否有匹配属性值的节点
        # 精确匹配：//input[@placeholder="联系人"]
        xpath_attr_match = re.search(r'//(\w+)\[@(\w+)=["\']([^"\']+)["\']\]', selector)
        if xpath_attr_match:
            target_tag = xpath_attr_match.group(1).lower()
            target_attr = xpath_attr_match.group(2)
            target_value = xpath_attr_match.group(3)
            for node in semantic_dom:
                if str(node.get("tag") or "").lower() != target_tag:
                    continue
                # 直接属性 — XPath 精确匹配要求完全相等
                node_val = str(node.get(target_attr) or "")
                if node_val == target_value:
                    return True
                # attributes 子字典
                attrs = node.get("attributes") or {}
                if isinstance(attrs, dict):
                    node_val = str(attrs.get(target_attr) or "")
                    if node_val == target_value:
                        return True
            return False

        # contains() 子串匹配：//input[contains(@placeholder, '联系人')]
        xpath_contains_match = re.search(
            r'//(\w+)\[contains\(@(\w+),\s*[\'"]([^\'"]+)[\'"]\)\]', selector
        )
        if xpath_contains_match:
            target_tag = xpath_contains_match.group(1).lower()
            target_attr = xpath_contains_match.group(2)
            target_value = xpath_contains_match.group(3)
            for node in semantic_dom:
                if str(node.get("tag") or "").lower() != target_tag:
                    continue
                node_val = str(node.get(target_attr) or "")
                if target_value in node_val:
                    return True
                attrs = node.get("attributes") or {}
                if isinstance(attrs, dict):
                    node_val = str(attrs.get(target_attr) or "")
                    if target_value in node_val:
                        return True
            return False

        # role=xxx[name="yyy"] 类型
        role_match = re.search(r'role=(\w+)\[name="([^"]+)"\]', selector)
        if role_match:
            target_role = role_match.group(1).lower()
            target_name = role_match.group(2)
            for node in semantic_dom:
                if str(node.get("role") or "").lower() != target_role:
                    continue
                node_text = str(node.get("text") or "")
                if target_name in node_text:
                    return True
            return False

        # 复杂 selector：保守通过，交给后续验证
        return True

    # ── 工具方法 ──────────────────────────────────────────

    @staticmethod
    def _extract_target_texts(intent: str) -> List[str]:
        """从 intent 中提取目标文本（引号内的内容）"""
        texts = []
        # 匹配中英文引号内的内容
        for m in re.finditer(r'["\u201c\u201d\u2018\u2019\']([^"\u201c\u201d\u2018\u2019\']+)["\u201c\u201d\u2018\u2019\']', intent):
            texts.append(m.group(1))
        return texts

    @staticmethod
    def _extract_target_labels(intent: str) -> List[str]:
        """从 intent 中提取目标标签（如"输入联系人姓名"中的"联系人姓名"）

        匹配策略：
        1. 引号内的内容优先（'收件人' → "收件人"）
        2. 输入/填写/选择 后面直到下一个标点/动词的内容
        """
        labels = []

        # 1. 先提取引号内内容（优先级最高）
        quoted = re.findall(r"[「'\"」']([^'\"「」]+)['\"'」]", intent)
        if quoted:
            labels.extend(quoted)

        # 2. 匹配"输入/填写/选择 + 标签名"模式
        # 需排除引号内内容被二次匹配
        for m in re.finditer(
            r"(?:输入|填写|选择|输入框|文本框)\s*"
            r"([^\s,，。、；：！？'\"「」\u2018\u2019\u201c\u201d]+)",
            intent,
        ):
            candidate = m.group(1).strip()
            # 排除已被引号匹配提取的，以及动词性残留（如"框输入"）
            if candidate and candidate not in labels and not re.match(r"^(框|的|了|在)", candidate):
                labels.append(candidate)

        return labels

    @staticmethod
    def _detect_component_library(semantic_dom: List[Dict[str, Any]]) -> str:
        """从 DOM 中自动检测组件库（轻量版，不依赖 skills_frontmatters）"""
        if not semantic_dom:
            return "generic"

        prefix_counts: Counter = Counter()
        for node in semantic_dom:
            cls = str(node.get("class") or "").lower()
            if not cls:
                attrs = node.get("attributes") or {}
                cls = str(attrs.get("class") or "").lower()
            if not cls:
                continue
            for part in cls.split():
                if "-" in part and len(part) >= 3:
                    segments = part.split("-")
                    prefix_counts[segments[0]] += 1

        if not prefix_counts:
            return "generic"

        top_prefix = max(prefix_counts, key=prefix_counts.get)

        # 已知组件库前缀映射
        library_map = {
            "el": "element-ui",
            "elx": "element-plus",
            "ant": "ant-design",
            "van": "vant",
            "ivu": "iview",
            "iv": "iview",
            "mui": "material-ui",
            "chakra": "chakra-ui",
            "nb": "ng-bootstrap",
            "mat": "angular-material",
            "p": "prime-ng",
        }

        return library_map.get(top_prefix, "generic")

    # ── 失败记录 ──────────────────────────────────────────

    def record_failure(self, route: str, action_type: str, component_type: str) -> None:
        """记录一次失败，降低对应规则的置信度"""
        structure = self._store.get(route)
        if not structure:
            return

        rule_key = f"{action_type}|{component_type}"
        rule = structure.rules.get(rule_key)
        if rule:
            rule.failure_count += 1
            # 如果失败次数远超成功次数，删除规则
            if rule.failure_count > rule.success_count * 3 and rule.failure_count > 5:
                structure.rules.pop(rule_key, None)
                logger.info(
                    "structure_learner REMOVE_RULE | route=%s | rule_key=%s | reason=low_confidence",
                    route, rule_key,
                )

    # ── 持久化 ──────────────────────────────────────────────

    def save_to_file(self, output_dir: Path) -> None:
        """持久化到 JSON 文件"""
        struct_dir = output_dir / _STRUCTURE_DIR_NAME
        try:
            struct_dir.mkdir(parents=True, exist_ok=True)
            struct_file = struct_dir / "page_structure_learner.json"
            data = {
                "version": 1,
                "saved_at": time.time(),
                "pages": {
                    route: {
                        "route": ps.route,
                        "component_library": ps.component_library,
                        "dom_signature": ps.dom_signature,
                        "rules": {
                            rk: asdict(lr) for rk, lr in ps.rules.items()
                        },
                        "learned_at": ps.learned_at,
                        "updated_at": ps.updated_at,
                        "hit_count": ps.hit_count,
                    }
                    for route, ps in self._store.items()
                },
            }
            struct_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                "structure_learner SAVED | file=%s | pages=%d | total_rules=%d",
                struct_file, len(self._store),
                sum(len(ps.rules) for ps in self._store.values()),
            )
        except Exception as e:
            logger.warning("structure_learner 保存失败: %s", e)

    def load_from_file(self, output_dir: Path) -> None:
        """从 JSON 文件加载"""
        struct_file = output_dir / _STRUCTURE_DIR_NAME / "page_structure_learner.json"
        if not struct_file.exists():
            logger.debug("structure_learner 无历史文件: %s", struct_file)
            return

        try:
            raw = struct_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            pages = data.get("pages") or {}
            loaded = 0
            rules_loaded = 0
            for route, page_data in pages.items():
                rules = {}
                for rk, rule_data in page_data.get("rules", {}).items():
                    if isinstance(rule_data, dict):
                        rules[rk] = LearnedRule(
                            action_type=rule_data.get("action_type", ""),
                            component_type=rule_data.get("component_type", "generic"),
                            selector_template=rule_data.get("selector_template", ""),
                            selector_type=rule_data.get("selector_type", "css"),
                            anchor_type=rule_data.get("anchor_type", "text"),
                            success_count=rule_data.get("success_count", 1),
                            failure_count=rule_data.get("failure_count", 0),
                        )
                        rules_loaded += 1
                self._store[route] = PageStructure(
                    route=page_data.get("route", route),
                    component_library=page_data.get("component_library", "generic"),
                    dom_signature=page_data.get("dom_signature", ""),
                    rules=rules,
                    learned_at=page_data.get("learned_at", 0),
                    updated_at=page_data.get("updated_at", 0),
                    hit_count=page_data.get("hit_count", 0),
                )
                loaded += 1

            logger.info(
                "structure_learner LOADED | file=%s | pages=%d | rules=%d",
                struct_file, loaded, rules_loaded,
            )
        except Exception as e:
            logger.warning("structure_learner 加载失败: %s", e)

    # ── 统计 ──────────────────────────────────────────────

    @property
    def size(self) -> int:
        return len(self._store)

    @property
    def total_rules(self) -> int:
        return sum(len(ps.rules) for ps in self._store.values())

    @property
    def stats(self) -> Dict[str, Any]:
        """页面结构学习命中率统计。"""
        lookups = self._stats_lookups or 1
        total_hits = self._stats_exact_hits + self._stats_similar_hits
        return {
            "lookups": self._stats_lookups,
            "exact_hits": self._stats_exact_hits,
            "similar_hits": self._stats_similar_hits,
            "total_hits": total_hits,
            "misses": self._stats_lookups - total_hits,
            "hit_rate": round(total_hits / lookups * 100, 1),
            "exact_hit_rate": round(self._stats_exact_hits / lookups * 100, 1),
            "similar_hit_rate": round(self._stats_similar_hits / lookups * 100, 1),
            "pages": len(self._store),
            "total_rules": self.total_rules,
        }
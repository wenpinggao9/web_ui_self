"""步骤⑨ L5 元素决策 —— 大模型 + Skill/二次LLM (五级链最后一级).

在 L1/L2/L3/L4 未命中后: action_type 预过滤 → LLM 选 index 或 use_skill → 可选二次 LLM.
Skill 脚本执行与 selector 验证在 resolver 中完成 (需 page).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..dom import DomIndex
from ..llm import LLMAdapter, PromptLoader
from ..skill_loader import get_component_structure
from .action_type_filter import filter_items_by_action_type, item_matches_action_type
from .decide_result import DecideResult
from .intent_align import (
    append_element_decide_user_hints,
    climb_to_matching_node,
    validate_menu_node_index,
)
from .skill_resolver import dispatch_skill

_DEFAULT_SYSTEM = """\
你是 UI 元素选择助手. 根据语义 DOM、intent、action_type 选出最匹配的 node_index, 只输出 JSON.

动作类型:
- fill → 可编辑 input/textarea (非 checkbox/radio/combobox 只读触发框)
- click → button/a/可点击元素; 展开下拉 → combobox 触发器; 「在下拉选项中点击」→ role=option
- assert_* → 按语义选含文本/容器/表格元素

多候选时: 按 intent 场景区分; 弹窗内优先 [弹窗] 标记节点.
选择 index 时必须核对 text/placeholder/name/role 与 intent 语义一致.

输出格式 (二选一):
格式一: {"index": int|null, "reason": "...", "confidence": 0.0-1.0}
格式二A (节点选择 skill): {"use_skill": {"skill_name": "choose_best_input_target|choose_best_click_target|choose_best_checkbox_target|find_switch_in_row", "index": int|null, "reason": "..."}}
格式二B (selector skill): {"use_skill": {"skill_name": "build_dropdown_option_selector|build_el_select_trigger_selector|build_checkbox_selector|build_radio_selector|build_tree_checkbox_selector|build_tree_node_selector|build_date_picker_selector", "index": int|null, "target_text": "...", "reason": "..."}}
菜单导航可选: {"skip_navigation": true, "reason": "..."}

index 必须为列表中 [数字] 的原始索引. 找不到则 index=-1. 只输出 JSON."""

_TEXT_ANCHOR_RETRY_SYSTEM = """\
你是「文本锚点定位」助手. 在语义 DOM 中找出 text/placeholder/aria-label 与 intent 最一致的一个节点.
不限制标签类型. 必须返回列表中的原始 index. 只输出 JSON:
{"index": <int 或 -1>, "reason": "...", "confidence": 0.0-1.0}"""

_XPATH_LLM_SYSTEM = """\
你是 XPath 构建专家. 根据 HTML 结构与需要点击的元素片段, 生成 Playwright 可用的 XPath selector.
只输出一行 XPath, 以 (// 开头并以 )[1] 结尾, 不要解释."""

_DEFAULT_USER = """\
动作类型: {{action_type}}
操作意图: {{intent}}

页面元素 (编号从 0 开始, 方括号内为 index):
{{dom}}"""

_SKILL_TO_COMPONENT_TYPE = {
    "build_dropdown_option_selector": "dropdown_option",
    "build_el_select_trigger_selector": "select_trigger",
    "build_checkbox_selector": "checkbox",
    "build_radio_selector": "radio",
    "build_tree_checkbox_selector": "tree_checkbox",
    "build_tree_node_selector": "tree_node",
}


class LLMElementDecider:
    """五级链 L5: LLM 元素决策 + skill 协议解析."""

    def __init__(
        self,
        llm: LLMAdapter,
        prompts: PromptLoader,
        skill_prompt: str = "",
        skill_path: str | Path | None = None,
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        self.skill_prompt = skill_prompt or ""
        self.skill_path = Path(skill_path) if skill_path else None

    def decide(
        self,
        dom: DomIndex,
        intent: str,
        action_type: str,
        *,
        items: Optional[list[dict]] = None,
        exclude: Optional[list[str]] = None,
        hint: Optional[str] = None,
        action_value: str = "",
        feature_titles_menu_nav: bool = False,
        feature_titles: Optional[list[str]] = None,
    ) -> DecideResult:
        if len(dom) == 0:
            return DecideResult(index=None, reason="DOM 为空")

        filtered_indices = self._filtered_indices(items, action_type, len(dom.selectors))
        prompt_dom = self._build_prompt_dom(dom, filtered_indices)
        intent_text = intent if not hint else intent
        hint_block = ""
        if hint:
            hint_block = (
                "\n\n【重试策略提示】上一步页面校验未通过, 请参考:\n"
                f"{hint.strip()[:800]}"
            )

        system = self._system_prompt()
        base_user = self.prompts.user(
            "element_decide", _DEFAULT_USER,
            action_type=action_type,
            intent=intent_text + hint_block,
            dom=prompt_dom,
        )
        user = append_element_decide_user_hints(
            base_user,
            action_type=action_type,
            intent=intent_text + hint_block,
            feature_titles_menu_nav=feature_titles_menu_nav,
        )

        try:
            data = self.llm.complete_json("element_decide", system, user).data
        except Exception:
            return DecideResult(index=None, reason="LLM 调用失败")

        if not isinstance(data, dict):
            return DecideResult(index=None, reason="LLM 响应非对象")

        if feature_titles_menu_nav and data.get("skip_navigation") is True:
            return DecideResult(
                skip_navigation=True,
                reason=str(data.get("reason") or "菜单导航: 无需点击"),
                confidence=float(data.get("confidence", 1.0)),
            )

        use_skill = data.get("use_skill")
        if isinstance(use_skill, dict):
            result = self._parse_use_skill(
                use_skill, items or [], intent, action_type,
                action_value=action_value, exclude=exclude,
            )
            return self._apply_menu_validation(
                result, items, intent,
                feature_titles_menu_nav=feature_titles_menu_nav,
                feature_titles=feature_titles,
            )

        idx, confidence, reason = self._parse_index_response(data, dom, exclude)
        first_gave_valid = idx is not None and idx >= 0

        if idx is not None and not first_gave_valid:
            idx = None

        if feature_titles_menu_nav and confidence < 0.5:
            return DecideResult(
                index=None,
                reason=f"菜单导航置信度过低 ({confidence:.2f})",
                confidence=confidence,
            )

        should_retry = bool(action_type) and (confidence <= 0.3 or not first_gave_valid)
        if should_retry and not feature_titles_menu_nav:
            retry = self._retry_text_anchor(dom, intent, hint, exclude)
            if retry.index is not None and retry.index >= 0:
                climbed = climb_to_matching_node(items or [], retry.index, action_type, intent)
                final_idx = climbed if climbed is not None else retry.index
                if items and not item_matches_action_type(items[final_idx], action_type):
                    if climbed is None:
                        final_idx = retry.index
                result = DecideResult(
                    index=final_idx,
                    reason=retry.reason or "二次LLM文本锚点",
                    confidence=max(retry.confidence, 0.7),
                )
                return self._apply_menu_validation(
                    result, items, intent,
                    feature_titles_menu_nav=feature_titles_menu_nav,
                    feature_titles=feature_titles,
                )

        result = DecideResult(index=idx, reason=reason or "", confidence=confidence)
        return self._apply_menu_validation(
            result, items, intent,
            feature_titles_menu_nav=feature_titles_menu_nav,
            feature_titles=feature_titles,
        )

    def build_selector_via_llm(
        self,
        component_library: str,
        component_type: str,
        target_text: str,
        html_structure: str,
        click_target_html: str,
    ) -> Optional[str]:
        """根据 component_structures 调用 LLM 生成 XPath."""
        if not target_text or not (html_structure or click_target_html):
            return None
        prompt = (
            f"根据以下信息生成 Playwright XPath selector.\n\n"
            f"组件库: {component_library}\n组件类型: {component_type}\n目标文本: {target_text}\n\n"
            f"典型 HTML 结构:\n{html_structure}\n\n"
            f"需要点击的元素:\n{click_target_html}\n\n"
            f"要求:\n"
            f"1. 用「{target_text}」做文本锚定 (normalize-space 或 contains)\n"
            f"2. 定位到需要点击的元素\n3. 末尾加 [1]\n"
            f"4. 只输出一行 XPath"
        )
        try:
            xpath = self.llm.complete_text("element_decide_xpath", _XPATH_LLM_SYSTEM, prompt)
        except Exception:
            return None
        xpath = (xpath or "").strip()
        if xpath.startswith("```"):
            lines = xpath.split("\n")
            xpath = lines[-1] if len(lines) > 1 else xpath[3:]
        if xpath.endswith("```"):
            xpath = xpath[:-3]
        xpath = xpath.strip().strip("`").strip('"').strip("'")
        if xpath and ("/" in xpath or xpath.startswith("(")):
            if not xpath.startswith("xpath="):
                return xpath if xpath.startswith("(") else f"xpath={xpath}"
            return xpath
        return None

    def try_llm_component_selector(
        self,
        skill_name: str,
        target_text: str,
        component_library: str,
    ) -> Optional[str]:
        if not self.skill_path:
            return None
        comp_type = _SKILL_TO_COMPONENT_TYPE.get(skill_name, "")
        if not comp_type:
            return None
        structure = get_component_structure(self.skill_path, component_library, comp_type)
        if not structure:
            structure = get_component_structure(self.skill_path, "generic", comp_type)
        if not structure:
            return None
        return self.build_selector_via_llm(
            component_library,
            comp_type,
            target_text,
            structure.get("html", ""),
            structure.get("click_target", ""),
        )

    @staticmethod
    def _apply_menu_validation(
        result: DecideResult,
        items: Optional[list[dict]],
        intent: str,
        *,
        feature_titles_menu_nav: bool,
        feature_titles: Optional[list[str]],
    ) -> DecideResult:
        if not feature_titles_menu_nav or result.skip_navigation:
            return result
        idx = result.index
        if idx is None or idx < 0 or not items:
            return result
        if validate_menu_node_index(items, idx, intent, feature_titles=feature_titles):
            return result
        return DecideResult(
            index=None,
            reason=f"菜单导航命中元素与 intent 目标不一致",
            confidence=0.0,
        )

    def _system_prompt(self) -> str:
        base = self.prompts.system("element_decide", _DEFAULT_SYSTEM)
        if self.skill_prompt:
            return base + "\n\n" + self.skill_prompt
        return base

    @staticmethod
    def _filtered_indices(items: Optional[list[dict]], action_type: str, dom_len: int) -> Optional[list[int]]:
        if not items or len(items) != dom_len:
            return None
        filtered = filter_items_by_action_type(items, action_type)
        if len(filtered) == len(items):
            return None
        idx_set = {id(x) for x in filtered}
        return [i for i, it in enumerate(items) if id(it) in idx_set]

    @staticmethod
    def _build_prompt_dom(dom: DomIndex, filtered_indices: Optional[list[int]]) -> str:
        if filtered_indices is None:
            return dom.numbered_text
        lines = dom.numbered_text.split("\n")
        picked = [lines[i] for i in filtered_indices if 0 <= i < len(lines)]
        note = f"(已按 action_type 过滤, 共 {len(picked)} 个候选, index 仍为原始编号)\n"
        return note + "\n".join(picked)

    def _parse_use_skill(
        self,
        use_skill: dict,
        items: list[dict],
        intent: str,
        action_type: str,
        *,
        action_value: str = "",
        exclude: Optional[list[str]] = None,
    ) -> DecideResult:
        skill_name = str(use_skill.get("skill_name") or "").strip()
        base_index = use_skill.get("index")
        if base_index is not None and not isinstance(base_index, int):
            try:
                base_index = int(base_index)
            except (TypeError, ValueError):
                base_index = None
        target_text = str(use_skill.get("target_text") or "").strip()
        reason = str(use_skill.get("reason") or "")

        idx, sel = dispatch_skill(
            skill_name, items, intent, action_type,
            base_index=base_index,
            action_value=action_value,
            target_text=target_text,
            page=None,
            exclude=set(exclude or []),
        )
        return DecideResult(
            index=idx,
            recommended_selector=sel,
            skill_name=skill_name,
            reason=reason,
            confidence=0.85,
        )

    def _parse_index_response(
        self,
        data: dict,
        dom: DomIndex,
        exclude: Optional[list[str]],
    ) -> tuple[Optional[int], float, str]:
        idx = _as_int(data.get("index"))
        if idx is None:
            target = data.get("target")
            if isinstance(target, dict):
                idx = _as_int(target.get("index") or target.get("node_index"))
        confidence = float(data.get("confidence", 0.5) or 0.5)
        reason = str(data.get("reason") or "")
        if idx is None:
            return None, confidence, reason
        if idx < 0 or idx >= len(dom.selectors):
            return idx if idx == -1 else None, confidence, reason
        from .playwright_api import info_key
        info = dom.selectors[idx]
        if exclude and info_key(info) in set(exclude):
            return None, confidence, reason
        return idx, confidence, reason

    def _retry_text_anchor(
        self,
        dom: DomIndex,
        intent: str,
        hint: Optional[str],
        exclude: Optional[list[str]],
    ) -> DecideResult:
        hint_block = f"\n\n【重试提示】\n{hint}" if hint else ""
        base_user = self.prompts.user(
            "element_decide", _DEFAULT_USER,
            action_type="未过滤",
            intent=intent + hint_block,
            dom=dom.numbered_text,
        )
        user = append_element_decide_user_hints(
            base_user, action_type="未过滤", intent=intent + hint_block,
        )
        try:
            data = self.llm.complete_json(
                "element_decide_retry",
                _TEXT_ANCHOR_RETRY_SYSTEM,
                user,
            ).data
        except Exception:
            return DecideResult(index=None, reason="二次 LLM 失败")
        if not isinstance(data, dict):
            return DecideResult(index=None)
        idx = _as_int(data.get("index"))
        if idx is None:
            idx = _as_int(data.get("node_index"))
        if idx is not None and (idx < 0 or idx >= len(dom.selectors)):
            idx = None
        if idx is not None and exclude:
            from .playwright_api import info_key
            if info_key(dom.selectors[idx]) in set(exclude):
                idx = None
        return DecideResult(
            index=idx,
            reason=str(data.get("reason") or ""),
            confidence=float(data.get("confidence", 0.75) or 0.75),
        )


def _as_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

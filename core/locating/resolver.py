"""步骤⑨ 元素定位三级降级链编排器.

顺序: L1缓存 → L2记忆 → L3大模型. 逐级降级, 命中即返回.
- L1/L2 命中后校验可用; 失效则自愈, 自愈失败清除条目降级.
- L3 成功后回填 L1+L2, 下次同页面不再需要大模型.
- 步骤⑬ 失败连锁清理: evict(缓存) + penalize(记忆).
"""
from __future__ import annotations

from typing import Any, Optional

from rich.console import Console

from ..execution.trace import dom_console_print_enabled
from ..dom.semantic_dom import (
    build_locator_info,
    dom_index_from_items,
    dom_index_from_picked_indices,
    extract_dom_index,
    extract_semantic_items,
)
from .cache import SelectorCache
from .llm_decider import LLMElementDecider
from .intent_route import is_ant_radio_option, is_checkbox, is_tree_checkbox
from .memory import SelectorMemory
from .node_refiner import refine_node_index
from .normalize import normalize_intent, normalize_url, skip_locator_persistence, validate_selector
from .playwright_api import info_key
from .resolve_trace import ResolveChain
from .self_heal import heal
from .intent_window import pick_intent_window_indices
from .skill_resolver import (
    build_selector_via_skill,
    extract_target_text_from_intent,
    info_from_recommended_selector,
    resolve_component_type,
    try_auto_skill_selector,
)

_COMPONENT_TYPE_TO_SKILL = {
    "select_trigger": "build_el_select_trigger_selector",
    "dropdown_option": "build_dropdown_option_selector",
    "checkbox": "build_checkbox_selector",
    "radio": "build_radio_selector",
    "tree_checkbox": "build_tree_checkbox_selector",
    "tree_node": "build_tree_node_selector",
    "date_picker": "build_date_picker_selector",
    "text_input": "build_fill_input_selector",
}


def _is_strong_checkbox_selector(sel: str) -> bool:
    s = (sel or "").lower()
    return any(
        k in s
        for k in (
            "checkbox",
            'type="checkbox"',
            "type='checkbox'",
            "[type=checkbox]",
            "role=checkbox",
            "menuitemcheckbox",
            "ant-checkbox-wrapper",
            "el-checkbox",
        )
    )


def _needs_checkbox_selector_upgrade(info: dict, intent: str) -> bool:
    """复选框 intent 且 selector 非 checkbox 控件, 需 build_*_checkbox_selector 升级."""
    intent_s = intent or ""
    if not is_tree_checkbox(intent_s) and not is_checkbox(intent_s):
        return False
    return not _is_strong_checkbox_selector(info.get("selector") or "")


def _checkbox_upgrade_skill(intent: str) -> str:
    if is_tree_checkbox(intent or ""):
        return "build_tree_checkbox_selector"
    return "build_checkbox_selector"


def _is_strong_radio_selector(sel: str) -> bool:
    s = (sel or "").lower()
    return any(
        k in s
        for k in (
            "ant-radio-wrapper",
            "el-radio",
            "role=radio",
            'type="radio"',
            "type='radio'",
            "[type=radio]",
        )
    )


def _needs_radio_selector_upgrade(info: dict, intent: str) -> bool:
    """单选 intent 且 selector 非 wrapper/radio 控件, 需 build_radio_selector 升级."""
    if not is_ant_radio_option(intent or ""):
        return False
    return not _is_strong_radio_selector(info.get("selector") or "")


class LocatorResolver:
    """把一个自然语言意图解析成可执行选择器的统一入口."""

    def __init__(
        self,
        decider: LLMElementDecider,
        cache: Optional[SelectorCache] = None,
        memory: Optional[SelectorMemory] = None,
        console: Optional[Console] = None,
        *,
        dom_limit: int = 80,
        intent_window: bool = True,
    ) -> None:
        self.decider = decider
        self.cache = cache
        self.memory = memory
        self.console = console or Console()
        self.dom_limit = max(1, int(dom_limit))
        self.intent_window = bool(intent_window)
        self._trace: Optional[Any] = None
        self._framework_selectors: Optional[dict[str, str]] = None
        self.last_chain: Optional[ResolveChain] = None

    def set_trace(self, trace: Optional[Any]) -> None:
        """注入 ExecutionTrace, 用于打印三级定位链路."""
        self._trace = trace

    def set_framework_selectors(self, selectors: Optional[dict[str, str]]) -> None:
        """注入从 skill.md 加载的框架专属选择器."""
        self._framework_selectors = selectors

    def resolve(
        self,
        page: Any,
        intent: str,
        action_type: str,
        dom_limit: Optional[int] = None,
        exclude: Optional[list[str]] = None,
        hint: Optional[str] = None,
        action_value: str = "",
        semantic_items: Optional[list[dict]] = None,
        dom_source: str = "",
        skip_acceleration: bool = False,
        feature_titles_menu_nav: bool = False,
        feature_titles: Optional[list[str]] = None,
    ) -> Optional[dict]:
        limit = self.dom_limit if dom_limit is None else max(1, int(dom_limit))
        url = _url(page)
        excl = set(exclude or [])
        chain = ResolveChain(
            intent=intent,
            action_type=action_type,
            hint=hint,
            exclude=list(exclude or []),
        )
        self.last_chain = chain
        if skip_locator_persistence(action_type):
            chain.add("定位链", "跳过(assert_text)")
            self._emit_chain(chain)
            return None

        # L1 缓存
        if self.cache and not skip_acceleration:
            info = self.cache.get(url, action_type, intent)
            if not info:
                chain.add("L1缓存", "未命中")
            elif info_key(info) in excl:
                chain.add("L1缓存", "跳过(已排除)", info_key(info))
            elif validate_selector(page, info):
                info, upgraded, upgrade_label = self._maybe_upgrade_component_selector(
                    page, intent, info, semantic_items,
                )
                if upgraded:
                    chain.add("L1缓存", f"{upgrade_label}升级", info_key(info))
                    if self.cache:
                        self.cache.put(url, action_type, intent, info)
                chain.mark_hit("L1缓存", info_key(info))
                self._emit_chain(chain)
                return self._tag(info, "L1缓存")
            else:
                chain.add("L1缓存", "校验失败", info["selector"])
                healed = heal(page, info)
                if healed:
                    chain.add("L1自愈", "命中", healed["selector"])
                    self.cache.put(url, action_type, intent, healed)
                    chain.mark_hit("L1自愈", healed["selector"])
                    self._emit_chain(chain)
                    return self._tag(healed, "L1自愈")
                chain.add("L1自愈", "失败", info["selector"])
                self.cache.evict(url, action_type, intent)
        else:
            if skip_acceleration:
                chain.add("L1缓存", "跳过(重试)")
            else:
                chain.add("L1缓存", "未启用")

        # L2 记忆库
        if self.memory and not skip_acceleration:
            info = self.memory.get(url, action_type, intent)
            if not info:
                chain.add("L2记忆", "未命中")
            elif info_key(info) in excl:
                chain.add("L2记忆", "跳过(已排除)", info_key(info))
            elif validate_selector(page, info):
                info, upgraded, upgrade_label = self._maybe_upgrade_component_selector(
                    page, intent, info, semantic_items,
                )
                if upgraded:
                    chain.add("L2记忆", f"{upgrade_label}升级", info_key(info))
                    if self.memory:
                        self.memory.record_success(url, action_type, intent, info)
                chain.mark_hit("L2记忆", info_key(info))
                self._emit_chain(chain)
                return self._tag(info, "L2记忆")
            else:
                chain.add("L2记忆", "校验失败", info["selector"])
        else:
            if skip_acceleration:
                chain.add("L2记忆", "跳过(重试)")
            else:
                chain.add("L2记忆", "未启用")

        # L3 大模型 — 优先复用已抽取的 semantic_items (共用 DOM)
        if semantic_items:
            source_items = semantic_items
            dom_note_prefix = "共用"
        else:
            source_items = extract_semantic_items(
                page, dialog_first=True, stable=True,
                selectors=self._framework_selectors, profile="locate",
            )
            dom_note_prefix = "实时"

        # 规则 skill 扫完整 DOM; LLM 用意图窗口或前 N 条
        rule_items = source_items
        rule_info = self._try_rule_skill_resolve(
            page, intent, action_type, rule_items, excl, chain,
        )
        if rule_info is not None:
            self._backfill(url, action_type, intent, rule_info)
            chain.mark_hit("L3规则", rule_info.get("selector") or "")
            self._emit_chain(chain)
            return self._tag(rule_info, "L3规则")

        llm_items: list[dict]
        if (
            self.intent_window
            and len(source_items) > limit
        ):
            picked = pick_intent_window_indices(
                source_items, intent, action_type, limit=limit,
            )
            dom = dom_index_from_picked_indices(source_items, picked)
            llm_items = source_items
            dom_note = (
                f"DOM候选={len(picked)}个(意图窗口/{len(source_items)}, {dom_note_prefix})"
                + (", 有hint" if hint else "")
            )
        else:
            sliced = source_items[:limit]
            dom = dom_index_from_items(sliced, limit=limit)
            llm_items = sliced if len(source_items) > limit else source_items
            dom_note = (
                f"DOM候选={len(dom)}个({dom_note_prefix})"
                + (", 有hint" if hint else "")
            )

        chain.llm_called = True
        chain.add("L3大模型", "已调用", note=dom_note)

        if dom_console_print_enabled():
            if semantic_items:
                self.console.print(
                    f"  [dim]└─ DOM ({dom_note_prefix}, {len(dom)}→LLM / {len(source_items)} 全量) — 见上方抓取输出[/dim]"
                )
                if dom_source:
                    self.console.print(f"  [dim]   ↳ 来源: {dom_source}[/dim]")
            else:
                self.console.print(f"  [dim]└─ DOM (L3, {len(dom)} items):[/dim]")
                for line in dom.numbered_text.split("\n"):
                    self.console.print(f"  [dim]   {line}[/dim]")
        result = self.decider.decide(
            dom, intent, action_type,
            items=llm_items,
            exclude=list(excl),
            hint=hint,
            action_value=action_value,
            feature_titles_menu_nav=feature_titles_menu_nav,
            feature_titles=feature_titles,
        )
        if result.skip_navigation:
            chain.add("L3大模型", "跳过导航", note=result.reason[:80] if result.reason else "")
            chain.mark_hit("L3大模型", "__SKIP_NAV__")
            self._emit_chain(chain)
            return self._tag(
                {"method": "css", "selector": "__SKIP_NAV__", "_skip_navigation": True},
                "L3大模型",
            )

        info: Optional[dict] = None
        llm_index = result.index
        excl_set = set(excl)

        if result.recommended_selector:
            skill_info = info_from_recommended_selector(result.recommended_selector)
            if info_key(skill_info) not in excl_set and validate_selector(page, skill_info):
                chain.add(
                    "L3Skill", "命中",
                    info_key(skill_info),
                    note=result.skill_name or "recommended_selector",
                )
                info = skill_info

        if info is None and result.skill_name and result.skill_name.startswith("build_"):
            sel = build_selector_via_skill(
                result.skill_name, source_items, intent,
                target_text=extract_target_text_from_intent(intent) or "",
                page=page, exclude=excl_set,
            )
            if sel:
                skill_info = info_from_recommended_selector(sel)
                if info_key(skill_info) not in excl_set:
                    chain.add("L3Skill", "命中", info_key(skill_info), note=result.skill_name)
                    info = skill_info

        if info is None and llm_index is not None and llm_index >= 0:
            refined_idx, skill_name = refine_node_index(
                llm_items, llm_index, intent, action_type, action_value=action_value,
            )
            if skill_name and refined_idx != llm_index:
                chain.add("L3纠偏", "命中", f"{llm_index}->{refined_idx}", note=skill_name)
                llm_index = refined_idx
            if 0 <= llm_index < len(llm_items):
                auto_sel = try_auto_skill_selector(
                    llm_items, intent, action_type, llm_index,
                    page=page, exclude=excl_set,
                    skill_path=getattr(self.decider, "skill_path", None),
                    llm_xpath_builder=self._llm_xpath_builder(),
                )
                if auto_sel:
                    skill_info = info_from_recommended_selector(auto_sel)
                    if info_key(skill_info) not in excl_set and validate_selector(page, skill_info):
                        chain.add("L3Skill", "命中", info_key(skill_info), note="auto_skill")
                        info = skill_info
                if info is None:
                    info = build_locator_info(llm_items[llm_index])
                    info = dict(info)

        if info:
            note = f"index={llm_index}" if llm_index is not None else ""
            chain.add("L3大模型", "命中", info["selector"], note)
            self._backfill(url, action_type, intent, info)
            chain.mark_hit("L3大模型", info["selector"])
            self._emit_chain(chain)
            return self._tag(info, "L3大模型")
        chain.add("L3大模型", "未命中", note="index=-1/排除/调用失败")
        self._emit_chain(chain)
        return None

    def _try_rule_skill_resolve(
        self,
        page: Any,
        intent: str,
        action_type: str,
        items: list[dict],
        excl: set[str],
        chain: ResolveChain,
    ) -> Optional[dict]:
        """L3 规则: 语义 DOM 推断组件类型 → build_* skill, 不经 LLM."""
        comp = resolve_component_type(items, intent, action_type)
        if not comp:
            return None
        skill_name = _COMPONENT_TYPE_TO_SKILL.get(comp)
        if not skill_name:
            return None
        target = extract_target_text_from_intent(intent) or ""
        sel = build_selector_via_skill(
            skill_name, items, intent,
            target_text=target, page=page, exclude=excl,
        )
        if not sel:
            chain.add("L3规则", "未命中", note=skill_name)
            return None
        info = info_from_recommended_selector(sel)
        if info_key(info) in excl or not validate_selector(page, info):
            chain.add("L3规则", "校验失败", info_key(info), note=skill_name)
            return None
        chain.add("L3规则", "命中", info_key(info), note=skill_name)
        return info

    def _maybe_upgrade_component_selector(
        self,
        page: Any,
        intent: str,
        info: dict,
        semantic_items: Optional[list[dict]] = None,
    ) -> tuple[dict, bool, str]:
        """L1/L2 命中弱 selector 时, 用 build_* skill 升级为 wrapper/控件 selector."""
        upgrade_plans: list[tuple[str, str]] = []
        if _needs_radio_selector_upgrade(info, intent):
            upgrade_plans.append(("build_radio_selector", "单选"))
        if _needs_checkbox_selector_upgrade(info, intent):
            upgrade_plans.append((_checkbox_upgrade_skill(intent), "复选框"))
        if not upgrade_plans:
            return info, False, ""

        items = semantic_items
        if not items:
            items = extract_semantic_items(
                page, dialog_first=True, stable=True,
                selectors=self._framework_selectors, profile="locate",
            )
        if not items:
            return info, False, ""

        target_text = extract_target_text_from_intent(intent) or ""
        for skill_name, label in upgrade_plans:
            sel = build_selector_via_skill(
                skill_name,
                items,
                intent,
                target_text=target_text,
                page=page,
            )
            if not sel:
                continue
            upgraded = info_from_recommended_selector(sel)
            if validate_selector(page, upgraded):
                return upgraded, True, label
        return info, False, ""

    def _emit_chain(self, chain: ResolveChain) -> None:
        if self._trace is None:
            return
        self._trace.emit(
            "locate_chain",
            intent=chain.intent,
            action_type=chain.action_type,
            hint=chain.hint,
            exclude=chain.exclude,
            steps=chain.steps,
            llm_called=chain.llm_called,
            hit_level=chain.hit_level,
            hit_selector=chain.hit_selector,
        )

    # ---------- 回填 ----------
    def _backfill(self, url: str, action_type: str, intent: str, info: dict) -> None:
        if skip_locator_persistence(action_type):
            return
        # 宽松子串匹配 (text=X 不带引号) 容易误匹配, 不回填到缓存,
        # 避免下次同意图直接复用导致点错元素.
        sel = info.get("selector", "")
        if sel.startswith("text=") and not sel.startswith('text="'):
            self._emit_backfill(
                url, action_type, intent, info,
                skipped=True, reason="宽松 text= 匹配不回填",
            )
            return
        if _needs_radio_selector_upgrade(info, intent) or _needs_checkbox_selector_upgrade(info, intent):
            self._emit_backfill(
                url, action_type, intent, info,
                skipped=True, reason="单选/复选框弱 selector 不回填",
            )
            return
        wrote_l1 = bool(self.cache)
        wrote_l2 = bool(self.memory)
        l2_score: Optional[int] = None
        if self.cache:
            self.cache.put(url, action_type, intent, info)
        if self.memory:
            self.memory.record_success(url, action_type, intent, info)
            k = self.memory._key(url, action_type, intent)
            entry = self.memory._data.get(k)
            if entry:
                l2_score = int(entry.get("score") or 0)
        self._emit_backfill(
            url, action_type, intent, info,
            wrote_l1=wrote_l1, wrote_l2=wrote_l2, l2_score=l2_score,
        )

    def _emit_backfill(
        self,
        url: str,
        action_type: str,
        intent: str,
        info: dict,
        *,
        skipped: bool = False,
        reason: str = "",
        wrote_l1: bool = False,
        wrote_l2: bool = False,
        l2_score: Optional[int] = None,
    ) -> None:
        cache_key = (
            f"{normalize_url(url)} | {action_type} | {normalize_intent(intent)}"
        )
        payload = {
            "cache_key": cache_key,
            "selector": info_key(info),
            "skipped": skipped,
            "reason": reason,
            "l1": wrote_l1,
            "l2": wrote_l2,
            "l2_score": l2_score,
        }
        if self._trace is not None:
            self._trace.emit("locate_backfill", **payload)
        else:
            self._print_backfill(payload)

    def _print_backfill(self, data: dict[str, Any]) -> None:
        if data.get("skipped"):
            self.console.print(
                f"[dim]  │   L3回填: 跳过 ({data.get('reason')}) "
                f"selector={data.get('selector')!r}[/dim]"
            )
            return
        parts = []
        if data.get("l1"):
            parts.append("L1缓存")
        if data.get("l2"):
            score = data.get("l2_score")
            parts.append(f"L2记忆(score={score})" if score is not None else "L2记忆")
        targets = "+".join(parts) if parts else "无"
        self.console.print(
            f"[cyan]  │   L3回填 → {targets}[/cyan] "
            f"[dim]key={data.get('cache_key')!r} selector={data.get('selector')!r}[/dim]"
        )

    # ---------- 步骤⑬ 失败连锁清理 ----------
    def evict(self, page: Any, intent: str, action_type: str, selector: Optional[str]) -> None:
        url = _url(page)
        if self.cache:
            self.cache.evict(url, action_type, intent)

    def penalize(self, page: Any, intent: str, action_type: str, selector: Optional[str]) -> None:
        url = _url(page)
        if self.memory:
            self.memory.record_failure(url, action_type, intent, selector)

    @staticmethod
    def _tag(info: dict, source: str) -> dict:
        out = dict(info)
        out["_source"] = source
        return out

    def _llm_xpath_builder(self):
        decider = self.decider
        if not getattr(decider, "skill_path", None):
            return None

        def _build(skill_name: str, target_text: str, component_library: str) -> Optional[str]:
            fn = getattr(decider, "try_llm_component_selector", None)
            if not callable(fn):
                return None
            return fn(skill_name, target_text, component_library)

        return _build


def _url(page: Any) -> str:
    """安全读取当前页面 URL, 避免页面关闭等异常打断定位链."""
    try:
        return page.url or ""
    except Exception:
        return ""

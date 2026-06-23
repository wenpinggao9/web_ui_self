"""步骤⑨ 元素定位五级降级链编排器.

顺序: L1缓存 → L2记忆 → L3规则(+build_* skill) → L4学习(Composite) → L5大模型.
- L2 内含通用模板子策略 (lookup_generic); L1 含自愈分支.
- L1/L2 可在 DOM 抽取前短路命中 (dispatcher try_acceleration_only).
- L1/L2 命中后校验可用; 失效则自愈, 自愈失败清除条目降级.
- L3 规则: IntentRuleEngine + build_* skill, 不经 LLM.
- L4 学习: CompositeStructureLearner (Jaccard + 页面结构), 跨批次持久化.
- L5: LLM → 本地 fallback → 二次 LLM (confidence≤0.3); 成功后回填 L1+L2+L4.
- 步骤⑬ 失败连锁清理: evict(缓存) + penalize(记忆+学习).
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
from .intent_route import (
    is_ant_radio_option,
    is_checkbox,
    is_dropdown_option,
    is_tree_checkbox,
    is_unsafe_dropdown_option_selector,
)
from .intent_rule_engine import IntentRuleEngine
from .memory import SelectorMemory
from .node_refiner import refine_node_index
from .normalize import normalize_intent, normalize_url, skip_locator_persistence, validate_selector
from .playwright_api import info_key
from .resolve_trace import ResolveChain
from .selector_type import infer_selector_type
from .composite_learner import CompositeStructureLearner
from .locate_observability import summarize_locating_stats
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
        learner: Optional[CompositeStructureLearner] = None,
        rule_engine: Optional[IntentRuleEngine] = None,
        console: Optional[Console] = None,
        *,
        dom_limit: int = 80,
        intent_window: bool = True,
    ) -> None:
        self.decider = decider
        self.cache = cache
        self.memory = memory
        self.learner = learner
        self.rule_engine = rule_engine if rule_engine is not None else IntentRuleEngine()
        self.console = console or Console()
        self.dom_limit = max(1, int(dom_limit))
        self.intent_window = bool(intent_window)
        self._trace: Optional[Any] = None
        self._framework_selectors: Optional[dict[str, str]] = None
        self.last_chain: Optional[ResolveChain] = None
        self._resolve_hits: dict[str, int] = {}
        self._case_stats_baseline: Optional[dict[str, Any]] = None

    def begin_case_stats(self) -> None:
        """用例开始时快照, 供 case_locating_stats 计算本用例增量."""
        decider_snap: dict[str, int] = {}
        if self.decider is not None and hasattr(self.decider, "stats_snapshot"):
            decider_snap = self.decider.stats_snapshot()
        self._case_stats_baseline = {
            "resolve_hits": dict(self._resolve_hits),
            "decider": decider_snap,
        }

    def case_locating_stats(self) -> dict[str, Any]:
        """本用例五级定位统计 (相对 begin_case_stats 增量 + 模块累计)."""
        from .locate_observability import counter_delta, summarize_locating_stats

        baseline = self._case_stats_baseline or {}
        resolve_delta = counter_delta(
            baseline.get("resolve_hits") or {},
            self._resolve_hits,
        )
        decider_delta: dict[str, Any] = {}
        if self.decider is not None and hasattr(self.decider, "stats_snapshot"):
            raw = counter_delta(
                baseline.get("decider") or {},
                self.decider.stats_snapshot(),
            )
            if raw:
                calls = raw.get("llm_calls", 0) + raw.get("retry_llm_calls", 0)
                decider_delta = {
                    **raw,
                    "total_llm_calls": calls,
                    "fallback_rate": round(raw.get("fallback_hits", 0) / calls * 100, 1) if calls else 0.0,
                }
        return summarize_locating_stats(
            cache=self.cache,
            memory=self.memory,
            rule_engine=self.rule_engine,
            learner=self.learner,
            decider=self.decider,
            resolve_hits=resolve_delta or None,
            decider_stats=decider_delta or None,
            scope="case",
        )

    def set_trace(self, trace: Optional[Any]) -> None:
        """注入 ExecutionTrace, 用于打印五级定位链路."""
        self._trace = trace

    def set_framework_selectors(self, selectors: Optional[dict[str, str]]) -> None:
        """注入从 skill.md 加载的框架专属选择器."""
        self._framework_selectors = selectors

    def try_acceleration_only(
        self,
        page: Any,
        intent: str,
        action_type: str,
        exclude: Optional[list[str]] = None,
        semantic_items: Optional[list[dict]] = None,
        *,
        skip_heuristics: bool = False,
    ) -> Optional[dict]:
        """仅 L1/L2 (含 L2 通用模板); L1 命中时跳过 DOM 抽取 (对齐 V3 cache 短路)."""
        chain = ResolveChain(
            intent=intent,
            action_type=action_type,
            hint=None,
            exclude=list(exclude or []),
        )
        self.last_chain = chain
        if skip_locator_persistence(action_type):
            return None
        hit = self._resolve_acceleration_layers(
            page, intent, action_type, set(exclude or []),
            semantic_items, skip_heuristics, chain,
        )
        if hit:
            self._emit_chain(chain)
        return hit

    @staticmethod
    def _reject_unsafe_accel_hit(
        intent: str,
        info: dict,
        chain: ResolveChain,
        layer: str,
    ) -> bool:
        """True → 拒绝该 L1/L2 命中 (继续降级)."""
        sel = info_key(info)
        if is_unsafe_dropdown_option_selector(intent, sel):
            chain.add(layer, "跳过(下拉option禁bare text)", sel)
            return True
        return False

    def _resolve_acceleration_layers(
        self,
        page: Any,
        intent: str,
        action_type: str,
        excl: set[str],
        semantic_items: Optional[list[dict]],
        skip_heuristics: bool,
        chain: ResolveChain,
    ) -> Optional[dict]:
        url = _url(page)

        if self.cache:
            info = self.cache.lookup(page, url, action_type, intent)
            if not info:
                chain.add("L1缓存", "未命中")
            elif info_key(info) in excl:
                chain.add("L1缓存", "跳过(已排除)", info_key(info))
            else:
                from_cache_heal = bool(info.pop("_from_cache_heal", False))
                info, upgraded, upgrade_label = self._maybe_upgrade_component_selector(
                    page, intent, info, semantic_items,
                )
                if upgraded:
                    label = "L1自愈" if from_cache_heal else "L1缓存"
                    chain.add(label, f"{upgrade_label}升级", info_key(info))
                    self.cache.put(url, action_type, intent, info, node=info)
                if from_cache_heal:
                    chain.mark_hit("L1缓存", info_key(info), note="自愈")
                    if not self._reject_unsafe_accel_hit(intent, info, chain, "L1缓存"):
                        return self._tag_and_track(info, "L1缓存")
                elif not self._reject_unsafe_accel_hit(intent, info, chain, "L1缓存"):
                    chain.mark_hit("L1缓存", info_key(info))
                    return self._tag_and_track(info, "L1缓存")

        if self.memory:
            info = self.memory.lookup_validate(page, url, action_type, intent)
            if not info:
                chain.add("L2记忆", "未命中")
            elif info_key(info) in excl:
                chain.add("L2记忆", "跳过(已排除)", info_key(info))
                info = self.memory.get(url, action_type, intent)
                if info:
                    info, upgraded, upgrade_label = self._maybe_upgrade_component_selector(
                        page, intent, info, semantic_items,
                    )
                    self._backfill_l1(url, action_type, intent, info)
                    if not self._reject_unsafe_accel_hit(intent, info, chain, "L2记忆"):
                        chain.mark_hit("L2记忆", info_key(info))
                        return self._tag_and_track(info, "L2记忆")
            else:
                info, upgraded, upgrade_label = self._maybe_upgrade_component_selector(
                    page, intent, info, semantic_items,
                )
                if upgraded:
                    chain.add("L2记忆", f"{upgrade_label}升级", info_key(info))
                self._backfill_l1(url, action_type, intent, info)
                if not self._reject_unsafe_accel_hit(intent, info, chain, "L2记忆"):
                    chain.mark_hit("L2记忆", info_key(info))
                    return self._tag_and_track(info, "L2记忆")

        skip_generic = skip_heuristics or is_dropdown_option(intent)
        if self.memory and not skip_generic:
            gen_items = semantic_items
            if not gen_items:
                gen_items = extract_semantic_items(
                    page, dialog_first=True, stable=True,
                    selectors=self._framework_selectors, profile="locate",
                )
            gen_info = self.memory.lookup_generic(
                page, action_type, intent, gen_items,
                component_library=self._detect_component_library(),
            )
            if not gen_info:
                chain.add("L2记忆", "通用·未命中")
            elif info_key(gen_info) in excl:
                chain.add("L2记忆", "通用·跳过(已排除)", info_key(gen_info))
            else:
                self._backfill_l1(url, action_type, intent, gen_info)
                if not self._reject_unsafe_accel_hit(intent, gen_info, chain, "L2记忆"):
                    chain.mark_hit("L2记忆", info_key(gen_info), note="通用模板")
                    return self._tag_and_track(gen_info, "L2记忆")
        elif self.memory and skip_generic and is_dropdown_option(intent):
            chain.add("L2记忆", "通用·跳过(下拉option)")

        return None

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
        skip_heuristics: bool = False,
        acceleration_prefetched: bool = False,
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

        if not skip_acceleration:
            hit = self._resolve_acceleration_layers(
                page, intent, action_type, excl, semantic_items,
                skip_heuristics, chain,
            )
            if hit:
                self._emit_chain(chain)
                return hit
        else:
            prefetched_note = "已在短路路径尝试" if acceleration_prefetched else "跳过(重试)"
            chain.add("L1缓存", prefetched_note)
            chain.add("L2记忆", prefetched_note)
            if skip_heuristics:
                chain.add("L2记忆", f"通用·{prefetched_note}")

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

        # 规则: V3 IntentRuleEngine → skill; LLM 用意图窗口或前 N 条
        rule_items = source_items
        run_l3_rules = not skip_heuristics or is_dropdown_option(intent)
        if run_l3_rules:
            intent_rule_info = self._try_intent_rule_resolve(
                page, intent, action_type, rule_items, excl, chain,
            )
            if intent_rule_info is not None:
                self._backfill(url, action_type, intent, intent_rule_info, semantic_items=rule_items)
                chain.mark_hit("L3规则", intent_rule_info.get("selector") or "")
                self._emit_chain(chain)
                return self._tag_and_track(intent_rule_info, "L3规则")

            rule_info = self._try_rule_skill_resolve(
                page, intent, action_type, rule_items, excl, chain,
            )
            if rule_info is not None:
                self._backfill(url, action_type, intent, rule_info, semantic_items=rule_items)
                chain.mark_hit("L3规则", rule_info.get("selector") or "")
                self._emit_chain(chain)
                return self._tag_and_track(rule_info, "L3规则")
        else:
            chain.add("L3规则", "跳过(重试)")

        # L4 学习: PageStructureLearner (route + 组件模板 + DOM 指纹)
        # skip_acceleration 仅跳过 L1/L2 重复查找; L4 仍应执行 (对齐 V3)
        if self.learner and not skip_heuristics:
            learn_info = self.learner.resolve(
                page, url, action_type, intent, semantic_items=source_items,
            )
            if learn_info is not None:
                self._backfill(
                    url, action_type, intent, learn_info, semantic_items=source_items,
                )
                chain.mark_hit("L4学习", learn_info.get("selector") or "")
                self._emit_chain(chain)
                return self._tag_and_track(learn_info, "L4学习")
            chain.add("L4学习", "未命中")
        elif self.learner and skip_heuristics:
            chain.add("L4学习", "跳过(重试)")
        elif not self.learner:
            chain.add("L4学习", "未启用")

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
        chain.add("L5大模型", "已调用", note=dom_note)

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
            chain.add("L5大模型", "跳过导航", note=result.reason[:80] if result.reason else "")
            chain.mark_hit("L5大模型", "__SKIP_NAV__")
            self._emit_chain(chain)
            return self._tag_and_track(
                {"method": "css", "selector": "__SKIP_NAV__", "_skip_navigation": True},
                "L5大模型",
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
                info["_from_skill"] = True

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
                    info["_from_skill"] = True

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
                        info["_from_skill"] = True
                if info is None:
                    info = build_locator_info(llm_items[llm_index])
                    info = dict(info)

        if info:
            note = f"index={llm_index}" if llm_index is not None else ""
            chain.add("L5大模型", "命中", info["selector"], note)
            self._backfill(url, action_type, intent, info, semantic_items=source_items)
            chain.mark_hit("L5大模型", info["selector"])
            self._emit_chain(chain)
            return self._tag_and_track(info, "L5大模型")
        chain.add("L5大模型", "未命中", note="index=-1/排除/调用失败")
        self._emit_chain(chain)
        return None

    def _backfill_l1(
        self,
        url: str,
        action_type: str,
        intent: str,
        info: dict,
    ) -> None:
        """L2 命中后回填 L1 (对齐 V3 selector_memory → selector_cache)."""
        if self.cache and info:
            self.cache.put(url, action_type, intent, info, node=info)
            self.cache.touch(url, action_type, intent)

    def _try_intent_rule_resolve(
        self,
        page: Any,
        intent: str,
        action_type: str,
        items: list[dict],
        excl: set[str],
        chain: ResolveChain,
    ) -> Optional[dict]:
        """L3: V3 IntentRuleEngine 确定性规则."""
        if not items:
            return None
        sel = self.rule_engine.resolve(page, intent, action_type, items)
        if not sel:
            chain.add("L3规则", "未命中", note="intent_rule_engine")
            return None
        info = info_from_recommended_selector(sel)
        if info_key(info) in excl or not validate_selector(page, info):
            chain.add(
                "L3规则", "未命中",
                note=self.rule_engine.last_matched_rule() or "intent_rule_engine",
            )
            return None
        rule_name = self.rule_engine.last_matched_rule() or "intent_rule_engine"
        chain.add("L3规则", "命中", info_key(info), note=rule_name)
        return info

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
            hit_note=chain.hit_note,
        )

    def _detect_component_library(self) -> str:
        """扫描最近抽取的 DOM 识别组件库 (element-ui / ant-design 等)."""
        from .memory import _detect_component_library as _detect
        if self._framework_selectors:
            known = {
                "el-select": "element-ui", "elx-select": "element-plus",
                "ant-select": "ant-design", "van-field": "vant",
            }
            for key, lib in known.items():
                if key in str(self._framework_selectors).lower():
                    return lib
        return "generic"

    # ---------- 回填 ----------
    def _backfill(
        self,
        url: str,
        action_type: str,
        intent: str,
        info: dict,
        *,
        semantic_items: Optional[list[dict]] = None,
    ) -> None:
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
        wrote_l4 = bool(self.learner)
        l2_score: Optional[int] = None
        if self.cache:
            self.cache.put(url, action_type, intent, info, node=info)
        if self.memory:
            comp_lib = self._detect_component_library()
            sel_type = infer_selector_type(
                info,
                source=str(info.get("_source") or ""),
                from_skill=bool(info.get("_from_skill")),
            )
            self.memory.record_success(
                url, action_type, intent, info,
                node=info, component_library=comp_lib,
                selector_type=sel_type,
            )
            self.memory.maybe_record_generic(
                intent, action_type, info,
                semantic_items=semantic_items,
                component_library=comp_lib,
            )
            k = self.memory._key(url, action_type, intent)
            entry = self.memory._store.get(k)
            if entry:
                l2_score = int(entry.get("success_count") or 0)
        if self.learner:
            comp_lib = self._detect_component_library()
            learn_kw: dict[str, Any] = {
                "semantic_items": semantic_items,
                "component_library": comp_lib,
            }
            try:
                self.learner.learn(
                    url, action_type, intent, info, **learn_kw,
                )
            except TypeError:
                self.learner.learn(url, action_type, intent, info)
        self._emit_backfill(
            url, action_type, intent, info,
            wrote_l1=wrote_l1, wrote_l2=wrote_l2, wrote_l4=wrote_l4, l2_score=l2_score,
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
        wrote_l4: bool = False,
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
            "l4": wrote_l4,
            "l2_score": l2_score,
        }
        if self._trace is not None:
            self._trace.emit("locate_backfill", **payload)
        else:
            self._print_backfill(payload)

    def _print_backfill(self, data: dict[str, Any]) -> None:
        if data.get("skipped"):
            self.console.print(
                f"[dim]  │   L5回填: 跳过 ({data.get('reason')}) "
                f"selector={data.get('selector')!r}[/dim]"
            )
            return
        parts = []
        if data.get("l1"):
            parts.append("L1缓存")
        if data.get("l2"):
            score = data.get("l2_score")
            parts.append(f"L2记忆(score={score})" if score is not None else "L2记忆")
        if data.get("l4"):
            parts.append("L4学习")
        targets = "+".join(parts) if parts else "无"
        self.console.print(
            f"[cyan]  │   L5回填 → {targets}[/cyan] "
            f"[dim]key={data.get('cache_key')!r} selector={data.get('selector')!r}[/dim]"
        )

    def acceleration_stats(self) -> dict[str, Any]:
        """批次级五级加速层命中率汇总 (对齐 V3 observability.get_hit_rate_summary)."""
        return summarize_locating_stats(
            cache=self.cache,
            memory=self.memory,
            rule_engine=self.rule_engine,
            learner=self.learner,
            decider=self.decider,
            resolve_hits=dict(self._resolve_hits) if self._resolve_hits else None,
            scope="batch",
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
        if self.learner:
            self.learner.record_failure(url, action_type, selector)

    @staticmethod
    def _tag(info: dict, source: str) -> dict:
        out = dict(info)
        out["_source"] = source
        return out

    def _tag_and_track(self, info: dict, source: str) -> dict:
        self._resolve_hits[source] = self._resolve_hits.get(source, 0) + 1
        return self._tag(info, source)

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

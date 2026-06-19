"""步骤⑨ 元素定位五级降级链编排器.

顺序: L1缓存 → L2记忆 → L3规则引擎 → L4结构学习 → L5大模型. 逐级降级, 命中即返回.
- L1/L2 命中后校验可用; 失效则自愈, 自愈失败清除条目降级.
- L5 成功后回填 L1+L2+L4, 下次同页面不再需要大模型.
- 步骤⑬ 失败连锁清理: evict(缓存) + penalize(记忆) + record_failure(学习).
"""
from __future__ import annotations

from typing import Any, Optional

from rich.console import Console

from ..execution.trace import dom_console_print_enabled
from ..dom.semantic_dom import (
    build_locator_info,
    dom_index_from_items,
    extract_dom_index,
    extract_semantic_items,
)
from .cache import SelectorCache
from .llm_decider import LLMElementDecider
from .memory import SelectorMemory
from .node_refiner import refine_node_index
from .normalize import skip_locator_persistence, validate_selector
from .playwright_api import info_key
from .resolve_trace import ResolveChain
from .rule_engine import RuleEngine
from .self_heal import heal
from .structure_learner import StructureLearner



class LocatorResolver:
    """把一个自然语言意图解析成可执行选择器的统一入口."""

    def __init__(
        self,
        decider: LLMElementDecider,
        cache: Optional[SelectorCache] = None,
        memory: Optional[SelectorMemory] = None,
        rule_engine: Optional[RuleEngine] = None,
        learner: Optional[StructureLearner] = None,
        console: Optional[Console] = None,
    ) -> None:
        self.decider = decider
        self.cache = cache
        self.memory = memory
        self.rule_engine = rule_engine
        self.learner = learner
        self.console = console or Console()
        self._trace: Optional[Any] = None
        self._framework_selectors: Optional[dict[str, str]] = None
        self.last_chain: Optional[ResolveChain] = None

    def set_trace(self, trace: Optional[Any]) -> None:
        """注入 ExecutionTrace, 用于打印五级定位链路."""
        self._trace = trace

    def set_framework_selectors(self, selectors: Optional[dict[str, str]]) -> None:
        """注入从 skill.md 加载的框架专属选择器."""
        self._framework_selectors = selectors

    def resolve(
        self,
        page: Any,
        intent: str,
        action_type: str,
        dom_limit: int = 80,
        exclude: Optional[list[str]] = None,
        hint: Optional[str] = None,
        action_value: str = "",
        semantic_items: Optional[list[dict]] = None,
        dom_source: str = "",
    ) -> Optional[dict]:
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
        if self.cache:
            info = self.cache.get(url, action_type, intent)
            if not info:
                chain.add("L1缓存", "未命中")
            elif info_key(info) in excl:
                chain.add("L1缓存", "跳过(已排除)", info_key(info))
            elif validate_selector(page, info):
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
            chain.add("L1缓存", "未启用")

        # L2 记忆库
        if self.memory:
            info = self.memory.get(url, action_type, intent)
            if not info:
                chain.add("L2记忆", "未命中")
            elif info_key(info) in excl:
                chain.add("L2记忆", "跳过(已排除)", info_key(info))
            elif validate_selector(page, info):
                chain.mark_hit("L2记忆", info_key(info))
                self._emit_chain(chain)
                return self._tag(info, "L2记忆")
            else:
                chain.add("L2记忆", "校验失败", info["selector"])
        else:
            chain.add("L2记忆", "未启用")

        # L3 规则引擎
        if self.rule_engine:
            info = self.rule_engine.resolve(
                page, intent, action_type, hint=hint, exclude=excl,
                framework_selectors=self._framework_selectors,
                semantic_items=semantic_items,
            )
            if not info:
                chain.add("L3规则", "未命中")
            elif info_key(info) in excl:
                chain.add("L3规则", "跳过(已排除)", info_key(info))
            else:
                self._backfill(url, action_type, intent, info)
                chain.mark_hit("L3规则", info_key(info))
                self._emit_chain(chain)
                return self._tag(info, "L3规则")
        else:
            chain.add("L3规则", "未启用")

        # L4 结构学习
        if self.learner:
            info = self.learner.resolve(page, url, action_type, intent)
            if not info:
                chain.add("L4学习", "未命中")
            elif info_key(info) in excl:
                chain.add("L4学习", "跳过(已排除)", info_key(info))
            else:
                chain.mark_hit("L4学习", info_key(info))
                self._emit_chain(chain)
                return self._tag(info, "L4学习")
        else:
            chain.add("L4学习", "未启用")

        # L5 大模型 — 优先复用已抽取的 semantic_items (V3 共用 DOM)
        if semantic_items:
            items = semantic_items[:dom_limit]
            dom = dom_index_from_items(items, limit=dom_limit)
            dom_note = f"DOM候选={len(dom)}个(共用)" + (", 有hint" if hint else "")
        else:
            items = extract_semantic_items(
                page, dialog_first=True, stable=True,
                selectors=self._framework_selectors, profile="locate",
            )[:dom_limit]
            dom = extract_dom_index(
                page, limit=dom_limit, dialog_first=True, stable=False,
                selectors=self._framework_selectors, items=items,
            )
            dom_note = f"DOM候选={len(dom)}个(实时)" + (", 有hint" if hint else "")
        chain.llm_called = True
        chain.add("L5大模型", "已调用", note=dom_note)
        if dom_console_print_enabled():
            if semantic_items:
                self.console.print(
                    f"  [dim]└─ DOM (共用, {len(dom)} items) — 见上方抓取输出[/dim]"
                )
                if dom_source:
                    self.console.print(f"  [dim]   ↳ 来源: {dom_source}[/dim]")
            else:
                label = "L5"
                self.console.print(f"  [dim]└─ DOM ({label}, {len(dom)} items):[/dim]")
                for line in dom.numbered_text.split("\n"):
                    self.console.print(f"  [dim]   {line}[/dim]")
        info, llm_index = self.decider.decide(dom, intent, action_type, exclude=list(excl), hint=hint)
        if info and llm_index is not None:
            refined_idx, skill_name = refine_node_index(
                items, llm_index, intent, action_type, action_value=action_value,
            )
            if skill_name and refined_idx != llm_index:
                chain.add("L5纠偏", "命中", f"{llm_index}->{refined_idx}", note=skill_name)
                llm_index = refined_idx
                info = build_locator_info(items[refined_idx])
                info = dict(info)
        if info:
            note = f"index={llm_index}" if llm_index is not None else ""
            chain.add("L5大模型", "命中", info["selector"], note)
            self._backfill(url, action_type, intent, info)
            chain.mark_hit("L5大模型", info["selector"])
            self._emit_chain(chain)
            return self._tag(info, "L5大模型")
        chain.add("L5大模型", "未命中", note="index=-1/排除/调用失败")
        self._emit_chain(chain)
        return None

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
            return
        # 一个成功定位结果会同时喂给短期缓存、长期记忆和结构学习.
        if self.cache:
            self.cache.put(url, action_type, intent, info)
        if self.memory:
            self.memory.record_success(url, action_type, intent, info)
        if self.learner:
            self.learner.learn(url, action_type, intent, info)

    # ---------- 步骤⑬ 失败连锁清理 ----------
    def evict(self, page: Any, intent: str, action_type: str, selector: Optional[str]) -> None:
        url = _url(page)
        # 失败后先清理确定不可靠的短期缓存和结构学习记录.
        if self.cache:
            self.cache.evict(url, action_type, intent)
        if self.learner:
            self.learner.record_failure(url, action_type, selector)

    def penalize(self, page: Any, intent: str, action_type: str, selector: Optional[str]) -> None:
        url = _url(page)
        # 长期记忆不立即删除, 通过扣分避免偶发失败造成经验丢失.
        if self.memory:
            self.memory.record_failure(url, action_type, intent, selector)

    @staticmethod
    def _tag(info: dict, source: str) -> dict:
        # 附加来源只用于日志/报告观测, 不影响执行层使用 selector.
        out = dict(info)
        out["_source"] = source
        return out


def _url(page: Any) -> str:
    """安全读取当前页面 URL, 避免页面关闭等异常打断定位链."""
    try:
        return page.url or ""
    except Exception:
        return ""

"""五级定位链可观测性汇总 (对齐 V3 observability.get_hit_rate_summary)."""
from __future__ import annotations

from typing import Any, Optional

MODULE_LABELS: dict[str, str] = {
    "selector_cache": "L1 缓存",
    "selector_memory": "L2 记忆",
    "intent_rule_engine": "L3 规则",
    "page_structure_learner": "L4 学习",
    "element_decider": "L5 大模型",
}

RESOLVE_LEVEL_LABELS: dict[str, str] = {
    "L1缓存": "L1 缓存",
    "L2记忆": "L2 记忆",
    "L3规则": "L3 规则",
    "L4学习": "L4 学习",
    "L5大模型": "L5 大模型",
}

# 内部 trace 别名 → 五级口径 (L1自愈/L2通用 均为 L1/L2 子策略)
_RESOLVE_LEVEL_ALIASES: dict[str, str] = {
    "L1自愈": "L1缓存",
    "L2通用": "L2记忆",
}


def normalize_resolve_hits(hits: dict[str, int]) -> dict[str, int]:
    """将内部 trace 别名归并为五级命中分布."""
    out: dict[str, int] = {}
    for level, count in hits.items():
        if count <= 0:
            continue
        canon = _RESOLVE_LEVEL_ALIASES.get(level, level)
        out[canon] = out.get(canon, 0) + count
    return out


def counter_delta(baseline: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    keys = set(baseline) | set(current)
    return {
        k: current.get(k, 0) - baseline.get(k, 0)
        for k in keys
        if current.get(k, 0) - baseline.get(k, 0) > 0
    }


def summarize_locating_stats(
    *,
    cache: Any = None,
    memory: Any = None,
    rule_engine: Any = None,
    learner: Any = None,
    decider: Any = None,
    resolve_hits: Optional[dict[str, int]] = None,
    decider_stats: Optional[dict[str, Any]] = None,
    scope: str = "batch",
) -> dict[str, Any]:
    """汇总各加速层模块 stats + 实际 resolve 命中分布."""
    summary: dict[str, Any] = {"scope": scope}
    for name, mod in (
        ("selector_cache", cache),
        ("selector_memory", memory),
        ("intent_rule_engine", rule_engine),
        ("page_structure_learner", learner),
    ):
        if mod is not None and hasattr(mod, "stats"):
            summary[name] = mod.stats

    if decider_stats is not None:
        summary["element_decider"] = decider_stats
    elif decider is not None and hasattr(decider, "stats"):
        summary["element_decider"] = decider.stats

    if resolve_hits:
        normalized = normalize_resolve_hits(resolve_hits)
        total_resolves = sum(normalized.values()) or 1
        non_llm = sum(
            v for k, v in normalized.items()
            if k not in ("L5大模型",)
        )
        summary["resolve_distribution"] = normalized
        summary["overall"] = {
            "total_resolves": sum(normalized.values()),
            "non_llm_resolves": non_llm,
            "non_llm_rate": round(non_llm / total_resolves * 100, 1),
            "llm_resolves": normalized.get("L5大模型", 0),
        }

    module_lookups = sum(
        s.get("lookups", 0) for k, s in summary.items()
        if k not in ("overall", "resolve_distribution", "scope") and isinstance(s, dict)
    )
    module_hits = sum(
        s.get("hits", 0) + s.get("exact_hits", 0) + s.get("similar_hits", 0)
        for k, s in summary.items()
        if k not in ("overall", "resolve_distribution", "scope") and isinstance(s, dict)
    )
    if module_lookups:
        overall = summary.setdefault("overall", {})
        overall["total_module_lookups"] = module_lookups
        overall["total_module_hits"] = module_hits
        overall["overall_module_hit_rate"] = round(module_hits / module_lookups * 100, 1)

    return summary

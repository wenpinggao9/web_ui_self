"""步骤③ 用例排序 —— 推断依赖边, 稳定拓扑排序.

依赖边来源:
  1. 用例显式 dependencies (引用其它 case_id)
  2. (可选) 大模型推断 [[i, j], ...] 表示 i 必须在 j 之前
稳定拓扑排序 (最小堆): 多起点时选索引最小的先出堆, 无约束时与原顺序一致.
检测到环 → 放弃排序, 保持原顺序.
"""
from __future__ import annotations

import heapq
import json
from typing import Optional

from ..llm import LLMAdapter, PromptLoader
from ..parser import ParsedCase

_DEFAULT_SYSTEM = """\
你分析多个测试用例之间的执行依赖. 给出依赖边列表 [[i, j], ...], [i, j] 表示用例 i 必须在用例 j 之前执行.
只在确有数据依赖时给边 (如"编辑地址"依赖"添加地址"先产生数据). 只输出 JSON: {"edges": [[i,j], ...]}."""

_DEFAULT_USER = """\
用例列表:
{{cases}}

请输出 {"edges": [[i,j], ...]} JSON。"""


def sort_cases(
    cases: list[ParsedCase],
    llm: Optional[LLMAdapter] = None,
    prompts: Optional[PromptLoader] = None,
    use_llm: bool = True,
) -> list[ParsedCase]:
    """合并显式依赖与可选 LLM 推断依赖, 返回稳定排序后的用例列表."""
    n = len(cases)
    if n <= 1:
        return cases

    # case_id 映射到原始索引, 依赖边统一用索引表达.
    id_to_idx = {c.case_id: i for i, c in enumerate(cases)}
    edges: set[tuple[int, int]] = set()

    # 1. 显式依赖
    for j, c in enumerate(cases):
        for dep in c.dependencies:
            i = id_to_idx.get(dep.strip())
            if i is not None and i != j:
                edges.add((i, j))

    # 2. 大模型推断
    if use_llm and llm is not None and prompts is not None:
        edges |= _llm_edges(cases, llm, prompts)

    return _stable_toposort(cases, edges)


def _stable_toposort(cases: list[ParsedCase], edges: set[tuple[int, int]]) -> list[ParsedCase]:
    """稳定拓扑排序: 多个可执行节点同时存在时保留原始相对顺序."""
    n = len(cases)
    indeg = [0] * n
    adj: list[list[int]] = [[] for _ in range(n)]
    for i, j in edges:
        adj[i].append(j)
        indeg[j] += 1

    # 最小堆确保同一层级中原始索引小的用例先执行.
    heap = [i for i in range(n) if indeg[i] == 0]
    heapq.heapify(heap)
    order: list[int] = []
    while heap:
        u = heapq.heappop(heap)
        order.append(u)
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                heapq.heappush(heap, v)

    if len(order) != n:  # 有环 → 保持原顺序
        return cases
    return [cases[i] for i in order]


def _llm_edges(cases: list[ParsedCase], llm: LLMAdapter, prompts: PromptLoader) -> set[tuple[int, int]]:
    """调用 LLM 推断用例间隐式依赖, 返回合法的索引边集合."""
    lines = []
    for i, c in enumerate(cases):
        # 只给前三步预览, 控制 prompt 长度同时保留依赖判断所需线索.
        steps_preview = " / ".join(c.steps[:3])
        lines.append(json.dumps({
            "index": i, "case_id": c.case_id, "module_path": c.module_path,
            "preconditions": c.preconditions, "dependencies": c.dependencies,
            "steps_preview": steps_preview,
        }, ensure_ascii=False))
    system = prompts.system("case_sort", _DEFAULT_SYSTEM)
    user = prompts.user("case_sort", _DEFAULT_USER, cases="\n".join(lines))
    try:
        data = llm.complete_json("case_sort", system, user).data
        raw = data.get("edges") if isinstance(data, dict) else None
        out: set[tuple[int, int]] = set()
        for e in raw or []:
            if isinstance(e, (list, tuple)) and len(e) == 2:
                i, j = int(e[0]), int(e[1])
                # 防御模型输出越界、自依赖等非法边.
                if 0 <= i < len(cases) and 0 <= j < len(cases) and i != j:
                    out.add((i, j))
        return out
    except Exception:
        # 推断失败不影响执行, 退回显式依赖/原顺序.
        return set()

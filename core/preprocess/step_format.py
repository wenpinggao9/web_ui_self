"""用例编排格式归一 —— 支持分离式与交错式两种步骤/断言结构.

格式 A (分离): 全部操作步骤 → 全部预期结果
格式 B (交错): 每步操作后立即跟对应预期 (解析为 execution_blocks)
"""
from __future__ import annotations

import re

from ..parser import ExecutionBlock, ParsedCase

_ARROW_RE = re.compile(r"\s*(?:->|→)\s*")


def build_execution_blocks(case: ParsedCase) -> list[ExecutionBlock]:
    """把 ParsedCase 归一成按块执行的序列 (API/前置展开步骤在最前)."""
    blocks: list[ExecutionBlock] = []

    if case.steps:
        blocks.append(ExecutionBlock(operations=list(case.steps), expectations=[]))

    if case.execution_blocks:
        blocks.extend(case.execution_blocks)
    elif case.expectations:
        blocks.append(ExecutionBlock(operations=[], expectations=list(case.expectations)))

    return blocks if blocks else [ExecutionBlock()]


def flatten_case_for_planning(case: ParsedCase) -> None:
    """将交错式 execution_blocks 展平为 steps + expectations（原地修改）。

    格式 A（分离式）的 case.steps/expectations 已完整，直接跳过。
    格式 B（交错式）的数据在 case.execution_blocks，需展平供 generate_actions 使用。
    """
    if not case.execution_blocks or case.expectations:
        return
    steps = list(case.steps)
    exps = list(case.expectations)
    for block in case.execution_blocks:
        steps.extend(block.operations)
        exps.extend(block.expectations)
    case.steps = steps
    case.expectations = exps


def is_interleaved_case(case: ParsedCase) -> bool:
    """是否为交错编排 (每步操作后紧跟断言)."""
    if case.execution_blocks:
        return True
    return any(_ARROW_RE.search(s) for s in case.steps)

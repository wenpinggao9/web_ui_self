"""用例编排格式归一 —— 支持分离式与交错式两种步骤/断言结构.

格式 A (分离): 全部操作步骤 → 全部预期结果
格式 B (交错): 每步操作后立即跟对应预期 (解析为 execution_blocks)
"""
from __future__ import annotations

import re
from typing import Any

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


def case_steps_and_expectations_for_display(case: ParsedCase) -> tuple[list[str], list[str]]:
    """展平交错式 execution_blocks 供 API/日志展示 (不修改 case 对象)."""
    steps = list(case.steps)
    expectations = list(case.expectations)
    if case.execution_blocks and not expectations:
        for block in case.execution_blocks:
            steps.extend(block.operations)
            expectations.extend(block.expectations)
    return steps, expectations


def build_case_origin_snapshot(case: ParsedCase) -> dict[str, Any]:
    """构建 origin_case —— 仅两种形态, 与 Markdown 编排一致.

    分离式: preconditions + steps + expectations
    交错式: preconditions + blocks[] (每块含 steps + expectations)
    """
    snapshot: dict[str, Any] = {
        "preconditions": list(case.preconditions),
    }

    if case.execution_blocks and not case.expectations:
        snapshot["format"] = "interleaved"
        snapshot["blocks"] = [
            {
                "steps": list(block.operations),
                "expectations": list(block.expectations),
            }
            for block in case.execution_blocks
        ]
    else:
        snapshot["steps"] = list(case.steps)
        snapshot["expectations"] = list(case.expectations)

    return snapshot


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


def merge_consecutive_ops_blocks(blocks: list[ExecutionBlock]) -> list[ExecutionBlock]:
    """合并连续无断言的操作块, 仅在含预期的块边界暂停."""
    if not blocks:
        return []
    merged: list[ExecutionBlock] = []
    pending_ops: list[str] = []
    for block in blocks:
        pending_ops.extend(block.operations)
        if block.expectations:
            merged.append(
                ExecutionBlock(
                    operations=list(pending_ops),
                    expectations=list(block.expectations),
                )
            )
            pending_ops = []
    if pending_ops:
        merged.append(ExecutionBlock(operations=pending_ops, expectations=[]))
    return merged


def prepare_execution_plan(case: ParsedCase) -> tuple[list[ExecutionBlock], bool]:
    """返回 (合并后的执行块, 是否按块交错执行).

    仅当 Markdown 解析出 execution_blocks 时启用按块模式;
    分离式用例仍走一次性规划 (generate_actions).
    """
    blocks = merge_consecutive_ops_blocks(build_execution_blocks(case))
    return blocks, bool(case.execution_blocks)

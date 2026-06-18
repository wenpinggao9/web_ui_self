"""从用例文本推断执行角色 (无业务配置表, 仅结构匹配)."""
from __future__ import annotations

import re
from typing import Optional

from .action_schema import PlannedAction
from ..parser.schema import ParsedCase


def infer_primary_role(
    case: ParsedCase,
    actions: list[PlannedAction],
    available_roles: Optional[list[str]] = None,
) -> Optional[str]:
    """推断用例主执行角色: case.role > 用例文本 > 动作 role (须与文本一致)."""
    roles = list(available_roles or [])
    role_set = set(roles)
    if case.role:
        return case.role

    text = _case_role_text(case)
    from_text = _infer_from_text(text, roles)
    if from_text:
        return from_text

    for a in actions:
        if a.role and a.role in role_set and _role_supported_by_text(text, a.role, roles):
            return a.role
    return None


def _case_role_text(case: ParsedCase) -> str:
    """验证点、前置、步骤均可能写明执行主体."""
    return " ".join(case.notes + case.preconditions + case.steps)


def _infer_from_text(text: str, roles: list[str]) -> Optional[str]:
    if not text or not roles:
        return None

    for r in sorted(roles, key=len, reverse=True):
        if r in text:
            return r

    for r in roles:
        m = re.fullmatch(r"teacher([A-Z])(?:_\w+)?", r)
        if m and f"老师{m.group(1)}" in text:
            return r

    return None


def _role_supported_by_text(text: str, role: str, roles: list[str]) -> bool:
    """动作上的 role 是否有用例文本依据 (防止 LLM 乱填)."""
    return _infer_from_text(text, roles) == role

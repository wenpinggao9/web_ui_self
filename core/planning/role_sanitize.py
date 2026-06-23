"""规划后 role 校验: 剥离无效/冗余 role, 避免交错分块误触发重新登录."""
from __future__ import annotations

from typing import Optional

from rich.console import Console

from .action_schema import PlannedAction


def sanitize_planned_roles(
    actions: list[PlannedAction],
    available_roles: Optional[list[str]] = None,
    *,
    primary_role: Optional[str] = None,
    console: Optional[Console] = None,
) -> list[PlannedAction]:
    """剥离不在配置中的 role; 与用例主角色相同则省略 (同用例续跑不切换会话)."""
    role_set = set(available_roles or [])
    c = console

    for action in actions:
        role = (action.role or "").strip()
        if not role:
            continue
        if role_set and role not in role_set:
            if c:
                c.print(
                    f"  [yellow]剥离无效 role={role!r} "
                    f"(不在 {sorted(role_set)}), intent={action.intent[:40]}[/yellow]"
                )
            action.role = None
            continue
        if primary_role and role == primary_role:
            if c:
                c.print(
                    f"  [dim]省略冗余 role={role!r} (与用例主角色相同)[/dim]"
                )
            action.role = None

    return actions

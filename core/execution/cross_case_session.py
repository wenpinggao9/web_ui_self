"""跨用例多角色浏览器会话 —— 切回已打开的角色窗口时同步页面."""
from __future__ import annotations

from typing import Any, Optional


def role_reentry_needs_refresh(
    *,
    role: str,
    last_active_role: Optional[str],
    cross_case_session: bool,
    role_already_has_context: bool,
) -> bool:
    """从其他角色的浏览器切回已缓存的角色上下文时, 应刷新页面."""
    return bool(
        cross_case_session
        and role_already_has_context
        and last_active_role
        and last_active_role != role
    )


def refresh_page_on_role_reentry(
    page: Any,
    *,
    role: str,
    last_active_role: Optional[str],
    cross_case_session: bool,
    role_already_has_context: bool,
    timeout_ms: int = 10000,
    console: Any = None,
) -> bool:
    """跨角色切回时 reload, 避免后台 tab 仍展示切走前的旧 DOM/数据."""
    if not role_reentry_needs_refresh(
        role=role,
        last_active_role=last_active_role,
        cross_case_session=cross_case_session,
        role_already_has_context=role_already_has_context,
    ):
        return False
    try:
        page.reload(timeout=timeout_ms, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        if console is not None:
            console.print(
                f"  [cyan]↻ 切回角色 {role} (上一步为 {last_active_role}), "
                f"刷新页面同步最新数据[/cyan]",
            )
        return True
    except Exception as exc:
        if console is not None:
            console.print(
                f"  [yellow]切回角色 {role} 刷新失败(继续): {exc}[/yellow]",
            )
        return False

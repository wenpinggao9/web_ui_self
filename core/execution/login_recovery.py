"""登录页兜底: 就绪检查前若仍在登录页则重新走角色登录."""
from __future__ import annotations

from typing import Any, Callable, Optional

from rich.console import Console


def recover_stuck_on_login_page(
    page: Any,
    role: Optional[str],
    page_switcher: Optional[Callable[[str], Any]],
    *,
    console: Optional[Console] = None,
) -> Optional[Any]:
    """若当前 URL 为登录页且已知 role, 通过 page_switcher 重新登录. 返回新 page 或 None."""
    if not role or not page_switcher:
        return None
    from ..session.login import url_hints_login

    try:
        url = page.url or ""
    except Exception:
        return None
    if not url_hints_login(url):
        return None

    c = console
    if c:
        c.print(f"  [yellow]检测到登录页, 以 role={role} 重新登录[/yellow]")
    try:
        new_page = page_switcher(role)
    except Exception as exc:
        if c:
            c.print(f"  [yellow]登录页恢复失败: {exc}[/yellow]")
        return None
    if not new_page:
        return None
    try:
        still_login = url_hints_login(new_page.url or "")
    except Exception:
        still_login = True
    if c and not still_login:
        c.print(f"  [green]登录页恢复成功 → {new_page.url}[/green]")
    elif c and still_login:
        c.print("  [yellow]登录页恢复后仍在登录路由[/yellow]")
    return new_page


def filter_redundant_login_goto(
    recovery: list[Any],
    page: Any,
) -> list[Any]:
    """已在登录页时过滤 LLM 生成的 goto 登录 recovery, 避免重复导航."""
    from ..session.login import url_hints_login

    try:
        on_login = url_hints_login(page.url or "")
    except Exception:
        on_login = False
    if not on_login:
        return recovery

    kept = []
    for rec in recovery:
        intent = (getattr(rec, "intent", None) or "").lower()
        value = (getattr(rec, "value", None) or "").lower()
        if getattr(rec, "type", "") == "goto" and (
            "login" in intent or "登录" in intent
            or "login" in value or "/user/login" in value
        ):
            continue
        kept.append(rec)
    return kept

"""Tab handoff 诊断日志 — 始终输出到控制台, 不依赖 trace.enabled."""
from __future__ import annotations

from typing import Any, Optional

from rich.console import Console

from .script_helpers import _context_from_any, _page_alive, _page_usable, _url_safe


def _tab_urls(ctx: Any) -> list[str]:
    if ctx is None:
        return []
    try:
        out: list[str] = []
        for p in ctx.pages:
            try:
                closed = p.is_closed()
            except Exception:
                closed = True
            url = _url_safe(p) if not closed else "<closed>"
            out.append(url[:100])
        return out
    except Exception:
        return []


def log_tab_handoff(
    event: str,
    *,
    console: Optional[Console] = None,
    trace: Any = None,
    **fields: Any,
) -> None:
    """记录 tab handoff 关键节点 (控制台始终打印 + 可选 trace 落盘)."""
    c = console or Console()
    parts: list[str] = []
    for key, val in fields.items():
        if val is None:
            continue
        text = str(val)
        if len(text) > 120:
            text = text[:117] + "..."
        parts.append(f"{key}={text!r}")
    suffix = " | ".join(parts[:10])
    c.print(f"  [cyan]↳ TabHandoff[/cyan] {event}" + (f" — {suffix}" if suffix else ""))
    if trace is not None:
        trace.emit("tab_handoff", event=event, **fields)


def log_tab_snapshot(
    label: str,
    *,
    active: Any = None,
    list_anchor: Any = None,
    console: Optional[Console] = None,
    trace: Any = None,
    **extra: Any,
) -> None:
    """打印当前 active / list_anchor / context 内全部 tab 快照."""
    ctx = _context_from_any(active, list_anchor)
    tabs = _tab_urls(ctx)
    log_tab_handoff(
        label,
        console=console,
        trace=trace,
        active_alive=_page_alive(active),
        active_usable=_page_usable(active, timeout_ms=200) if _page_alive(active) else False,
        active_url=_url_safe(active) if _page_alive(active) else "<closed>",
        list_alive=_page_alive(list_anchor) if list_anchor is not None else None,
        list_url=_url_safe(list_anchor) if _page_alive(list_anchor) else (
            "<closed>" if list_anchor is not None else None
        ),
        context_tab_count=len(tabs),
        context_tabs="; ".join(tabs) if tabs else "(none)",
        **extra,
    )

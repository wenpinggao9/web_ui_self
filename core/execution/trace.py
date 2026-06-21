"""执行链路追踪 —— 在控制台打印各节点详情, 并落盘 执行追踪.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from ..dom import compact_dom_lines

_print_dom_enabled: bool = False


def configure_dom_console_print(enabled: bool) -> None:
    """全局开关: 是否在控制台打印完整 semantic DOM."""
    global _print_dom_enabled
    _print_dom_enabled = bool(enabled)


def dom_console_print_enabled() -> bool:
    return _print_dom_enabled


def print_captured_dom(
    console: Optional[Console],
    items: list[dict],
    *,
    label: str,
    source: str = "",
) -> None:
    """将抓取到的 semantic_items 完整打印到控制台 ([索引] 格式)."""
    if not _print_dom_enabled or not console or not items:
        return
    console.print(f"  [dim]└─ DOM ({label}, {len(items)} items):[/dim]")
    if source:
        console.print(f"  [dim]   ↳ {source}[/dim]")
    for line in compact_dom_lines(items).split("\n"):
        if line:
            console.print(f"  [dim]   {line}[/dim]")


class ExecutionTrace:
    """收集并打印动作规划、定位、分发、后校验等关键节点信息."""

    def __init__(self, console: Optional[Console] = None, enabled: bool = False) -> None:
        self.console = console or Console()
        self.enabled = enabled
        self.events: list[dict[str, Any]] = []

    def emit(self, phase: str, **data: Any) -> None:
        entry = {"phase": phase, **data}
        self.events.append(entry)
        if not self.enabled:
            return
        self._print(phase, data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.events, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _print(self, phase: str, data: dict[str, Any]) -> None:
        c = self.console
        if phase == "precondition":
            c.print(f"[dim]  ├─ 前置展开[/dim] 新增 {data.get('added', 0)} 步")
            for i, s in enumerate(data.get("steps") or [], 1):
                c.print(f"[dim]  │   {i}. {s}[/dim]")
        elif phase == "action_plan":
            c.print(f"[dim]  ├─ 动作规划[/dim] 共 {data.get('count', 0)} 个动作")
            for i, a in enumerate(data.get("actions") or [], 1):
                c.print(self._fmt_action(i, a))
        elif phase == "step_begin":
            c.print(
                f"[dim]  ├─ 执行准备[/dim] step={data.get('step_no')} "
                f"type={data.get('type')} value={data.get('value')!r}"
            )
            if data.get("url"):
                c.print(f"[dim]  │   URL: {data['url']}[/dim]")
        elif phase == "locate_chain":
            c.print(
                f"[dim]  ├─ 三级定位链[/dim] [{data.get('action_type')}] {data.get('intent', '')[:60]}"
            )
            if data.get("hint"):
                c.print(f"[dim]  │   resolve_hint: {str(data.get('hint'))[:120]}[/dim]")
            if data.get("exclude"):
                c.print(f"[dim]  │   已排除: {data.get('exclude')}[/dim]")
            for step in data.get("steps") or []:
                sel = step.get("selector")
                sel_part = f" → {sel!r}" if sel else ""
                note = step.get("note") or ""
                note_part = f" ({note})" if note else ""
                c.print(
                    f"[dim]  │   {step.get('level')}: {step.get('status')}{sel_part}{note_part}[/dim]"
                )
            if data.get("hit_level"):
                c.print(
                    f"[dim]  │   ✓ 最终命中: {data.get('hit_level')} "
                    f"selector={data.get('hit_selector')!r}[/dim]"
                )
            elif data.get("llm_called"):
                c.print("[dim]  │   ✗ 三级均未命中 (含 L3 大模型)[/dim]")
            else:
                c.print("[dim]  │   ✗ 三级均未命中 (未调用 L3)[/dim]")
        elif phase == "locate_backfill":
            if data.get("skipped"):
                c.print(
                    f"[dim]  │   L3回填: 跳过 ({data.get('reason')}) "
                    f"selector={data.get('selector')!r}[/dim]"
                )
            else:
                parts = []
                if data.get("l1"):
                    parts.append("L1缓存")
                if data.get("l2"):
                    score = data.get("l2_score")
                    parts.append(
                        f"L2记忆(score={score})" if score is not None else "L2记忆"
                    )
                targets = "+".join(parts) if parts else "无"
                c.print(
                    f"[cyan]  │   L3回填 → {targets}[/cyan] "
                    f"[dim]key={data.get('cache_key')!r} "
                    f"selector={data.get('selector')!r}[/dim]"
                )
        elif phase == "locate":
            src = data.get("source", "?")
            if src == "失败":
                src = "三级均未命中"
            c.print(
                f"[dim]  ├─ 元素定位结果[/dim] 来源={src} "
                f"selector={data.get('selector')!r}"
            )
            if data.get("nth") is not None:
                c.print(f"[dim]  │   nth={data.get('nth')}[/dim]")
            if data.get("target_html"):
                c.print(f"[dim]  │   目标元素: {data['target_html'][:160]}[/dim]")
            if data.get("hint"):
                c.print(f"[dim]  │   resolve_hint: {data['hint'][:120]}[/dim]")
        elif phase == "dispatch":
            ok = data.get("ok")
            mark = "✔" if ok else "✘"
            color = "green" if ok else "red"
            t = data.get("type", "")
            label = "API" if t == "api_call" else "Playwright"
            c.print(f"[{color}]  ├─ {label} 分发 {mark}[/{color}] {data.get('message', '')[:200]}")
        elif phase == "retry_plan":
            c.print(
                f"[dim]  ├─ 重试策略 LLM[/dim] focus={data.get('retry_focus')} "
                f"hint={str(data.get('resolve_hint') or '')[:100]}"
            )
        elif phase == "retry":
            c.print(
                f"[dim]  ├─ 后校验重试[/dim] 第{data.get('attempt')}次 "
                f"exclude={data.get('exclude')} force_selector={data.get('force_selector')!r}"
            )
            if data.get("retry_focus"):
                c.print(f"[dim]  │   hint: {str(data.get('retry_focus'))[:120]}[/dim]")
        elif phase == "post_check":
            ok = data.get("step_ok")
            color = "green" if ok else "yellow"
            c.print(
                f"[{color}]  ├─ 后校验 step_ok={ok} "
                f"retry_focus={data.get('retry_focus')} "
                f"reason={data.get('reason', '')[:120]}[/{color}]"
            )
        elif phase == "page_recover":
            reason = data.get("reason") or ""
            suffix = f" ({reason})" if reason else ""
            c.print(f"[dim]  ├─ Tab恢复 → {data.get('url', '')}{suffix}[/dim]")
        elif phase == "detail_submit_wait":
            c.print(
                f"[dim]  ├─ 提交后等待[/dim] outcome={data.get('outcome')} "
                f"url={data.get('url', '')}"
            )
        elif phase == "readiness":
            ready = data.get("ready")
            color = "green" if ready else "yellow"
            c.print(f"[{color}]  ├─ 就绪检查 ready={ready}[/{color}]")
            if data.get("note"):
                c.print(f"[dim]  │   {data['note']}[/dim]")
            for i, r in enumerate(data.get("recovery") or [], 1):
                c.print(f"[dim]  │   recovery {i}. [{r.get('type')}] {r.get('intent')}[/dim]")
        elif phase == "assert_live_read":
            c.print(f"[dim]  ├─ 断言实时读页 → {data.get('url', '')}[/dim]")
        elif phase == "page_state_capture":
            cnt = data.get("count", "")
            suffix = f" ({cnt} items)" if cnt else ""
            c.print(f"[dim]  ├─ 页面 DOM(共用){suffix} → {data.get('url', '')}[/dim]")
        elif phase == "assert_use_state":
            shared = "共用DOM" if data.get("shared") else ""
            c.print(
                f"[dim]  ├─ 断言复用页面状态 {shared}[/dim] (url={data.get('url', '')})"
            )
        else:
            c.print(f"[dim]  ├─ {phase}[/dim] {data}")

    @staticmethod
    def _fmt_action(i: int, a: Any, prefix: str = "  │   ") -> str:
        if isinstance(a, dict):
            t, intent, val = a.get("type"), a.get("intent"), a.get("value")
            split = a.get("intent_split")
        else:
            t, intent, val = a.type, a.intent, a.value
            split = getattr(a, "intent_split", False)
        extra = f" value={val!r}" if val else ""
        tag = " [拆分]" if split else ""
        return f"[dim]{prefix}{i}. [{t}]{tag} {intent}{extra}[/dim]"

    @staticmethod
    def summarize_actions(actions: list[Any]) -> list[dict[str, Any]]:
        out = []
        for a in actions:
            if isinstance(a, dict):
                out.append(a)
            else:
                out.append({
                    "type": a.type,
                    "intent": a.intent,
                    "value": a.value,
                    "negate": getattr(a, "negate", False),
                    "intent_split": getattr(a, "intent_split", False),
                })
        return out

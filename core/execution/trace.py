"""执行链路追踪 —— 在控制台打印各节点详情, 并落盘 执行追踪.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from rich.console import Console


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
        elif phase == "intent_split":
            c.print(
                f"[dim]  ├─ 意图拆分[/dim] {data.get('before', 0)} → {data.get('after', 0)} 个动作"
                + (f" (拆分 {data.get('split_count', 0)} 处)" if data.get("split_count") else "")
            )
            for d in data.get("details") or []:
                c.print(f"[dim]  │   · {d}[/dim]")
            for i, a in enumerate(data.get("actions") or [], 1):
                c.print(self._fmt_action(i, a, prefix="  │   "))
        elif phase == "step_begin":
            c.print(
                f"[dim]  ├─ 执行准备[/dim] step={data.get('step_no')} "
                f"type={data.get('type')} value={data.get('value')!r}"
            )
            if data.get("url"):
                c.print(f"[dim]  │   URL: {data['url']}[/dim]")
        elif phase == "locate_chain":
            c.print(
                f"[dim]  ├─ 五级定位链[/dim] [{data.get('action_type')}] {data.get('intent', '')[:60]}"
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
                c.print("[dim]  │   ✗ 五级均未命中 (含 L5 大模型)[/dim]")
            else:
                c.print("[dim]  │   ✗ 五级均未命中 (未调用 L5)[/dim]")
        elif phase == "locate":
            src = data.get("source", "?")
            if src == "失败":
                src = "五级均未命中"
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
        elif phase == "readiness":
            ready = data.get("ready")
            color = "green" if ready else "yellow"
            c.print(f"[{color}]  ├─ 就绪检查 ready={ready}[/{color}]")
            if data.get("note"):
                c.print(f"[dim]  │   {data['note']}[/dim]")
            for i, r in enumerate(data.get("recovery") or [], 1):
                c.print(f"[dim]  │   recovery {i}. [{r.get('type')}] {r.get('intent')}[/dim]")
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

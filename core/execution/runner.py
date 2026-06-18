"""步骤⑭ 执行编排器 PlaywrightRunner —— 逐动作执行主循环.

阶段A: 意图拆分在编排器(agent)预先完成; 这里逐动作分发, 记录结果, 失败截图,
保存 执行日志.json, 生成 HTML 报告.
阶段B: 接入 步骤⑩就绪检查 / 步骤⑫后校验 / 步骤⑬带重试 (开关位已预留).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from ..planning import PlannedAction
from ..report import render_report
from .dispatcher import ActionDispatcher
from .post_check import should_post_check
from .retry import RetryController
from .trace import ExecutionTrace


@dataclass
class ExecResult:
    """单步执行结果, 同时服务控制台输出、JSON 日志和 HTML 报告."""

    step_no: int
    raw_text: str          # 意图
    action: str            # 动作类型
    status: str            # PASS | FAIL
    duration_ms: int
    error: Optional[str] = None
    screenshot: Optional[str] = None
    message: Optional[str] = None
    selector: Optional[str] = None
    # report.py 兼容字段
    locator_repr: Optional[str] = None
    heal_attempts: int = 0
    code: Optional[str] = None
    resolved_html: Optional[str] = None
    post_check_ok: bool = True


class PlaywrightRunner:
    """按顺序执行动作列表, 串联就绪检查、重试、日志和报告输出."""

    def __init__(
        self,
        dispatcher: ActionDispatcher,
        out_dir: Path,
        console: Optional[Console] = None,
        screenshot_on_failure: bool = True,
        # 阶段B 组件 (None 则该能力关闭)
        readiness_checker: Optional[Any] = None,   # ReadinessChecker
        retry_controller: Optional[RetryController] = None,
        pre_readiness_check: bool = False,
        post_step_check: bool = False,
        # 阶段⑲ 可观测性 (可选)
        observability: Optional[Any] = None,       # ObservabilityCollector
        trace: Optional[ExecutionTrace] = None,
        # 【多角色】角色变化时切换浏览器会话, 签名: (role: str) -> page
        page_switcher: Optional[Any] = None,
        # 框架探测: skill.md 路径
        skill_path: Optional[str | Path] = None,
        # API 调用器 (用于 api_call 动作)
        api_runner: Optional[Any] = None,          # ApiRunner 实例
    ) -> None:
        self.dispatcher = dispatcher
        self.dispatcher.api_runner = api_runner    # 注入到 dispatcher
        self.out_dir = Path(out_dir)
        self.console = console or Console()
        self.shot_fail = screenshot_on_failure
        self.readiness_checker = readiness_checker
        self.retry_controller = retry_controller
        self.pre_readiness_check = pre_readiness_check and readiness_checker is not None
        self.post_step_check = post_step_check and retry_controller is not None
        self.observability = observability
        self.trace = trace
        self.page_switcher = page_switcher
        self.skill_path = Path(skill_path) if skill_path else None
        self.screens_dir = self.out_dir / "截图"
        self.screens_dir.mkdir(parents=True, exist_ok=True)

    def run_actions(self, actions: list[PlannedAction], case_id: str) -> list[ExecResult]:
        from ..readiness import is_advancing
        from ..skill_loader import get_framework_selectors

        results: list[ExecResult] = []
        seq = 0
        # 框架探测: 执行开始时识别页面使用的组件库, 加载对应选择器
        if self.skill_path:
            fw_sels = get_framework_selectors(self.skill_path, self.dispatcher.page)
            if fw_sels:
                self.console.print(f"  [cyan]框架识别: 检测到对应框架, 已注入选择器[/cyan]")
            else:
                self.console.print(f"  [dim]框架识别: 未匹配已知框架, 使用通用选择器[/dim]")
            # 注入到定位链
            resolver = getattr(self.dispatcher, 'resolver', None)
            if resolver:
                resolver.set_framework_selectors(fw_sels)

        results: list[ExecResult] = []
        seq = 0
        # 上一步后校验失败时, 下一步强制做就绪检查, 让页面有机会恢复到可操作状态.
        last_post_ok = True
        current_role: Optional[str] = None
        # 若首个动作已带 role, 预先切换, 避免 readiness/定位仍用上一用例的会话
        if actions and actions[0].role and self.page_switcher:
            new_page = self.page_switcher(actions[0].role)
            if new_page:
                self.dispatcher.set_page(new_page)
                current_role = actions[0].role

        # 按索引遍历, 就绪恢复动作插入到当前动作前, 保证最终 actions 列表包含完整步骤顺序
        idx = 0
        while idx < len(actions):
            or_bundle = self._collect_or_group(actions, idx)
            if or_bundle is not None:
                group, end_idx = or_bundle
                seq, last_post_ok = self._run_or_assert_group(
                    group, case_id, results, seq, last_post_ok,
                )
                idx = end_idx
                continue

            action = actions[idx]
            # 下一步意图, 传给后校验让 LLM 结合当前和下一步判断
            next_act = actions[idx + 1] if idx + 1 < len(actions) else None
            # 变量替换: 把 api_call 返回的 ${varName} 替换为实际值
            api_ctx = getattr(self.dispatcher, 'api_context', {})
            if api_ctx:
                if action.intent:
                    for k, v in api_ctx.items():
                        action.intent = action.intent.replace(f"${{{k}}}", str(v))
                if action.value:
                    for k, v in api_ctx.items():
                        action.value = action.value.replace(f"${{{k}}}", str(v))

            # 【多角色】角色变化时切换浏览器会话
            if action.role and action.role != current_role:
                if self.page_switcher:
                    new_page = self.page_switcher(action.role)
                    if new_page:
                        self.dispatcher.set_page(new_page)
                        self.console.print(f"  [cyan]↻ 切换角色 → {action.role}[/cyan]")
                        current_role = action.role

            # 步骤⑩ 就绪检查 (推进门控: 上一步成功时只在推进类动作做检查)
            if self.pre_readiness_check:
                force = is_advancing(action) if last_post_ok else True
                if force:
                    seq, idx = self._run_readiness_with_insert(action, case_id, results, seq, actions, idx)

            seq += 1
            idx += 1
            t0 = time.time()

            # 步骤⑲ 可观测性: 步骤边界
            if self.observability:
                self.observability.start_step(seq, action)

            self.console.print(f"[blue]▶ Step {seq:02d}[/blue] [{action.type}] {action.intent}")
            if self.trace:
                try:
                    url = self.dispatcher.page.url
                except Exception:
                    url = ""
                self.trace.emit(
                    "step_begin",
                    step_no=seq,
                    type=action.type,
                    intent=action.intent,
                    value=action.value,
                    url=url,
                )

            if self.post_step_check and should_post_check(action):
                # 开启后校验时, RetryController 内部负责执行、校验和必要重试.
                outcome = self.retry_controller.run(action, case_id, next_action=next_act)
                ok = outcome.ok and outcome.post_ok
                msg = outcome.message
                post_ok = outcome.post_ok
                # 重试耗尽仍失败: 就绪恢复 (如关弹窗) 后再次尝试本步骤
                if not ok and self.pre_readiness_check:
                    rec = self._recover_and_retry(action, case_id, results, seq, next_act)
                    if rec is not None:
                        ok, msg, post_ok, seq = rec
            else:
                # 未开启后校验或动作无需后校验时, 直接分发执行.
                ok, msg = self.dispatcher.dispatch(action, case_id=case_id)
                post_ok = ok
                # 断言失败自愈: 就绪恢复后重试一次
                if not ok and action.is_assert() and self.pre_readiness_check:
                    rdy = self.readiness_checker.check(self.dispatcher.page, action)
                    if not rdy.ready and rdy.recovery:
                        self.console.print(f"  [yellow]断言失败, 执行恢复后重试: {action.intent}[/yellow]")
                        self._execute_recovery_steps(rdy.recovery, case_id, results, seq)
                        ok, msg = self.dispatcher.dispatch(action, case_id=case_id)
                        post_ok = ok

            status = "PASS" if ok else "FAIL"

            # 关键步骤失败阻断: api_call 失败或断言中仍有未替换的变量, 后续步骤无意义, 直接终止
            if status == "FAIL":
                if action.type == "api_call":
                    self.console.print(f"  [red]关键步骤 api_call 失败, 终止用例执行[/red]")
                    r = ExecResult(
                        step_no=seq, raw_text=action.intent, action=action.type, status="FAIL",
                        duration_ms=duration, error=msg, message=msg,
                    )
                    results.append(r)
                    self._save_exec_log(results)
                    self._render(case_id, results)
                    return results
                if action.is_assert() and ("${" in (action.intent or "") or "${" in (action.value or "")):
                    self.console.print(f"  [red]断言中存在未替换变量, 终止用例执行[/red]")
                    r = ExecResult(
                        step_no=seq, raw_text=action.intent, action=action.type, status="FAIL",
                        duration_ms=duration, error=msg, message=msg,
                    )
                    results.append(r)
                    self._save_exec_log(results)
                    self._render(case_id, results)
                    return results
            duration = int((time.time() - t0) * 1000)
            shot = self._screenshot(seq, status) if (not ok and self.shot_fail) else None
            # selector/resolved_html 等字段用于报告排查定位结果.
            r = ExecResult(
                step_no=seq, raw_text=action.intent, action=action.type, status=status,
                duration_ms=duration, error=None if ok else msg,
                screenshot=str(shot) if shot else None, message=msg,
                selector=action.selector, locator_repr=action.selector,
                resolved_html=msg if ok else None, post_check_ok=post_ok,
            )

            # 步骤⑲ 可观测性: 步骤结束
            if self.observability:
                self.observability.end_step(seq, r)
            self._log_result(r)
            results.append(r)
            last_post_ok = post_ok

        self._save_exec_log(results)
        self._render(case_id, results)
        return results

    def _run_readiness(self, action: PlannedAction, case_id: str, results: list[ExecResult], seq: int) -> int:
        """执行就绪检查; 未就绪则跑恢复动作并记录."""
        rdy = self.readiness_checker.check(self.dispatcher.page, action)
        if self.trace:
            self.trace.emit(
                "readiness",
                ready=rdy.ready,
                note=rdy.note,
                recovery=[
                    {"type": r.type, "intent": r.intent, "value": r.value}
                    for r in rdy.recovery
                ],
            )
        if rdy.ready:
            return seq
        if rdy.note:
            self.console.print(f"  [yellow]就绪检查: {rdy.note}[/yellow]")
        return self._execute_recovery_steps(rdy.recovery, case_id, results, seq)

    def _run_readiness_with_insert(
        self, action: PlannedAction, case_id: str, results: list[ExecResult],
        seq: int, actions: list[PlannedAction], idx: int,
    ) -> tuple[int, int]:
        """就绪检查 + 恢复动作插入 actions 列表, 返回 (新 seq, 新 idx)."""
        rdy = self.readiness_checker.check(self.dispatcher.page, action)
        if self.trace:
            self.trace.emit(
                "readiness",
                ready=rdy.ready,
                note=rdy.note,
                recovery=[
                    {"type": r.type, "intent": r.intent, "value": r.value}
                    for r in rdy.recovery
                ],
            )
        if rdy.ready:
            return seq, idx
        if rdy.note:
            self.console.print(f"  [yellow]就绪检查: {rdy.note}[/yellow]")
        # 把恢复动作插入到 actions 列表中当前动作前面, 保证 codegen 能包含它们
        for rec in rdy.recovery:
            actions.insert(idx, rec)
            idx += 1
        seq = self._execute_recovery_steps(rdy.recovery, case_id, results, seq)
        return seq, idx

    def _execute_recovery_steps(
        self, recovery: list[PlannedAction], case_id: str, results: list[ExecResult], seq: int,
    ) -> int:
        """执行恢复动作列表 (勾选弹窗、关对话框等)."""
        for rec in recovery:
            rec.is_recovery = True
            seq += 1
            t0 = time.time()
            self.console.print(f"  [magenta]↺ 恢复 Step {seq:02d}[/magenta] [{rec.type}] {rec.intent}")
            ok, msg = self.dispatcher.dispatch(rec, case_id=case_id)
            results.append(ExecResult(
                step_no=seq, raw_text=f"[恢复] {rec.intent}", action=rec.type,
                status="PASS" if ok else "FAIL", duration_ms=int((time.time() - t0) * 1000),
                error=None if ok else msg, message=msg, selector=rec.selector,
                locator_repr=rec.selector,
            ))
        return seq

    def _recover_and_retry(
        self,
        action: PlannedAction,
        case_id: str,
        results: list[ExecResult],
        seq: int,
        next_action: Optional[PlannedAction] = None,
    ) -> Optional[tuple[bool, str, bool, int]]:
        """步骤失败后的恢复重试: 就绪检查 → 恢复动作 → 再次执行失败步骤."""
        rdy = self.readiness_checker.check(self.dispatcher.page, action)
        if self.trace:
            self.trace.emit(
                "failure_recovery",
                ready=rdy.ready,
                intent=action.intent,
                recovery=[
                    {"type": r.type, "intent": r.intent, "value": r.value}
                    for r in rdy.recovery
                ],
            )
        if rdy.ready or not rdy.recovery:
            return None
        self.console.print(
            f"  [yellow]步骤失败, 执行恢复后重试原动作: {action.intent}[/yellow]"
        )
        seq = self._execute_recovery_steps(rdy.recovery, case_id, results, seq)
        retry_action = action.model_copy(
            update={
                "force_selector": None,
                "selector": None,
                "exclude_selectors": [],
                "resolve_hint": None,
            }
        )
        if self.post_step_check:
            outcome = self.retry_controller.run(retry_action, case_id, next_action=next_action)
            if outcome.ok and outcome.post_ok:
                self.console.print(f"  [green]✚ 恢复后重试成功 (第{outcome.attempts}次)[/green]")
                # 回填定位结果供 codegen
                action.selector = retry_action.selector
                action.value = retry_action.value
                return True, outcome.message, True, seq
        else:
            ok, msg = self.dispatcher.dispatch(retry_action, case_id=case_id)
            if ok:
                action.selector = retry_action.selector
                return True, msg, True, seq
        return None

    @staticmethod
    def _collect_or_group(
        actions: list[PlannedAction], idx: int,
    ) -> Optional[tuple[list[PlannedAction], int]]:
        """连续同 or_group 的断言 → 任一分支通过即整组通过."""
        a = actions[idx]
        gid = (a.extras or {}).get("or_group") if a.is_assert() else None
        if not gid:
            return None
        end = idx
        while end < len(actions) and actions[end].is_assert():
            if (actions[end].extras or {}).get("or_group") != gid:
                break
            end += 1
        return actions[idx:end], end

    def _run_or_assert_group(
        self,
        group: list[PlannedAction],
        case_id: str,
        results: list[ExecResult],
        seq: int,
        last_post_ok: bool,
    ) -> tuple[int, bool]:
        """执行或断言组: 依次尝试, 首个通过即成功."""
        seq += 1
        t0 = time.time()
        intents = " | ".join(a.intent for a in group)
        self.console.print(
            f"[blue]▶ Step {seq:02d}[/blue] [assert_or] {intents[:120]}{'...' if len(intents) > 120 else ''}"
        )
        ok, msg = False, ""
        for branch in group:
            ok, msg = self.dispatcher.dispatch(branch, case_id=case_id)
            if ok:
                extras = dict(branch.extras or {})
                extras["or_winner"] = True
                branch.extras = extras
                msg = f"或断言通过(分支: {branch.intent}): {msg}"
                break
        if not ok:
            msg = f"或断言全部失败 ({len(group)} 个分支): {msg}"
        duration = int((time.time() - t0) * 1000)
        status = "PASS" if ok else "FAIL"
        r = ExecResult(
            step_no=seq,
            raw_text=intents,
            action="assert_or",
            status=status,
            duration_ms=duration,
            error=None if ok else msg,
            message=msg,
            post_check_ok=ok,
        )
        self._log_result(r)
        results.append(r)
        return seq, ok

    # ---------- 输出 ----------
    def _screenshot(self, step_no: int, status: str) -> Path:
        path = self.screens_dir / f"step_{step_no:03d}_{status.lower()}.png"
        try:
            # 失败截图不使用 full_page, 降低截图耗时并保留当前视口状态.
            self.dispatcher.page.screenshot(path=str(path), full_page=False)
        except Exception:
            pass
        return path

    def _log_result(self, r: ExecResult) -> None:
        color = {"PASS": "green", "FAIL": "red"}.get(r.status, "white")
        mark = {"PASS": "✔", "FAIL": "✘"}.get(r.status, "?")
        self.console.print(f"  [{color}]{mark} {r.status}[/{color}] ({r.duration_ms}ms) {r.message or ''}")
        if r.error:
            self.console.print(f"  [red]Error: {r.error}[/red]")

    def _save_exec_log(self, results: list[ExecResult]) -> None:
        # JSON 日志保留完整结构化字段, 比控制台文本更适合自动分析.
        data = [asdict(r) for r in results]
        (self.out_dir / "执行日志.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _render(self, case_id: str, results: list[ExecResult]) -> None:
        # HTML 报告生成失败不影响用例执行结果, 仅打印告警.
        total_ms = sum(r.duration_ms for r in results)
        report_dir = self.out_dir / "报告"
        try:
            render_report(case_id, "", total_ms, results, report_dir)
        except Exception as e:  # noqa: BLE001
            self.console.print(f"[yellow]报告生成失败: {e}[/yellow]")

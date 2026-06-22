"""步骤⑮ 核心编排器 UITestAgent —— 系统总指挥.

run_tests(测试文件):
  解析 → 排序 → (每条用例) 前置展开 → 登录 → 导航 → 动作规划
  → 执行编排器 → 判定 → 关闭浏览器 → 汇总.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from playwright.sync_api import sync_playwright

from . import codegen
from .business_loader import BusinessLoader
from .execution import ActionDispatcher, PlaywrightRunner
from .execution.script_helpers import (
    bring_page_to_front,
    find_list_tab_anchor,
    is_detail_submission_url,
    pick_role_handoff_page,
    recover_active_page,
    _page_usable,
    _url_safe,
)
from .execution.trace import ExecutionTrace
from .execution.post_check import PostStepChecker
from .execution.retry import RetryController
from .llm import LLMAdapter, PromptLoader
from .locating import (
    LLMElementDecider, LocatorResolver, SelectorCache, SelectorMemory,
    StructureLearner,
)
from .observability import ObservabilityCollector
from .output import FileManager
from .parser import parse_case
from .planning import ActionPlanner, strip_duplicate_menu_clicks
from .planning.page_nav import should_preserve_page_on_case_start
from .preprocess import PreconditionExpander, sort_cases
from .parser import ExecutionBlock
from .preprocess.step_format import (
    build_execution_blocks,
    flatten_case_for_planning,
    prepare_execution_plan,
)
from .profile import ProfileManager
from .readiness import ReadinessCaseContext, ReadinessChecker
from .resources import ResourceManager
from .session import Navigator, login
from .skill_loader import load_skill_text, format_skills_for_decider
from .variable_substitution import substitute_in_list, format_session_context
from .execution.session_ops import enrich_session_actions
from .watermark import load_watermark_config
from .report import build_report_data, save_batch_overview


class UITestAgent:
    def __init__(self, config: dict[str, Any], project_root: str | Path) -> None:
        self.config = config
        self.root = Path(project_root)
        self.console = Console()

        llm_cfg = config["llm"]
        # 步骤⑲ 可观测性: 当前用例的收集器, LLM 调用经 _route_llm 归集
        self._cur_obs: Optional[ObservabilityCollector] = None
        self.llm = LLMAdapter(llm_cfg, observe=self._route_llm)
        self.prompts = PromptLoader(self.root / "prompts", llm_cfg.get("prompts"))

        # 步骤㉒ 技能知识注入动作规划 + entrypoints 路径
        skill_path = self.root / "prompts" / "skill.md"
        from .locating.skill_invoke import configure_skill_path
        configure_skill_path(skill_path)
        skill_text = load_skill_text(skill_path)
        skill_decider_prompt = format_skills_for_decider(skill_path)
        self.precondition = PreconditionExpander(self.llm, self.prompts)
        self.planner = ActionPlanner(self.llm, self.prompts, skill_text=skill_text)
        self.navigator = Navigator(self.console)
        # 步骤⑳ 资源管理
        self.resources = ResourceManager(self.root)

        self.target = config.get("target", {})
        self.pw_cfg = config.get("playwright", {})
        self.runner_cfg = config.get("runner", {})
        self.watermark_cfg = load_watermark_config(self.runner_cfg)
        from .execution.trace import configure_dom_console_print
        configure_dom_console_print(self.runner_cfg.get("print_dom_to_console", False))

        # 【多系统扩展】Profile 管理器
        self.profile_mgr = ProfileManager(config)

        # 步骤⑨ 智能加速层: L1 纯内存缓存, L2 文件记忆 (带 TTL)
        accel = self.root / "智能加速"
        accel_cfg = config.get("acceleration", {})
        l1_ttl = int(accel_cfg.get("l1_ttl_minutes", 30)) * 60
        l2_ttl = int(accel_cfg.get("l2_ttl_days", 10)) * 24 * 3600
        self.cache = SelectorCache(ttl_s=l1_ttl)
        self.memory = SelectorMemory(accel / "选择器记忆库.json", ttl_s=l2_ttl)
        self.learner = StructureLearner(accel / "结构学习.json")
        self.decider = LLMElementDecider(
            self.llm, self.prompts,
            skill_prompt=skill_decider_prompt,
            skill_path=skill_path,
        )
        self.resolver = LocatorResolver(
            self.decider, cache=self.cache, memory=self.memory,
            console=self.console,
            dom_limit=int(config.get("locating", {}).get("dom_limit", 80)),
            intent_window=bool(config.get("locating", {}).get("intent_window", True)),
        )
        # 步骤⑩⑫ 可靠性组件
        self.readiness = ReadinessChecker(self.llm, self.prompts)
        self.post_checker = PostStepChecker(self.llm, self.prompts, console=self.console)

    def _route_llm(self, stage: str, system: str, user: str, raw: str) -> None:
        """步骤⑲: 把 LLM 调用归集到当前用例的可观测性收集器."""
        if self._cur_obs is not None:
            self._cur_obs.on_llm_call(stage, system, user, raw)

    def _inject_business(self, biz: BusinessLoader) -> None:
        """把业务目录的配置注入到 ProfileManager 中."""
        from .profile import SessionConfig

        base_url = biz.get_base_url()
        roles = biz.get_roles()
        if biz.project_dir:
            self.profile_mgr.sessions[biz.project_dir.name] = SessionConfig(
                name=biz.project_dir.name,
                target_system=biz.system_dir.name,
                roles=roles,
            )
        if base_url or biz.get_apis() or biz.get_enums():
            self.profile_mgr.profiles[biz.system_dir.name] = biz.build_system_profile()

    def run_tests(self, test_file: str | Path) -> dict[str, Any]:
        # 【业务目录】从用例路径向上自动发现业务配置
        biz = BusinessLoader()
        biz_loaded = biz.discover(test_file)
        if biz_loaded:
            self.console.print(f"[cyan]业务: {biz.system_dir.name} | 项目: {biz.project_dir.name}[/cyan]")
            # 注入业务知识到 ProfileManager
            self._inject_business(biz)

        cases = parse_case(test_file)
        if not cases:
            raise ValueError(f"用例文件未解析出任何用例: {test_file}")
        cases = sort_cases(cases, self.llm, self.prompts,
                           use_llm=self.runner_cfg.get("case_sort_llm", len(cases) > 1))

        # 让所有用例继承系统名 (用于兜底账号回退)
        if biz_loaded:
            for case in cases:
                if not case.target_system:
                    case.target_system = biz.system_dir.name

        import time as _time
        batch_start = _time.time()
        fm = FileManager(self.root)
        self.console.print(f"[cyan]批次目录: {fm.batch_dir}[/cyan]")

        case_results = []
        session_vars: dict[str, Any] = {}   # 跨用例共享变量池 (api_call 返回值等)
        cross_session = self.runner_cfg.get("cross_case_session", False)
        role_contexts: dict[str, tuple] = {} if cross_session else None  # 跨用例复用
        with sync_playwright() as p:
            browser_type = getattr(p, self.pw_cfg.get("browser", "chromium"))
            browser = browser_type.launch(headless=self.pw_cfg.get("headless", False))
            try:
                for case in cases:
                    case_results.append(self._run_one_case(
                        case, browser, fm, biz=biz, case_file=test_file,
                        session_vars=session_vars, role_contexts=role_contexts,
                    ))
                    self.memory.save()
                    self.learner.save()
            finally:
                if cross_session:
                    # 跨用例会话: 最终关闭残留上下文
                    for ctx, pg, *_ in role_contexts.values():
                        try:
                            pg.close()
                        except Exception:
                            pass
                        try:
                            ctx.close()
                        except Exception:
                            pass
                browser.close()

        # 持久化智能加速层 (L1 内存, L2记忆+L4学习落盘)
        self.memory.save()
        self.learner.save()

        passed = sum(1 for r in case_results if r["passed"])
        failed = len(case_results) - passed
        batch_ms = int((_time.time() - batch_start) * 1000)
        batch_duration = f"{batch_ms / 1000:.1f}秒" if batch_ms >= 1000 else f"{batch_ms}ms"
        try:
            ov_json, ov_html = save_batch_overview(
                fm.batch_dir,
                source_file=str(test_file),
                case_results=case_results,
                watermark_cfg=self.watermark_cfg,
                execution_time=batch_duration,
                batch_timestamp=fm.batch_dir.name,
            )
            self.console.print(f"[cyan]批次报告: {ov_html}[/cyan]")
        except Exception as e:  # noqa: BLE001
            self.console.print(f"[yellow]批次报告生成失败: {e}[/yellow]")
        suite_path = codegen.generate_suite_script(
            fm.batch_dir, [r["case_id"] for r in case_results], self.root,
        )
        if suite_path:
            self.console.print(f"[cyan]批次套件: {suite_path}[/cyan]")
        summary = {
            "总数": len(case_results),
            "通过数": passed,
            "失败数": failed,
            "批次目录": str(fm.batch_dir),
            "用例结果": case_results,
        }
        self.console.rule("[bold]汇总")
        self.console.print(f"总数 {summary['总数']}  通过 {passed}  失败 {failed}")
        return summary

    def _run_one_case(
        self, case, browser, fm: FileManager, biz: BusinessLoader = None, case_file: str | Path = "",
        session_vars: dict[str, Any] | None = None,
        role_contexts: dict[str, tuple] | None = None,
    ) -> dict[str, Any]:
        self.console.rule(f"[bold cyan]用例 {case.case_id}")

        # 步骤⑲ 每条用例独立的可观测性收集器
        obs = ObservabilityCollector()
        self._cur_obs = obs

        # 【多系统扩展】解析 profile + session
        profile, session = self.profile_mgr.resolve(case)
        self.console.print(f"[dim]系统: {profile.name} | 项目: {session.name or 'default'} | 角色: {case.role or '-'}[/dim]")

        # 登录页配置 (业务知识中的 login_page + 项目配置的 base_url)
        login_cfg = dict(base_url=biz.get_base_url() if biz else profile.base_url)
        if biz:
            login_cfg.update(biz.get_login_page())

        # 【前置条件分流】API 类前置 → 插入步骤文本供规划; runner 由 dispatcher 在 api_call 时懒加载
        if profile.apis and case.preconditions:
            api_keywords = []
            for tpl in profile.apis.values():
                api_keywords.extend(tpl.keywords)

            api_preconditions = []
            normal_preconditions = []
            for p in case.preconditions:
                if any(kw in p for kw in api_keywords):
                    api_preconditions.append(p)
                else:
                    normal_preconditions.append(p)

            if api_preconditions:
                case.steps = list(api_preconditions) + case.steps
                case.precondition_step_count = len(api_preconditions)

            case.preconditions = normal_preconditions

        # 【多系统扩展】变量替换: 步骤和预期中的 ${var}, 优先使用会话变量池
        if session_vars:
            case.steps = substitute_in_list(case.steps, session_vars)
            case.expectations = substitute_in_list(case.expectations, session_vars)
            for block in case.execution_blocks:
                block.operations[:] = substitute_in_list(block.operations, session_vars)
                block.expectations[:] = substitute_in_list(block.expectations, session_vars)

        # 步骤② 前置条件展开 (旧模式: LLM 文本展开, 仅在没有 API 前置时执行)
        trace = ExecutionTrace(
            self.console,
            enabled=self.runner_cfg.get("verbose_trace", True),
        )
        self.precondition.expand(case)
        trace.emit(
            "precondition",
            added=case.precondition_step_count,
            steps=case.steps,
        )
        fm.save_parsed_case(case.case_id, case)

        # 浏览器上下文 (按角色隔离, 支持用例内角色切换)
        # 跨用例会话模式: 复用上一层传入的 role_contexts
        default_timeout = self.pw_cfg.get("default_timeout_ms", 10000)

        if role_contexts is None:
            role_contexts: dict[str, Any] = {}  # role → (context, page, primary_page)

        def _get_page_for_role(role: str) -> Any:
            """获取或创建角色对应的浏览器页面并登录."""
            if role in role_contexts:
                ctx, pg, primary = role_contexts[role]
                pg = pick_role_handoff_page(ctx, pg, primary_page=primary)
                if not _page_usable(pg):
                    pg, _ = recover_active_page(pg, prefer=primary)
                anchor = find_list_tab_anchor(pg, primary if primary is not pg else None)
                if anchor is not None:
                    cur_u = _url_safe(pg) if _page_usable(pg) else ""
                    if not _page_usable(pg) or is_detail_submission_url(cur_u):
                        pg = anchor
                role_contexts[role] = (ctx, pg, primary)
                bring_page_to_front(pg)
                return pg
            ctx = browser.new_context(viewport=self.pw_cfg.get("viewport"))
            pg = ctx.new_page()
            pg.set_default_timeout(default_timeout)
            username, credential = self.profile_mgr.get_credentials(session, role)
            is_verify_code = credential.isdigit() and len(credential) <= 6
            login_kwargs = dict(login_cfg)
            login_kwargs["username"] = username
            self.console.print(f"  [dim]登录配置: base_url={login_kwargs.get('base_url')}, login_url={login_kwargs.get('login_url')}[/dim]")
            if is_verify_code:
                login(pg, login_kwargs, force=True, verify_code=credential)
            else:
                login_kwargs["password"] = credential
                login(pg, login_kwargs, force=True)
            self.console.print(f"[dim]登录完成 role={role} url={pg.url}[/dim]")
            role_contexts[role] = (ctx, pg, pg)
            bring_page_to_front(pg)
            return pg

        # 第一个角色作为主页面
        role_keys = list(session.roles.keys()) if session.roles else []
        first_role = role_keys[0] if role_keys else None
        if first_role:
            page = _get_page_for_role(first_role)
        else:
            context_browser = browser.new_context(viewport=self.pw_cfg.get("viewport"))
            page = context_browser.new_page()
            page.set_default_timeout(default_timeout)
            page = login(page, self.target, force=True)

        passed = False
        actions: list = []
        dispatcher = None
        runner = None
        primary_role: Optional[str] = None
        import time as _time
        case_start = _time.time()
        results: list = []
        try:
            # 【多系统扩展】步骤④ 登录 (带角色)
            # 有 session.roles 时已在 _get_page_for_role(first_role) 用业务 base_url 登录;
            # case.role 为空时切勿 fallback 到 config.yaml 的 self.target (会跳到商城).
            if session.roles:
                if case.role and case.role != first_role:
                    page = _get_page_for_role(case.role)
            elif first_role is None:
                # 无业务角色: 256-260 已用 self.target 登录
                pass

            # 步骤⑤ 导航 (auto_navigate=false 时跳过, 由用例步骤自行导航)
            if self.runner_cfg.get("auto_navigate", True):
                page = self.navigator.navigate(page, case.module_path,
                                               self.pw_cfg.get("default_timeout_ms", 10000))
            else:
                self.console.print("[dim]自动导航已关闭, 由用例步骤导航[/dim]")

            # 跨用例会话复用: 列表页刷新清筛选; 前置/步骤表明已在子页上下文时不 reload
            if self.runner_cfg.get("cross_case_session", False) and role_contexts:
                if not should_preserve_page_on_case_start(case.preconditions, case.steps):
                    try:
                        page.reload(
                            timeout=self.pw_cfg.get("default_timeout_ms", 10000),
                            wait_until="domcontentloaded",
                        )
                    except Exception as reload_err:  # noqa: BLE001
                        self.console.print(
                            f"  [yellow]跨用例列表页刷新失败(继续): {reload_err}[/yellow]"
                        )

            # 动作规划 (单次 LLM, 含拆分); 交错式按块, 分离式一次性
            roles = list(session.roles.keys()) if session.roles else None
            cross_session = self.runner_cfg.get("cross_case_session", False)
            exec_blocks, by_blocks = prepare_execution_plan(case)

            # 模式 2: 预规划动作注入, 跳过 LLM 规划
            preplanned = getattr(self, "_preplanned_actions", None)
            if preplanned:
                self.console.print(f"  [cyan]预规划模式: 使用 {len(preplanned)} 个预定义动作[/cyan]")
                actions = preplanned
                raw = ""
                flatten_case_for_planning(case)
                self._print_action_list(actions, "预规划动作")

                fm.save_planned_actions(case.case_id, actions)

                from .planning.role_infer import infer_primary_role
                primary_role = infer_primary_role(case, actions, role_keys)
                if primary_role:
                    case.role = primary_role
                    self.console.print(f"[dim]推断执行角色: {primary_role}[/dim]")
                    if session.roles and primary_role != first_role:
                        page = _get_page_for_role(primary_role)
                        self.console.print(f"  [cyan]↻ 切换至用例主角色 → {primary_role}[/cyan]")

                if not actions:
                    self.console.print("[yellow]预规划动作为空, 跳过执行[/yellow]")
                    return self._case_result_summary(case, [], False, case_start)

                self.resolver.set_trace(trace)
                dispatcher, runner = self._build_runner(
                    page=page,
                    trace=trace,
                    obs=obs,
                    biz=biz,
                    profile=profile,
                    session_vars=session_vars,
                    _get_page_for_role=_get_page_for_role if session.roles else None,
                    case=case,
                )
                results = runner.run_actions(actions, case.case_id)
                passed = bool(results) and all(r.status == "PASS" for r in results)
                page = dispatcher.page
                fm.save_planned_actions(case.case_id, actions)

            elif by_blocks:
                self.console.print(
                    f"  [dim]交错编排: {len(exec_blocks)} 个执行块 "
                    f"(原 {len(case.execution_blocks)} 段)[/dim]"
                )
                actions, raw, passed, primary_role, dispatcher, runner = self._plan_and_run_by_blocks(
                    case=case,
                    exec_blocks=exec_blocks,
                    page=page,
                    roles=roles,
                    cross_session=cross_session,
                    fm=fm,
                    trace=trace,
                    obs=obs,
                    biz=biz,
                    profile=profile,
                    session=session,
                    role_keys=role_keys,
                    first_role=first_role,
                    session_vars=session_vars,
                    _get_page_for_role=_get_page_for_role,
                )
                if dispatcher is not None:
                    page = dispatcher.page
            else:
                flatten_case_for_planning(case)
                session_ops_cfg = (biz.get_knowledge() if biz else {}).get("session_ops")
                ctx_summary = format_session_context(session_vars, session_ops_cfg)
                actions, raw = self.planner.generate_actions(
                    case, roles,
                    current_url=page.url,
                    cross_case_session=cross_session,
                    session_context=ctx_summary,
                )
                self._print_plan_raw(raw)
                self._print_action_list(actions, "规划完成")

                actions = strip_duplicate_menu_clicks(actions, case.module_path)
                actions = enrich_session_actions(actions, session_vars, session_ops_cfg)

                fm.save_raw_response(case.case_id, raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
                fm.save_prompt(case.case_id, self._dump_prompt(case, exec_blocks))
                fm.save_planned_actions(case.case_id, actions)

                from .planning.role_infer import infer_primary_role
                primary_role = infer_primary_role(case, actions, role_keys)
                if primary_role:
                    case.role = primary_role
                    self.console.print(f"[dim]推断执行角色: {primary_role}[/dim]")
                    if session.roles and primary_role != first_role:
                        page = _get_page_for_role(primary_role)
                        self.console.print(f"  [cyan]↻ 切换至用例主角色 → {primary_role}[/cyan]")

                if not actions:
                    self.console.print("[yellow]规划结果为空, 跳过执行[/yellow]")
                    return self._case_result_summary(case, [], False, case_start)

                self.resolver.set_trace(trace)
                dispatcher, runner = self._build_runner(
                    page=page,
                    trace=trace,
                    obs=obs,
                    biz=biz,
                    profile=profile,
                    session_vars=session_vars,
                    _get_page_for_role=_get_page_for_role if session.roles else None,
                    case=case,
                )
                results = runner.run_actions(actions, case.case_id)
                passed = bool(results) and all(r.status == "PASS" for r in results)
                page = dispatcher.page
                fm.save_planned_actions(case.case_id, actions)

            if dispatcher is not None and session_vars is not None:
                session_vars.update(dispatcher.api_context)
            if (
                dispatcher is not None
                and role_contexts is not None
                and self.runner_cfg.get("cross_case_session", False)
            ):
                sync_role = case.role or primary_role or first_role
                if sync_role and sync_role in role_contexts:
                    ctx, _, primary = role_contexts[sync_role]
                    list_anchor = getattr(dispatcher, "_list_tab_anchor", None)
                    handoff = pick_role_handoff_page(
                        ctx,
                        dispatcher.page,
                        list_anchor=list_anchor,
                        primary_page=primary,
                    )
                    if not _page_usable(handoff):
                        handoff, _ = recover_active_page(
                            handoff, prefer=list_anchor or primary,
                        )
                    role_contexts[sync_role] = (ctx, handoff, primary)
                    try:
                        sync_url = handoff.url or ""
                    except Exception:
                        sync_url = ""
                    self.console.print(f"  [dim]↳ 会话页同步 → {sync_url}[/dim]")
        except Exception as e:  # noqa: BLE001
            self.console.print("[red]用例执行异常:[/red]", str(e))
            passed = False
        finally:
            # 跨用例会话模式: 不关闭上下文, 留给下一个 case 复用
            # 独立模式: 关闭所有角色的浏览器上下文
            if role_contexts is not None and not self.runner_cfg.get("cross_case_session", False):
                for ctx, pg, *_ in role_contexts.values():
                    try:
                        pg.close()
                    except Exception:
                        pass
                    try:
                        ctx.close()
                    except Exception:
                        pass
            # 步骤⑲ 可观测性落盘 + 步骤⑳ 临时资源清理
            cd = fm.case_dir(case.case_id)
            obs.save(cd / "可观测性.json")
            trace.save(cd / "执行追踪.json")
            self._cur_obs = None
            self.resources.cleanup()

        # 步骤㉑ 生成可独立运行的 Playwright Python 脚本
        codegen_login = dict(login_cfg)
        login_role = case.role or (list(session.roles.keys())[0] if session.roles else None)
        if login_role and session.roles:
            uname, cred = self.profile_mgr.get_credentials(session, login_role)
            codegen_login["username"] = uname
            if cred.isdigit() and len(cred) <= 6:
                codegen_login["verify_code"] = cred
            else:
                codegen_login["password"] = cred
        api_ctx: dict = {}
        if dispatcher is not None:
            api_ctx = dict(dispatcher.api_context)
        if actions:
            popup_used = False
            popup_steps: list = []
            dismiss_before: list[str] = []
            idempotent_skip: list[str] = []
            if dispatcher is not None:
                popup_used = dispatcher.popup_dismiss_was_used()
                popup_steps = list(dispatcher.popup_recovery_steps)
                dismiss_before = list(dispatcher.popup_dismiss_before_intents)
                idempotent_skip = list(dispatcher.idempotent_skip_intents)
            codegen.generate_spec(
                case.case_id, actions,
                profile.base_url,
                out_dir=fm.case_dir(case.case_id),
                login_config=codegen_login,
                api_context=api_ctx,
                project_root=self.root,
                case_file=case_file,
                popup_dismiss_used=popup_used,
                popup_recoveries=popup_steps,
                popup_dismiss_before_intents=dismiss_before,
                idempotent_skip_intents=idempotent_skip,
            )

        return self._case_result_summary(
            case, results, passed, case_start,
        )

    def _case_result_summary(
        self, case, results: list, passed: bool, case_start: float,
    ) -> dict[str, Any]:
        import time as _time
        total_ms = int((_time.time() - case_start) * 1000)
        passed_steps = sum(1 for r in results if getattr(r, "status", None) == "PASS")
        failed_steps = sum(1 for r in results if getattr(r, "status", None) == "FAIL")
        total_steps = len(results)
        step_rate = f"{(passed_steps / total_steps * 100):.1f}%" if total_steps else "0%"
        exec_time = f"{total_ms / 1000:.1f}秒" if total_ms >= 1000 else f"{total_ms}ms"
        report_data = build_report_data(
            case.case_id, results, total_ms,
            feature_titles=list(case.module_path or []),
        ) if results else {"details": [], "total_steps": 0, "passed": 0, "failed": 0}
        return {
            "case_id": case.case_id,
            "passed": passed,
            "success": passed,
            "total_steps": total_steps,
            "passed_steps": passed_steps,
            "failed_steps": failed_steps,
            "step_success_rate": step_rate,
            "execution_time": exec_time,
            "details": report_data.get("details", []),
        }

    def _print_plan_raw(self, raw: Any) -> None:
        if raw:
            text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            self.console.print(f"  [dim]└─ 规划原始 JSON: {text}[/dim]")

    def _print_action_list(self, actions: list, title: str) -> None:
        self.console.print(f"  [dim]{title} {len(actions)} 个动作[/dim]")
        for i, a in enumerate(actions, 1):
            ex = getattr(a, "extras", None) or {}
            rk = f" row_key={ex['row_key']}" if ex.get("row_key") else ""
            intent = a.intent[:50] + ("..." if len(a.intent) > 50 else "")
            self.console.print(
                f"    [dim]{i:2d}. {a.type:12s} {intent}{rk}[/dim]"
            )

    def _make_readiness_case_context(
        self,
        case,
        biz: BusinessLoader | None,
    ) -> ReadinessCaseContext:
        hints: list[str] = []
        return ReadinessCaseContext(
            notes=list(case.notes),
            steps=list(case.steps),
            preconditions=list(case.preconditions),
            business_hints=hints,
        )

    def _build_runner(
        self,
        *,
        page,
        trace: ExecutionTrace,
        obs: ObservabilityCollector,
        biz: BusinessLoader | None,
        profile,
        session_vars: dict[str, Any] | None,
        _get_page_for_role,
        fm: FileManager | None = None,
        case_id: str = "",
        case: Any = None,
    ) -> tuple[ActionDispatcher, PlaywrightRunner]:
        self.resolver.set_trace(trace)
        knowledge = biz.get_knowledge() if biz else {}
        rctx = self._make_readiness_case_context(case, biz) if case else None
        dispatcher = ActionDispatcher(
            page, self.resolver,
            self.pw_cfg.get("default_timeout_ms", 10000),
            trace=trace,
            llm=self.llm,
            console=self.console,
            prompts=self.prompts,
            api_profile=profile if profile.apis else None,
            session_ops_cfg=knowledge.get("session_ops"),
            page_capture=knowledge.get("page_capture"),
        )
        if session_vars:
            dispatcher.api_context.update(session_vars)
        retry_ctrl = RetryController(
            dispatcher, self.post_checker, self.resolver, console=self.console,
            max_retries=self.runner_cfg.get("post_step_max_retries", 5),
            trace=trace,
            readiness_checker=self.readiness,
        )
        out_dir = fm.case_dir(case_id) if fm and case_id else self.root
        runner = PlaywrightRunner(
            dispatcher,
            out_dir=out_dir,
            console=self.console,
            screenshot_on_failure=self.runner_cfg.get("screenshot_on_failure", True),
            readiness_checker=self.readiness,
            retry_controller=retry_ctrl,
            pre_readiness_check=self.runner_cfg.get("pre_readiness_check", False),
            post_step_check=self.runner_cfg.get("post_step_check", False),
            observability=obs,
            trace=trace,
            page_switcher=_get_page_for_role,
            skill_path=self.root / "prompts" / "skill.md",
            readiness_case_context=rctx,
            watermark_cfg=self.watermark_cfg,
            feature_titles=list(case.module_path or []) if case else [],
        )
        return dispatcher, runner

    def _plan_and_run_by_blocks(
        self,
        *,
        case,
        exec_blocks: list[ExecutionBlock],
        page,
        roles,
        cross_session: bool,
        fm: FileManager,
        trace: ExecutionTrace,
        obs: ObservabilityCollector,
        biz: BusinessLoader | None,
        profile,
        session,
        role_keys: list[str],
        first_role: str | None,
        session_vars: dict[str, Any] | None,
        _get_page_for_role,
    ) -> tuple[list, list[Any], bool, Optional[str], ActionDispatcher | None, PlaywrightRunner | None]:
        from .planning.role_infer import infer_primary_role

        all_actions: list = []
        all_raws: list[Any] = []
        primary_role: Optional[str] = None
        dispatcher: ActionDispatcher | None = None
        runner: PlaywrightRunner | None = None
        passed = True
        total = len(exec_blocks)

        for block_no, block in enumerate(exec_blocks, start=1):
            if not block.operations and not block.expectations:
                continue

            ops_preview = "、".join(block.operations[:2])
            if len(block.operations) > 2:
                ops_preview += "…"
            exp_flag = f" + {len(block.expectations)} 条预期" if block.expectations else ""
            self.console.print(
                f"  [cyan]── 块 {block_no}/{total}[/cyan] "
                f"[dim]{ops_preview or '(仅断言)'}{exp_flag}[/dim]"
            )

            try:
                session_ops_cfg = (biz.get_knowledge() if biz else {}).get("session_ops")
                ctx_summary = format_session_context(session_vars, session_ops_cfg)
                block_actions, raws = self.planner.generate_block_actions(
                    case, block, roles,
                    block_no=block_no,
                    total_blocks=total,
                    current_url=page.url,
                    preconditions=case.preconditions,
                    cross_case_session=cross_session,
                    session_context=ctx_summary,
                )
            except Exception as e:  # noqa: BLE001
                self.console.print(f"  [red]块 {block_no} 规划失败: {e}[/red]")
                passed = False
                break
            all_raws.extend(raws)
            for raw in raws:
                self._print_plan_raw(raw)

            block_actions = strip_duplicate_menu_clicks(block_actions, case.module_path)
            block_actions = enrich_session_actions(block_actions, session_vars, session_ops_cfg)
            self._print_action_list(block_actions, f"块 {block_no} 规划")

            if primary_role is None and block_actions:
                primary_role = infer_primary_role(case, block_actions, role_keys)
                if primary_role:
                    case.role = primary_role
                    self.console.print(f"[dim]推断执行角色: {primary_role}[/dim]")
                    if session.roles and primary_role != first_role:
                        page = _get_page_for_role(primary_role)
                        self.console.print(f"  [cyan]↻ 切换至用例主角色 → {primary_role}[/cyan]")

            if dispatcher is None:
                dispatcher, runner = self._build_runner(
                    page=page,
                    trace=trace,
                    obs=obs,
                    biz=biz,
                    profile=profile,
                    session_vars=session_vars,
                    _get_page_for_role=_get_page_for_role if session.roles else None,
                    fm=fm,
                    case_id=case.case_id,
                    case=case,
                )

            all_actions.extend(block_actions)
            if not block_actions:
                continue

            results = runner.run_actions(block_actions, case.case_id)
            page = dispatcher.page
            if not results or not all(r.status == "PASS" for r in results):
                passed = False
                self.console.print(f"  [red]块 {block_no} 执行失败, 停止后续块[/red]")
                break

        fm.save_raw_response(
            case.case_id,
            json.dumps(all_raws, ensure_ascii=False) if all_raws else "",
        )
        fm.save_prompt(case.case_id, self._dump_prompt(case, exec_blocks))
        fm.save_planned_actions(case.case_id, all_actions)

        if not all_actions:
            self.console.print("[yellow]规划结果为空, 跳过执行[/yellow]")
            return [], all_raws, False, primary_role, dispatcher, runner

        return all_actions, all_raws, passed, primary_role, dispatcher, runner

    def _dump_prompt(self, case, exec_blocks=None) -> str:
        blocks = exec_blocks or build_execution_blocks(case)
        blocks_text = ""
        for i, b in enumerate(blocks, start=1):
            ops = "\n".join(f"  {j + 1}. {s}" for j, s in enumerate(b.operations)) or "  (无)"
            exps = "\n".join(f"  {j + 1}. {e}" for j, e in enumerate(b.expectations)) or "  (无)"
            blocks_text += f"\n### 块 {i}\n操作:\n{ops}\n预期:\n{exps}\n"
        return (
            f"# 用例 {case.case_id}\n\n"
            f"模块路径: {' / '.join(case.module_path)}\n"
            f"前置展开步骤数: {case.precondition_step_count}\n\n"
            f"## 系统提示词 (动作规划)\n\n```\n{self.prompts.system('action_plan')}\n```\n\n"
            f"## 执行块\n{blocks_text}\n"
        )

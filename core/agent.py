"""步骤⑮ 核心编排器 UITestAgent —— 系统总指挥.

run_tests(测试文件):
  解析 → 排序 → (每条用例) 前置展开 → 登录 → 导航 → 动作规划 → 意图拆分
  → 执行编排器 → 判定 → 关闭浏览器 → 汇总.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from playwright.sync_api import sync_playwright

from . import codegen
from .business_loader import BusinessLoader
from .execution import ActionDispatcher, PlaywrightRunner
from .execution.trace import ExecutionTrace
from .execution.post_check import PostStepChecker
from .execution.retry import RetryController
from .llm import LLMAdapter, PromptLoader
from .locating import (
    LLMElementDecider, LocatorResolver, RuleEngine, SelectorCache,
    SelectorMemory, StructureLearner,
)
from .observability import ObservabilityCollector
from .output import FileManager
from .parser import parse_case
from .planning import ActionPlanner, IntentSplitter, strip_duplicate_menu_clicks
from .planning.page_nav import should_preserve_page_on_case_start
from .preprocess import PreconditionExpander, sort_cases
from .preprocess.step_format import build_execution_blocks, flatten_case_for_planning
from .profile import ProfileManager
from .readiness import ReadinessChecker
from .resources import ResourceManager
from .session import Navigator, login
from .skill_loader import load_skill_text
from .variable_substitution import substitute_in_list


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

        # 步骤㉒ 技能知识注入动作规划
        skill_text = load_skill_text(self.root / "prompts" / "skill.md")
        self.precondition = PreconditionExpander(self.llm, self.prompts)
        self.planner = ActionPlanner(self.llm, self.prompts, skill_text=skill_text)
        self.splitter = IntentSplitter(self.llm, self.prompts)
        self.navigator = Navigator(self.console)
        # 步骤⑳ 资源管理
        self.resources = ResourceManager(self.root)

        self.target = config.get("target", {})
        self.pw_cfg = config.get("playwright", {})
        self.runner_cfg = config.get("runner", {})

        # 【多系统扩展】Profile 管理器
        self.profile_mgr = ProfileManager(config)

        # 步骤⑨ 智能加速层: L1 纯内存缓存, L2 文件记忆 (带 TTL)
        accel = self.root / "智能加速"
        accel_cfg = config.get("acceleration", {})
        l1_ttl = int(accel_cfg.get("l1_ttl_minutes", 30)) * 60
        l2_ttl = int(accel_cfg.get("l2_ttl_days", 10)) * 24 * 3600
        self.cache = SelectorCache(ttl_s=l1_ttl)
        self.memory = SelectorMemory(accel / "选择器记忆库.json", ttl_s=l2_ttl)
        self.learner = StructureLearner(accel / "页面结构学习.json")
        self.rule_engine = RuleEngine()
        self.decider = LLMElementDecider(self.llm, self.prompts)
        self.resolver = LocatorResolver(
            self.decider, cache=self.cache, memory=self.memory,
            rule_engine=self.rule_engine, learner=self.learner, console=self.console,
        )
        # 步骤⑩⑫ 可靠性组件
        self.readiness = ReadinessChecker(self.llm, self.prompts)
        self.post_checker = PostStepChecker(self.llm, self.prompts)

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
                    for ctx, pg in role_contexts.values():
                        try:
                            pg.close()
                        except Exception:
                            pass
                        try:
                            ctx.close()
                        except Exception:
                            pass
                browser.close()

        # 持久化智能加速层 (L1 纯内存, 仅 L2/L4 落盘)
        self.memory.save()
        self.learner.save()

        passed = sum(1 for r in case_results if r["passed"])
        failed = len(case_results) - passed
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
            role_contexts: dict[str, Any] = {}  # role → (context, page)

        def _get_page_for_role(role: str) -> Any:
            """获取或创建角色对应的浏览器页面并登录."""
            if role in role_contexts:
                return role_contexts[role][1]
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
            role_contexts[role] = (ctx, pg)
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

            # 步骤⑤ 导航
            page = self.navigator.navigate(page, case.module_path,
                                           self.pw_cfg.get("default_timeout_ms", 10000))

            # 跨用例会话复用: 列表页刷新清筛选; 前置/步骤表明已在子页上下文时不 reload
            if self.runner_cfg.get("cross_case_session", False) and role_contexts:
                if not should_preserve_page_on_case_start(case.preconditions, case.steps):
                    page.reload(timeout=self.pw_cfg.get("default_timeout_ms", 10000))

            # 步骤⑥ 动作规划 (一次性规划整个 case 的所有步骤和预期)
            roles = list(session.roles.keys()) if session.roles else None
            flatten_case_for_planning(case)
            actions, raw = self.planner.generate_actions(
                case, roles,
                current_url=page.url,
                cross_case_session=self.runner_cfg.get("cross_case_session", False),
            )

            # 打印原始 JSON (拆分前)
            if raw:
                self.console.print(
                    f"  [dim]└─ 规划原始 JSON: {raw}[/dim]"
                )

            # 打印规划结果 (拆分前)
            plan_count = len(actions)
            self.console.print(
                f"  [dim]规划完成 {plan_count} 个动作[/dim]"
            )
            for i, a in enumerate(actions, 1):
                self.console.print(f"    [dim]{i:2d}. {a.type:12s} {a.intent[:50]}{'...' if len(a.intent) > 50 else ''}[/dim]")

            # 步骤⑦ 意图拆分 (每 case 一次 LLM) + 重复菜单点击剥离
            before_split = len(actions)
            actions, split_notes, split_raw = self.splitter.split_all_with_raw(actions)
            actions = strip_duplicate_menu_clicks(actions, case.module_path)
            after_split = len(actions)

            if split_raw:
                self.console.print(f"  [dim]└─ 意图拆分原始 JSON: {split_raw}[/dim]")
                fm.save_intent_split_response(case.case_id, split_raw)
            if split_notes:
                for line in split_notes:
                    self.console.print(f"  [dim]{line}[/dim]")
            if after_split != before_split:
                self.console.print(
                    f"  [dim]意图拆分 {before_split} → {after_split} 个动作 (新增 {after_split - before_split})[/dim]"
                )
                for i, a in enumerate(actions, 1):
                    tag = "拆分" if getattr(a, "intent_split", False) else "保留"
                    self.console.print(
                        f"    [dim]{i:2d}. [{tag}] {a.type:12s} "
                        f"{a.intent[:50]}{'...' if len(a.intent) > 50 else ''}[/dim]"
                    )
            elif not split_notes:
                self.console.print(f"  [dim]意图拆分: 无需拆分[/dim]")
            fm.save_raw_response(case.case_id, raw)
            fm.save_prompt(case.case_id, self._dump_prompt(case))
            fm.save_planned_actions(case.case_id, actions)

            # 动作规划后再确定主角色, 避免一律用 roles 里第一个账号(常为 admin)登录
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
                return {"case_id": case.case_id, "passed": False, "reason": "无动作"}

            # 步骤⑨⑪⑫⑬⑭ 定位 + 分发 + 后校验重试 + 执行编排
            self.resolver.set_trace(trace)
            knowledge = biz.get_knowledge() if biz else {}
            dispatcher = ActionDispatcher(
                page, self.resolver,
                self.pw_cfg.get("default_timeout_ms", 10000),
                trace=trace,
                llm=self.llm,
                api_profile=profile if profile.apis else None,
                session_ops_cfg=knowledge.get("session_ops"),
                page_capture=knowledge.get("page_capture"),
            )
            # 会话变量注入到 dispatcher 上下文, 供执行期变量替换使用
            if session_vars:
                dispatcher.api_context.update(session_vars)
            retry_ctrl = RetryController(
                dispatcher, self.post_checker, self.resolver, console=self.console,
                max_retries=self.runner_cfg.get("post_step_max_retries", 5),
                trace=trace,
                readiness_checker=self.readiness,
            )
            runner = PlaywrightRunner(
                dispatcher,
                out_dir=fm.case_dir(case.case_id),
                console=self.console,
                screenshot_on_failure=self.runner_cfg.get("screenshot_on_failure", True),
                readiness_checker=self.readiness,
                retry_controller=retry_ctrl,
                pre_readiness_check=self.runner_cfg.get("pre_readiness_check", False),
                post_step_check=self.runner_cfg.get("post_step_check", False),
                observability=obs,
                trace=trace,
                page_switcher=_get_page_for_role if session.roles else None,
            )
            results = runner.run_actions(actions, case.case_id)
            passed = bool(results) and all(r.status == "PASS" for r in results)
            # 合并本用例产生的 api 返回值到会话变量池, 供后续用例使用
            if session_vars is not None:
                session_vars.update(dispatcher.api_context)
            # 跨用例: 写回当前标签页, 下一用例从详情/列表的实际停留页继续
            if role_contexts is not None and self.runner_cfg.get("cross_case_session", False):
                sync_role = case.role or primary_role or first_role
                if sync_role and sync_role in role_contexts:
                    ctx, _ = role_contexts[sync_role]
                    role_contexts[sync_role] = (ctx, dispatcher.page)
                    self.console.print(f"  [dim]↳ 会话页同步 → {dispatcher.page.url}[/dim]")
        except Exception as e:  # noqa: BLE001
            self.console.print(f"[red]用例执行异常: {e}[/red]")
        finally:
            # 跨用例会话模式: 不关闭上下文, 留给下一个 case 复用
            # 独立模式: 关闭所有角色的浏览器上下文
            if role_contexts is not None and not self.runner_cfg.get("cross_case_session", False):
                for ctx, pg in role_contexts.values():
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
        try:
            api_ctx = dict(dispatcher.api_context)
        except NameError:
            pass
        if actions:
            popup_used = False
            popup_steps: list = []
            dismiss_before: list[str] = []
            idempotent_skip: list[str] = []
            try:
                popup_used = dispatcher.popup_dismiss_was_used()
                popup_steps = list(dispatcher.popup_recovery_steps)
                dismiss_before = list(dispatcher.popup_dismiss_before_intents)
                idempotent_skip = list(dispatcher.idempotent_skip_intents)
            except NameError:
                pass
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

        return {"case_id": case.case_id, "passed": passed}

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

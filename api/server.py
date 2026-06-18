"""步骤⑯ REST API 服务 (FastAPI) —— 远程触发测试.

接口:
  模式 1 (全自动):
    POST /api/v1/ui-test/run            上传用例 → 规划+执行+报告
  模式 2 (人工校正):
    POST /api/v1/ui-test/plan           上传用例 → 返回规划动作 (人工校正)
    POST /api/v1/ui-test/run-preplanned 上传用例+校正后动作序列 → 执行+报告
  通用:
    GET  /api/v1/ui-test/status/{id}    查状态+进度
    GET  /api/v1/ui-test/result/{id}    获取结果
    GET  /api/v1/ui-test/report/{id}    下载ZIP报告
    GET  /api/v1/ui-test/tasks          任务列表
    POST /api/v1/ui-test/task/{id}/complete   代理完成回调
    POST /api/v1/agent/heartbeat        代理心跳
"""
from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .remote_agent_runner import AgentRegistry, dispatch_to_agent
from .server_runner import run_server_mode
from .task_manager import TaskManager, TaskStatus

# 动作规划评估所需
from core.llm import LLMAdapter, PromptLoader
from core.parser import parse_case
from core.planning import ActionPlanner, strip_duplicate_menu_clicks
from core.preprocess import PreconditionExpander
from core.preprocess.step_format import prepare_execution_plan
from core.skill_loader import load_skill_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

app = FastAPI(title="UI 自动化测试 API", version="3.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

task_manager = TaskManager()
agent_registry = AgentRegistry()
# 本服务对外可达基址 (供本地代理回调), 可用环境变量覆盖
SERVER_BASE = os.environ.get("SERVER_BASE", "http://127.0.0.1:8000")


def _load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


@app.get("/api/v1/ui-test/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/ui-test/plan")
async def plan_actions(
    file: UploadFile = File(..., description="用例文件 (.md)"),
    case_index: int = Form(0, description="用例索引 (从 0 开始), -1 表示全部"),
    case_path: str = Form("", description="用例原始路径, 用于加载业务知识/API, 如 业务/vip视频/大学增加前审/cases/xxx.md"),
) -> dict[str, Any]:
    """动作规划评估: 上传用例 → 返回规划动作列表 (不执行)."""

    content = await file.read()
    text = content.decode("utf-8")

    # 写临时文件供解析器使用
    fd, temp_path = tempfile.mkstemp(suffix=".md", prefix="plan_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)

    # 业务路径自动发现: 按文件名在 业务/ 目录下搜索匹配
    biz_path = case_path.strip() if case_path.strip() else ""
    if not biz_path:
        # 搜索 业务/**/<filename>
        for p in (PROJECT_ROOT / "业务").rglob(file.filename or ""):
            if p.is_file():
                biz_path = str(p)
                break
    if not biz_path:
        biz_path = str(PROJECT_ROOT / file.filename)

    try:
        config = _load_config()
        llm_cfg = config["llm"]
        llm = LLMAdapter(llm_cfg)
        prompts = PromptLoader(PROJECT_ROOT / "prompts", llm_cfg.get("prompts"))
        skill_text = load_skill_text(PROJECT_ROOT / "prompts" / "skill.md")

        cases = parse_case(temp_path)
        if not cases:
            raise HTTPException(400, "未解析出任何用例")

        if case_index == -1:
            # 返回全部用例
            results = []
            for case in cases:
                actions, origin, _ = _plan_one_case(case, llm, prompts, skill_text, biz_path)
                results.append({
                    "case_id": case.case_id,
                    "origin_case": origin,
                    "actions": [_dump_action(a) for a in actions],
                    "action_count": len(actions),
                })
            return {"cases": results, "total_cases": len(results)}
        else:
            if case_index < 0 or case_index >= len(cases):
                raise HTTPException(400, f"case_index={case_index} 超出范围 (0-{len(cases)-1})")
            case = cases[case_index]
            actions, origin, _ = _plan_one_case(case, llm, prompts, skill_text, biz_path)
            return {
                "case_id": case.case_id,
                "case_index": case_index,
                "origin_case": origin,
                "actions": [_dump_action(a) for a in actions],
                "action_count": len(actions),
            }
    finally:
        _safe_unlink(temp_path)


def _dump_action(a) -> dict:
    """输出精简动作: 去掉 negate=false、role 等字段 (role 在 case 级别输出)."""
    d = {
        "type": a.type,
        "intent": a.intent,
    }
    if a.value is not None:
        d["value"] = a.value
    if a.extras:
        d["extras"] = a.extras
    if a.negate:
        d["negate"] = True
    return d


def _plan_one_case(case, llm, prompts, skill_text, case_file_path: str) -> tuple[list, dict]:
    """规划单个用例的动作列表. 返回 (actions, origin_case)."""
    from core.business_loader import BusinessLoader
    from core.profile import ProfileManager, SessionConfig

    # 保存原始用例内容 (前置展开会修改 case.steps)
    origin = {
        "preconditions": list(case.preconditions) if case.preconditions else None,
        "steps": list(case.steps),
        "expectations": list(case.expectations),
    }
    if origin["preconditions"] is None:
        del origin["preconditions"]

    # 【业务目录】从用例路径向上自动发现业务配置 (与执行路径一致)
    biz = BusinessLoader()
    biz_loaded = biz.discover(case_file_path)

    # 加载 profile 获取 API 关键词
    profile_mgr = ProfileManager({})
    if biz_loaded:
        profile_mgr.sessions[biz.project_dir.name] = SessionConfig(
            name=biz.project_dir.name,
            target_system=biz.system_dir.name,
            roles={},
        )
        profile_mgr.profiles[biz.system_dir.name] = biz.build_system_profile()
    profile, session = profile_mgr.resolve(case)

    # 【前置条件分流】API 类前置 → 插入步骤文本供规划 (与执行路径一致)
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

    precondition = PreconditionExpander(llm, prompts)
    planner = ActionPlanner(llm, prompts, skill_text=skill_text)

    # 前置条件展开 (仅非 API 前置)
    precondition.expand(case)

    # 构建执行块
    exec_blocks, by_blocks = prepare_execution_plan(case)

    actions = []
    if by_blocks:
        for block in exec_blocks:
            block_actions, _ = planner.generate_block_actions(case, block)
            actions.extend(block_actions)
    else:
        actions, _ = planner.generate_actions(case)

    actions = strip_duplicate_menu_clicks(actions, case.module_path)

    return actions, origin


@app.post("/api/v1/ui-test/run-preplanned")
async def run_preplanned(
    file: UploadFile = File(..., description="用例文件 (.md), 用于获取 case_id/模块路径等元信息"),
    actions_json: str = Form(..., description="人工校正后的动作序列 JSON 数组"),
    config_override: str = Form("", description="JSON 字符串, 覆盖 config.yaml 字段"),
) -> dict[str, Any]:
    """模式 2: 接收人工校正后的动作序列 → 执行 → 报告."""
    from core.agent import UITestAgent
    from core.planning import PlannedAction

    content = await file.read()
    override: Optional[dict] = None
    if config_override.strip():
        try:
            override = json.loads(config_override)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"config_override 不是合法 JSON: {e}")

    # 解析动作序列
    try:
        raw_actions = json.loads(actions_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"actions_json 不是合法 JSON: {e}")

    if not isinstance(raw_actions, list):
        raise HTTPException(400, "actions_json 必须是 JSON 数组")

    # 转换为 PlannedAction 列表
    actions = []
    for item in raw_actions:
        if not isinstance(item, dict):
            continue
        try:
            a = PlannedAction(
                type=item.get("type", "click"),
                intent=item.get("intent", ""),
                value=item.get("value"),
                negate=bool(item.get("negate", False)),
                extras=item.get("extras", {}) or {},
                role=item.get("role"),
            )
            if a.intent:
                actions.append(a)
        except Exception:
            continue

    if not actions:
        raise HTTPException(400, "未解析出任何有效动作")

    # 写临时文件
    suffix = Path(file.filename or "case.md").suffix or ".md"
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="uitest_")
    with os.fdopen(fd, "wb") as f:
        f.write(content)

    task = task_manager.create(file_name=file.filename or "case.md", mode="preplanned", temp_path=temp_path)

    config = _load_config()

    def _job(t) -> dict[str, Any]:
        try:
            cfg = copy.deepcopy(config)
            if override:
                _deep_merge(cfg, override)
            cfg.setdefault("playwright", {})["headless"] = True
            # 用 UITestAgent 执行, 但传入预规划的动作
            agent = UITestAgent(cfg, project_root=PROJECT_ROOT)
            # 替换 agent 的 planner 为预规划动作注入器
            agent._preplanned_actions = actions
            result = agent.run_tests(temp_path)
            return result
        finally:
            _safe_unlink(temp_path)

    task_manager.run_async(task.id, _job)

    return {"task_id": task.id, "status": task_manager.get(task.id).status.value, "action_count": len(actions)}


@app.post("/api/v1/ui-test/run")
async def run_test(
    file: UploadFile = File(..., description="用例文件 (.md)"),
    mode: str = Form("server", description="server | local_ui"),
    config_override: str = Form("", description="JSON 字符串, 覆盖 config.yaml 字段"),
) -> dict[str, Any]:
    content = await file.read()
    override: Optional[dict] = None
    if config_override.strip():
        try:
            override = json.loads(config_override)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"config_override 不是合法 JSON: {e}")

    # 上传文件存临时目录
    suffix = Path(file.filename or "case.md").suffix or ".md"
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="uitest_")
    with os.fdopen(fd, "wb") as f:
        f.write(content)

    task = task_manager.create(file_name=file.filename or "case.md", mode=mode, temp_path=temp_path)

    if mode == "server":
        config = _load_config()

        def _job(t) -> dict[str, Any]:
            try:
                return run_server_mode(temp_path, PROJECT_ROOT, config, override)
            finally:
                _safe_unlink(temp_path)

        task_manager.run_async(task.id, _job)

    elif mode == "local_ui":
        agent = agent_registry.pick_idle()
        if agent is None:
            task_manager.update(task.id, status=TaskStatus.FAILED, error="无可用本地代理")
            _safe_unlink(temp_path)
            raise HTTPException(503, "无可用本地代理 (local_ui 模式需要先启动并注册本地代理)")
        callback = f"{SERVER_BASE}/api/v1/ui-test/task/{task.id}/complete"
        try:
            dispatch_to_agent(agent, task.id, task.file_name, content, callback, override)
            agent_registry.mark(agent.agent_id, "busy")
            task_manager.update(task.id, status=TaskStatus.RUNNING, progress=f"已下发给代理 {agent.agent_id}")
        except Exception as e:  # noqa: BLE001
            task_manager.update(task.id, status=TaskStatus.FAILED, error=f"下发代理失败: {e}")
            raise HTTPException(502, f"下发本地代理失败: {e}")
        finally:
            _safe_unlink(temp_path)
    else:
        _safe_unlink(temp_path)
        raise HTTPException(400, f"未知模式: {mode} (server | local_ui)")

    return {"task_id": task.id, "status": task_manager.get(task.id).status.value}


@app.get("/api/v1/ui-test/status/{task_id}")
def get_status(task_id: str) -> dict[str, Any]:
    t = task_manager.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t.to_public()


@app.get("/api/v1/ui-test/result/{task_id}")
def get_result(task_id: str) -> dict[str, Any]:
    t = task_manager.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {"task_id": t.id, "status": t.status.value, "result": t.result, "error": t.error}


@app.get("/api/v1/ui-test/report/{task_id}")
def download_report(task_id: str):
    t = task_manager.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if not t.batch_dir or not Path(t.batch_dir).exists():
        raise HTTPException(404, "报告尚未生成")
    buf = io.BytesIO()
    base = Path(t.batch_dir)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in base.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(base.parent))
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="report_{task_id}.zip"'},
    )


@app.get("/api/v1/ui-test/tasks")
def list_tasks() -> dict[str, Any]:
    return {"tasks": task_manager.list_all()}


@app.post("/api/v1/ui-test/task/{task_id}/complete")
async def task_complete(task_id: str, payload: dict[str, Any]) -> dict[str, str]:
    """本地代理执行完成后回调上报结果."""
    t = task_manager.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    ok = payload.get("success", True)
    result = payload.get("result")
    error = payload.get("error")
    agent_id = payload.get("agent_id")
    task_manager.update(
        task_id,
        status=TaskStatus.COMPLETED if ok else TaskStatus.FAILED,
        result=result, error=error, progress="完成" if ok else "失败",
        batch_dir=(result or {}).get("批次目录") if result else None,
    )
    if agent_id:
        agent_registry.mark(agent_id, "idle")
    return {"status": "received"}


@app.post("/api/v1/agent/heartbeat")
async def agent_heartbeat(payload: dict[str, Any]) -> dict[str, Any]:
    """本地代理每 30 秒上报心跳."""
    agent_id = payload.get("agent_id")
    url = payload.get("url")
    status = payload.get("status", "idle")
    if not agent_id or not url:
        raise HTTPException(400, "缺少 agent_id 或 url")
    agent_registry.upsert(agent_id, url, status)
    return {"status": "ok", "known_agents": len(agent_registry.list_all())}


@app.get("/api/v1/agent/list")
def list_agents() -> dict[str, Any]:
    return {"agents": agent_registry.list_all()}


def _safe_unlink(path: Optional[str]) -> None:
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v

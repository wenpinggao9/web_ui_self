"""步骤⑯ REST API 服务 (FastAPI) —— 远程触发测试.

接口:
  POST /api/v1/ui-test/run            上传文件+模式+配置覆盖 → 任务编号
  GET  /api/v1/ui-test/status/{id}    查状态+进度
  GET  /api/v1/ui-test/result/{id}    获取结果
  GET  /api/v1/ui-test/report/{id}    下载ZIP报告
  GET  /api/v1/ui-test/tasks          任务列表
  POST /api/v1/ui-test/task/{id}/complete   代理完成回调
  POST /api/v1/agent/heartbeat        代理心跳

执行路由:
  server     → 服务器运行器 (无头浏览器本地执行)
  local_ui   → 远程代理运行器 → 下发给本地有头代理 (步骤⑰)
"""
from __future__ import annotations

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

"""步骤⑰ 本地界面代理 (Flask) —— 有头浏览器调试.

接口:
  GET  /health      健康检查
  POST /run         接收服务器下发的任务 (base64 文件) → UITestAgent(headless=False) 异步执行 → 回调服务器

注册: 每 30 秒向服务器 POST /api/v1/agent/heartbeat 上报心跳.

启动: python -m api.local_agent  (环境变量配置)
  AGENT_ID       代理标识 (默认随机)
  AGENT_PORT     监听端口 (默认 8100)
  AGENT_URL      本代理对服务器可达的基址 (默认 http://127.0.0.1:8100)
  SERVER_URL     服务器基址 (默认 http://127.0.0.1:8000)
"""
from __future__ import annotations

import base64
import copy
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests
import yaml
from flask import Flask, jsonify, request

from core.agent import UITestAgent

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AGENT_ID = os.environ.get("AGENT_ID", "agent-" + uuid.uuid4().hex[:6])
AGENT_PORT = int(os.environ.get("AGENT_PORT", "8100"))
AGENT_URL = os.environ.get("AGENT_URL", f"http://127.0.0.1:{AGENT_PORT}")
SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:8000")

app = Flask(__name__)
_busy_lock = threading.Lock()
_busy = {"value": False}


def _load_config() -> dict[str, Any]:
    return yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))


@app.get("/health")
def health():
    return jsonify({"status": "ok", "agent_id": AGENT_ID, "busy": _busy["value"]})


@app.post("/run")
def run():
    data = request.get_json(force=True)
    task_id = data["task_id"]
    file_name = data.get("file_name", "case.md")
    file_bytes = base64.b64decode(data["file_b64"])
    callback_url = data["callback_url"]
    override = data.get("config_override") or {}

    suffix = Path(file_name).suffix or ".md"
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="agent_")
    with os.fdopen(fd, "wb") as f:
        f.write(file_bytes)

    with _busy_lock:
        _busy["value"] = True

    threading.Thread(target=_execute, args=(task_id, temp_path, override, callback_url), daemon=True).start()
    return jsonify({"status": "accepted", "task_id": task_id, "agent_id": AGENT_ID})


def _execute(task_id: str, temp_path: str, override: dict, callback_url: str) -> None:
    success, result, error = True, None, None
    try:
        cfg = copy.deepcopy(_load_config())
        _deep_merge(cfg, override)
        cfg.setdefault("playwright", {})["headless"] = False   # 有头, 用于调试
        agent = UITestAgent(cfg, project_root=PROJECT_ROOT)
        result = agent.run_tests(temp_path)
    except Exception as e:  # noqa: BLE001
        success, error = False, f"{type(e).__name__}: {e}"
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        with _busy_lock:
            _busy["value"] = False

    try:
        requests.post(callback_url, json={
            "success": success, "result": result, "error": error, "agent_id": AGENT_ID,
        }, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"[agent] 回调失败: {e}")


def _heartbeat_loop() -> None:
    while True:
        try:
            requests.post(f"{SERVER_URL}/api/v1/agent/heartbeat", json={
                "agent_id": AGENT_ID, "url": AGENT_URL,
                "status": "busy" if _busy["value"] else "idle",
            }, timeout=8)
        except Exception:
            pass
        time.sleep(30)


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def main() -> None:
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    print(f"[agent] {AGENT_ID} 监听 {AGENT_URL}, 服务器 {SERVER_URL}")
    app.run(host="0.0.0.0", port=AGENT_PORT)


if __name__ == "__main__":
    main()

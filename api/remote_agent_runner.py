"""步骤⑯/⑰ 远程代理运行器 + 代理注册表.

本地界面模式: 服务器把任务下发给已注册的"有头浏览器"本地代理 (步骤⑰),
代理执行完回调 /task/{id}/complete 上报结果.

代理注册表: 更新或插入代理 / 移除过期代理(90秒无心跳) / 标记空闲/忙碌 / 挑空闲代理.
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

_STALE_TIMEOUT_S = 90


@dataclass
class AgentInfo:
    agent_id: str
    url: str                       # 本地代理基址, 如 http://127.0.0.1:8100
    status: str = "idle"           # idle | busy
    last_heartbeat: float = field(default_factory=time.time)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentInfo] = {}
        self._lock = threading.Lock()

    def upsert(self, agent_id: str, url: str, status: str = "idle") -> None:
        """更新或插入代理 + 刷新心跳时间."""
        with self._lock:
            a = self._agents.get(agent_id)
            if a:
                a.url = url
                a.last_heartbeat = time.time()
                a.status = status
            else:
                self._agents[agent_id] = AgentInfo(agent_id=agent_id, url=url, status=status)

    def mark(self, agent_id: str, status: str) -> None:
        with self._lock:
            a = self._agents.get(agent_id)
            if a:
                a.status = status

    def remove_stale(self) -> None:
        now = time.time()
        with self._lock:
            for aid in [k for k, v in self._agents.items() if now - v.last_heartbeat > _STALE_TIMEOUT_S]:
                self._agents.pop(aid, None)

    def pick_idle(self) -> Optional[AgentInfo]:
        self.remove_stale()
        with self._lock:
            for a in self._agents.values():
                if a.status == "idle":
                    return a
        return None

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"agent_id": a.agent_id, "url": a.url, "status": a.status,
                 "last_heartbeat": a.last_heartbeat}
                for a in self._agents.values()
            ]


def dispatch_to_agent(
    agent: AgentInfo,
    task_id: str,
    file_name: str,
    file_bytes: bytes,
    callback_url: str,
    config_override: Optional[dict[str, Any]] = None,
    timeout_s: int = 15,
) -> None:
    """把任务(base64 文件)下发给本地代理. 代理异步执行后回调 callback_url."""
    payload = {
        "task_id": task_id,
        "file_name": file_name,
        "file_b64": base64.b64encode(file_bytes).decode("ascii"),
        "callback_url": callback_url,
        "config_override": config_override or {},
    }
    resp = requests.post(agent.url.rstrip("/") + "/run", json=payload, timeout=timeout_s)
    resp.raise_for_status()

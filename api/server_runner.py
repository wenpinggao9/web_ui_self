"""步骤⑯ 服务器运行器 —— 服务器模式: 无头浏览器本地执行.

创建 UITestAgent(headless=True) 同步跑完, 返回汇总.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

from core.agent import UITestAgent


def run_server_mode(
    task_file: str | Path,
    project_root: str | Path,
    config: dict[str, Any],
    config_override: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    cfg = copy.deepcopy(config)
    if config_override:
        _deep_merge(cfg, config_override)
    # 服务器模式强制无头
    cfg.setdefault("playwright", {})["headless"] = True
    agent = UITestAgent(cfg, project_root=project_root)
    return agent.run_tests(task_file)


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v

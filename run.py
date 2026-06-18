"""框架入口 (v3 重构).

单用例:  python run.py natural_cases/login_success.md
批量目录: python run.py natural_cases/

流程: 解析(Markdown) → 排序 → 前置展开 → 登录 → 导航 → 动作规划 → 意图拆分
      → 语义DOM → 五级定位(阶段A仅大模型) → 分发执行 → 报告.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from core.agent import UITestAgent


def load_config(path: Path) -> dict:
    """读取 YAML 配置文件, 供 Agent 初始化 LLM、浏览器和运行参数."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def discover_cases(path: Path) -> list[Path]:
    """支持传入单个 .md 用例文件, 或传入目录批量发现目录下的 .md 用例."""
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.glob("*.md") if p.is_file())
    raise FileNotFoundError(path)


def main() -> int:
    # 入口只负责参数解析、配置加载和用例调度, 具体执行逻辑交给 UITestAgent.
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="用例文件(.md)或目录")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    # 以 run.py 所在目录作为项目根目录, 避免从其他 cwd 启动时找不到配置/资源.
    root = Path(__file__).parent
    config = load_config(root / args.config)

    # target 可以是单个用例文件, 也可以是用例目录.
    cases = discover_cases(Path(args.target))
    if not cases:
        print(f"未发现 .md 用例: {args.target}", file=sys.stderr)
        return 2

    # Agent 内部负责登录、导航、动作规划、元素定位、执行和报告生成.
    agent = UITestAgent(config, project_root=root)

    # 批量执行时只要任意用例失败, 整体进程返回非 0, 便于 CI/脚本判断.
    any_failed = False
    for case_file in cases:
        summary = agent.run_tests(str(case_file))
        if summary["失败数"] > 0:
            any_failed = True

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())

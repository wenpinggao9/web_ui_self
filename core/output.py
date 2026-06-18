"""步骤㉓ 文件与输出管理.

目录结构:
  输出/UI测试/<时间戳>/<用例编号>/
    ├── 截图/
    ├── 语义DOM/
    ├── 报告/
    ├── 已解析用例.json
    ├── 使用的模型提示词.md
    ├── 模型原始响应.txt          (动作规划 LLM)
    ├── 模型原始响应.json
    ├── 意图拆分原始响应.txt      (意图拆分 LLM, 每 case 一次)
    ├── 意图拆分原始响应.json
    ├── 已规划动作.json
    ├── playwright_<用例编号>.py  (可独立运行的脚本)
    ├── 执行追踪.json   (verbose_trace 开启时)
    └── 执行日志.json   (由 runner 写)
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class FileManager:
    """管理一次测试批次的所有输出文件和用例子目录."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # 每次运行创建独立时间戳目录, 避免历史报告互相覆盖.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.batch_dir = self.root / "输出" / "UI测试" / ts
        self.batch_dir.mkdir(parents=True, exist_ok=True)

    def case_dir(self, case_id: str) -> Path:
        """返回用例输出目录, 并确保常用子目录已创建."""
        d = self.batch_dir / _safe(case_id)
        for sub in ("截图", "语义DOM", "报告"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d

    def save_parsed_case(self, case_id: str, case: Any) -> None:
        self._write_json(self.case_dir(case_id) / "已解析用例.json", _to_jsonable(case))

    def save_prompt(self, case_id: str, text: str) -> None:
        (self.case_dir(case_id) / "使用的模型提示词.md").write_text(text, encoding="utf-8")

    def save_raw_response(self, case_id: str, text: str) -> None:
        (self.case_dir(case_id) / "模型原始响应.txt").write_text(text, encoding="utf-8")
        self._try_save_json(text, self.case_dir(case_id) / "模型原始响应.json")

    def save_intent_split_response(self, case_id: str, text: str) -> None:
        if not text:
            return
        (self.case_dir(case_id) / "意图拆分原始响应.txt").write_text(text, encoding="utf-8")
        self._try_save_json(text, self.case_dir(case_id) / "意图拆分原始响应.json")

    def _try_save_json(self, text: str, path: Path) -> None:
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                import json as _json
                data = _json.loads(m.group(0))
                self._write_json(path, data)
            except Exception:
                pass

    def save_planned_actions(self, case_id: str, actions: list[Any]) -> None:
        # Pydantic 模型优先用 model_dump, 普通对象走通用 JSON 转换.
        data = [a.model_dump() if hasattr(a, "model_dump") else _to_jsonable(a) for a in actions]
        self._write_json(self.case_dir(case_id) / "已规划动作.json", data)

    def save_semantic_dom(self, case_id: str, step_no: int, text: str) -> None:
        (self.case_dir(case_id) / "语义DOM" / f"step_{step_no:03d}.txt").write_text(text, encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe(name: str) -> str:
    """把用例编号转换成可作为目录/文件名的安全字符串."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _to_jsonable(obj: Any) -> Any:
    """递归把 dataclass/Path 等对象转换成可 JSON 序列化的数据."""
    if is_dataclass(obj):
        # source_path 是本地调试信息, 不写入用例结构化输出.
        return {k: _to_jsonable(v) for k, v in asdict(obj).items() if k != "source_path"}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj

"""步骤⑯ UI测试任务管理器 —— 内存字典 + 线程锁存任务状态, 后台线程执行.

状态流转: submitted → running → completed / failed.
"""
from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional


class TaskStatus(str, Enum):
    SUBMITTED = "submitted"   # 已提交
    RUNNING = "running"       # 运行中
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 已失败


@dataclass
class Task:
    id: str
    file_name: str
    mode: str                            # server | local_ui
    status: TaskStatus = TaskStatus.SUBMITTED
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    progress: str = ""
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    batch_dir: Optional[str] = None
    temp_path: Optional[str] = None       # 上传文件临时路径, 结束后清理

    def to_public(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "file_name": self.file_name,
            "mode": self.mode,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress,
            "batch_dir": self.batch_dir,
        }


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def create(self, file_name: str, mode: str, temp_path: Optional[str] = None) -> Task:
        tid = uuid.uuid4().hex[:12]
        task = Task(id=tid, file_name=file_name, mode=mode, temp_path=temp_path)
        with self._lock:
            self._tasks[tid] = task
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.to_public() for t in self._tasks.values()]

    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            for k, v in fields.items():
                setattr(t, k, v)
            t.updated_at = datetime.now().isoformat(timespec="seconds")

    def run_async(self, task_id: str, fn: Callable[[Task], dict[str, Any]]) -> None:
        """后台线程执行 fn(task), 自动管理状态流转与异常."""
        def _runner() -> None:
            self.update(task_id, status=TaskStatus.RUNNING, progress="执行中")
            task = self.get(task_id)
            if task is None:
                return
            try:
                result = fn(task)
                self.update(
                    task_id, status=TaskStatus.COMPLETED, result=result,
                    progress="完成", batch_dir=result.get("批次目录"),
                )
            except Exception as e:  # noqa: BLE001
                self.update(
                    task_id, status=TaskStatus.FAILED,
                    error=f"{type(e).__name__}: {e}", progress="失败",
                )
                traceback.print_exc()

        threading.Thread(target=_runner, daemon=True).start()

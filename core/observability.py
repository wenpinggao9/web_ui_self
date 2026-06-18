"""步骤⑲ 可观测性 —— 出了问题能知道为什么.

ObservabilityCollector 用 hook 收集:
  开始步骤 / 结束步骤 → 步骤边界
  大模型调用 → 完整提示词和原始返回
  DOM快照 → DOM 数据
  记录截图 → 截图路径
失败归因: 选择器未找到 / 值不匹配 / 页面超时 / 元素不可见 → 修复建议.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class StepTrace:
    """单个步骤的观测数据快照."""

    step_no: int
    action_type: str
    intent: str
    status: Optional[str] = None
    message: Optional[str] = None
    selector: Optional[str] = None
    screenshot: Optional[str] = None
    dom_snapshot: Optional[str] = None
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    failure_attribution: Optional[dict[str, str]] = None


class ObservabilityCollector:
    """收集运行期诊断信息, 用于失败复盘和报告增强."""

    def __init__(self) -> None:
        self.steps: list[StepTrace] = []
        self._cur: Optional[StepTrace] = None
        # 未归属到具体步骤的 LLM 调用 (如动作规划/前置展开)
        self.global_llm_calls: list[dict[str, Any]] = []

    # ---------- 步骤边界 ----------
    def start_step(self, step_no: int, action: Any) -> None:
        """标记一个步骤开始, 后续 DOM/LLM/截图归属到该步骤."""
        self._cur = StepTrace(step_no=step_no, action_type=action.type, intent=action.intent)
        self.steps.append(self._cur)

    def end_step(self, step_no: int, result: Any) -> None:
        """标记一个步骤结束, 回填结果并在失败时做初步归因."""
        if self._cur is None:
            return
        self._cur.status = getattr(result, "status", None)
        self._cur.message = getattr(result, "message", None)
        self._cur.selector = getattr(result, "selector", None)
        self._cur.screenshot = getattr(result, "screenshot", None)
        if self._cur.status == "FAIL":
            self._cur.failure_attribution = attribute_failure(
                getattr(result, "error", None) or self._cur.message or ""
            )
        self._cur = None

    # ---------- LLM 调用 (adapter.observe 回调) ----------
    def on_llm_call(self, stage: str, system: str, user: str, raw: str) -> None:
        entry = {"stage": stage, "system": system, "user": user, "raw": raw}
        if self._cur is not None:
            self._cur.llm_calls.append(entry)
        else:
            # 动作规划/用例排序等发生在具体步骤之外, 归到全局调用列表.
            self.global_llm_calls.append(entry)

    # ---------- DOM 快照 ----------
    def on_dom_snapshot(self, text: str) -> None:
        if self._cur is not None:
            self._cur.dom_snapshot = text

    # ---------- 截图 ----------
    def on_screenshot(self, path: str) -> None:
        if self._cur is not None:
            self._cur.screenshot = path

    # ---------- 落盘 ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "global_llm_calls": self.global_llm_calls,
            "steps": [asdict(s) for s in self.steps],
        }

    def save(self, path: str | Path) -> None:
        """将观测数据写入 JSON 文件."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def attribute_failure(error: str) -> dict[str, str]:
    """失败归因: 把错误信息归类并给修复建议."""
    e = (error or "").lower()
    if "找不到元素" in error or "no element" in e or "not found" in e:
        return {"category": "选择器未找到", "suggestion": "检查意图描述是否准确, 或页面是否就绪(弹窗/表单未打开)"}
    if "timeout" in e or "超时" in error:
        return {"category": "页面超时", "suggestion": "增大超时或在该步前加等待; 确认元素是否异步加载"}
    if "not visible" in e or "不可见" in error or "intercepts pointer" in e:
        return {"category": "元素不可见", "suggestion": "元素被遮挡或隐藏, 检查是否有遮罩/未展开的面板"}
    if "值" in error or "value" in e or "格式" in error:
        return {"category": "值不匹配", "suggestion": "输入值不符合占位符/格式要求, 调整测试值"}
    return {"category": "未分类", "suggestion": "查看完整错误与该步 DOM 快照定位原因"}

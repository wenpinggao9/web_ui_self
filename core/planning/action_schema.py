"""规划动作结构 —— 规划器只产出"做什么", 不含选择器.

与旧 Action 的本质区别: 没有 locator 字段. 选择器在执行时由五级链动态解析,
这样页面改版后选择器变了、意图不变, 也能继续工作.

type 动作类型枚举 (英文标识符, 中文意图):
  click 点击 / hover 悬停 / fill 输入 / press 按键 / goto 跳转 / wait 等待 /
  upload 上传 / assert_text 断言文本 / assert_count 断言计数 / assert_table 断言表格 / asset 资产 /
  api_call API 调用 / bind_session 会话记录 / scroll 滚动
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

ACTION_TYPES = {
    "click", "hover", "fill", "press", "goto", "wait", "upload",
    "assert_text", "assert_count", "assert_table", "asset", "api_call", "scroll",
    "bind_session",
}


class PlannedAction(BaseModel):
    """规划阶段与执行阶段之间传递的动作对象."""

    type: str = Field(..., description="动作类型, 见 ACTION_TYPES")
    intent: str = Field(..., description="自然语言原子意图, 如 '点击保存地址按钮'")
    value: Optional[str] = Field(None, description="输入值/等待时长/断言期望值/上传字段等")
    negate: bool = Field(False, description="否定断言: 验证页面不存在 value")
    extras: dict[str, Any] = Field(default_factory=dict, description="上传字段名等附加参数")
    role: Optional[str] = Field(None, description="执行该动作的角色, 仅角色变化时才出现")

    # 运行期填充, 不由规划器产出
    selector: Optional[str] = Field(None, exclude=True)
    intent_split: bool = Field(False, exclude=True, description="是否由意图拆分得到")
    is_recovery: bool = Field(False, exclude=True, description="是否为就绪检查的恢复动作")
    # 步骤⑬ 重试用: 强制复用上次选择器 / 排除已试选择器 / 换元素提示
    force_selector: Optional[str] = Field(None, exclude=True)
    exclude_selectors: list[str] = Field(default_factory=list, exclude=True)
    resolve_hint: Optional[str] = Field(None, exclude=True)
    skip_acceleration: bool = Field(False, exclude=True, description="重试时跳过 L1/L2 缓存记忆")
    locator_info: Optional[dict] = Field(None, exclude=True, description="运行时定位信息(含 method)")

    def needs_locating(self) -> bool:
        """需要定位元素的动作类型."""
        return self.type in {"click", "hover", "fill", "press", "upload", "scroll"}

    def is_assert(self) -> bool:
        """断言动作不一定需要元素定位, 由执行层按类型分别处理."""
        return self.type in {"assert_text", "assert_count", "assert_table", "asset"}

    def clone_child(self, intent: str) -> "PlannedAction":
        """拆分出的子动作: 继承 value/extras, 清除 selector, 打拆分标记."""
        return PlannedAction(
            type=self.type,
            intent=intent,
            value=self.value,
            negate=self.negate,
            extras=dict(self.extras),
            role=self.role,
            intent_split=True,
        )


def coerce_action(raw: dict[str, Any]) -> Optional[PlannedAction]:
    """把 LLM 原始 dict 容错转换为 PlannedAction; 非法则返回 None."""
    if not isinstance(raw, dict):
        return None
    t = str(raw.get("type") or raw.get("类型") or "").strip()
    intent = str(raw.get("intent") or raw.get("意图") or "").strip()
    if not t or not intent:
        return None
    # 常见别名归一
    # LLM 可能使用通用英文动词或旧版动作名, 这里归一到框架支持的动作集合.
    alias = {
        "input": "fill", "type": "fill", "tap": "click", "mouseover": "hover",
        "assert": "assert_text", "assert_visible": "assert_text", "navigate": "goto",
        "key": "press", "sleep": "wait",
    }
    t = alias.get(t, t)
    if t not in ACTION_TYPES:
        return None
    value = raw.get("value")
    if value is None:
        value = raw.get("值")
    role = raw.get("role") or raw.get("角色")
    role = None if role is None else str(role).strip() or None
    return PlannedAction(
        type=t,
        intent=intent,
        value=None if value is None else str(value),
        negate=bool(raw.get("negate") or raw.get("否定")),
        extras={k: v for k, v in (raw.get("extras") or {}).items()} if isinstance(raw.get("extras"), dict) else {},
        role=role,
    )

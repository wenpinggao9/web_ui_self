"""变量替换工具 —— 将步骤/预期中的 ${varName} 替换为运行时值."""
from __future__ import annotations

from typing import Any


def substitute_variables(text: str, context: dict[str, Any]) -> str:
    """将 ${varName} 替换为 context 中的值.

    例: "工单ID=${orderId1}的生产状态" + {"orderId1": "118743302"}
       → "工单ID=118743302的生产状态"
    """
    if not context:
        return text
    for k, v in context.items():
        # 只替换 ${name} 这种显式占位符, 避免误替换普通业务文本.
        placeholder = "${" + k + "}"
        if placeholder in text:
            text = text.replace(placeholder, str(v))
    return text


def substitute_in_list(items: list[str], context: dict[str, Any]) -> list[str]:
    """批量替换列表中的字符串."""
    if not context:
        # 没有上下文时原样返回, 保持调用方列表对象不被无意义复制.
        return items
    return [substitute_variables(item, context) for item in items]

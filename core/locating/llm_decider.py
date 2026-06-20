"""步骤⑨ 第3级 元素决策解析器 —— 大模型全量解析 (对齐 V3 L5).

输入: 带编号的语义DOM + 意图 + 动作类型 → 大模型返回节点编号 → 映射成选择器.
最慢但最灵活. 成功后由 resolver 回填 L1 缓存与 L2 记忆.
"""
from __future__ import annotations

from typing import Optional

from ..dom import DomIndex
from ..llm import LLMAdapter, PromptLoader

_DEFAULT_SYSTEM = """\
你是"元素决策器". 给你一个带编号的页面元素列表和一个操作意图, 你要选出最匹配的元素编号.

规则:
- 输入类(fill): 只选可编辑 input/textarea, 排除只读和下拉容器.
- 点击类(click): 选按钮/链接/可点击项; 展开下拉 → combobox 触发器; 「在下拉选项中点击」→ 必须选 role=option/listbox 内选项, 禁止选 combobox input.
- 选项语义: intent 引号内可能是业务筛选概念, 选展开面板中语义最接近的可见 option (子串/简称/合理 UI 文案均可).
- 悬停类(hover): 选可触发悬停的容器; 展开菜单时优先 role=button + haspopup=menu.
- 上传(upload): 只选文件输入框.
- 综合看文本/placeholder/name/role/type; [弹窗]/[表单] 优先; 禁止编造编号.
只输出 JSON: {"index": 数字}. 找不到则 {"index": -1}."""

_DEFAULT_USER = """\
动作类型: {{action_type}}
操作意图: {{intent}}

页面元素 (编号从 0 开始):
{{dom}}

请输出 {"index": 数字} JSON."""


class LLMElementDecider:
    """定位链最后一级: 让 LLM 在语义 DOM 编号列表中选择目标元素."""

    def __init__(self, llm: LLMAdapter, prompts: PromptLoader) -> None:
        self.llm = llm
        self.prompts = prompts

    def decide(
        self,
        dom: DomIndex,
        intent: str,
        action_type: str,
        exclude: Optional[list[str]] = None,
        hint: Optional[str] = None,
    ) -> tuple[Optional[dict], Optional[int]]:
        """返回 (selector 信息, LLM 选中 index); 找不到返回 (None, None)."""
        if len(dom) == 0:
            return None, None
        # hint 只作为补充上下文注入意图, 不改变 action_type 或 DOM 输入.
        intent_text = intent if not hint else f"{intent}"
        hint_block = ""
        if hint:
            hint_block = (
                "\n\n【重试策略提示】上一步页面校验未通过, 请认真参考下列线索选择编号 "
                "(若与意图冲突以意图为准, 但线索中的选择器/index 优先):\n"
                f"{hint.strip()[:800]}"
            )
        system = self.prompts.system("element_decide", _DEFAULT_SYSTEM)
        user = self.prompts.user(
            "element_decide", _DEFAULT_USER,
            action_type=action_type, intent=intent_text + hint_block, dom=dom.numbered_text,
        )
        try:
            data = self.llm.complete_json("element_decide", system, user).data
        except Exception:
            return None, None
        idx = _as_int(data.get("index") if isinstance(data, dict) else None)
        if idx is None or idx < 0 or idx >= len(dom.selectors):
            return None, idx
        info = dom.selectors[idx]
        from .playwright_api import info_key
        if exclude and info_key(info) in set(exclude):
            return None, idx
        return info, idx


def _as_int(v) -> Optional[int]:
    """宽松转换模型返回的 index, 转换失败时视为无结果."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

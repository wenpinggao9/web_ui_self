"""步骤② 前置条件展开 —— 把"已有"变成"先做".

大模型做两件事:
1. 剔除仅与登录相关的句子 ("用户已登录"等), 由步骤④ 统一处理
2. 拆解其余为 1+ 条可执行操作步骤 (自然语言, 每条一步)

大模型失败 → 正则兜底: 去掉登录句子, 其余整段保留为一步.
展开后步骤插到 操作步骤列表最前面, 回填 precondition_step_count.
"""
from __future__ import annotations

import re
from typing import Optional

from ..llm import LLMAdapter, PromptLoader
from ..parser import ParsedCase

_LOGIN_PAT = re.compile(r"(已登录|登录系统|登入|完成登录|处于登录|登录态|已登陆)")

_DEFAULT_SYSTEM = """\
你把测试用例的"前置条件"转成"为满足该前置需要先执行的操作步骤".
规则:
1. 剔除仅与"登录"有关的前置条件 (如"用户已登录""已登录系统"), 这些由系统统一处理, 不要生成步骤.
2. "已有/已存在/已添加 X" 这类前置, 要拆成创建该数据的**具体多步原子操作**, 而不是笼统的一句"添加一个X".
   例: "已有收货地址" → ["鼠标悬浮在右上角用户菜单按钮上", "点击菜单中的收货地址选项", "点击添加新地址按钮", "在收件人输入框输入测试用户",
       "在联系电话输入框输入13800000000", "点击省份下拉框", "在弹出选项中点击广东省",
       "点击城市下拉框", "在弹出选项中点击深圳市", "点击区县下拉框", "在弹出选项中点击罗湖区",
       "在详细地址输入框输入测试地址1号", "点击保存地址按钮"].
3. 每条只描述一个原子操作, 用自然语言; 输入类要给出具体测试值.
4. 若某前置条件无需任何操作 (纯状态描述且无法操作), 忽略它.
只输出 JSON: {"steps": ["步骤1", "步骤2", ...]}. 没有需要执行的步骤则 {"steps": []}."""

_DEFAULT_USER = """\
前置条件:
{{preconditions}}

请输出 {"steps": [...]} JSON。"""


class PreconditionExpander:
    """将前置条件转换为可执行步骤, 并插入到用例步骤最前面."""

    def __init__(self, llm: LLMAdapter, prompts: PromptLoader) -> None:
        self.llm = llm
        self.prompts = prompts

    def expand(self, case: ParsedCase) -> ParsedCase:
        """就地展开 case: 把前置步骤插到 steps 最前, 回填 precondition_step_count."""
        if not case.preconditions:
            return case
        steps = self._expand_text(case.preconditions)
        # 前置步骤必须在原始操作步骤前执行, 并记录数量供动作规划提示词使用.
        case.steps = steps + case.steps
        case.precondition_step_count = len(steps)
        return case

    def _expand_text(self, preconditions: list[str]) -> list[str]:
        # 用列表格式传给 LLM, 避免多条前置条件被误解成一整句.
        pre_text = "\n".join(f"- {p}" for p in preconditions)
        system = self.prompts.system("precondition", _DEFAULT_SYSTEM)
        user = self.prompts.user("precondition", _DEFAULT_USER, preconditions=pre_text)
        try:
            data = self.llm.complete_json("precondition", system, user).data
            steps = data.get("steps") if isinstance(data, dict) else None
            if isinstance(steps, list):
                return [str(s).strip() for s in steps if str(s).strip()]
        except Exception:
            # 前置展开是增强能力, 模型失败时走正则兜底保证用例仍可继续.
            pass
        return self._regex_fallback(preconditions)

    @staticmethod
    def _regex_fallback(preconditions: list[str]) -> list[str]:
        """兜底策略: 去掉登录类前置, 其它前置原样转成待执行步骤."""
        out = []
        for p in preconditions:
            if _LOGIN_PAT.search(p):
                continue
            out.append(p.strip())
        return out

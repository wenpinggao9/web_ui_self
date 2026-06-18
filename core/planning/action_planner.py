"""步骤⑥ 动作规划 —— 大模型把 步骤+预期 翻译成 {类型, 意图, 值} 结构化动作.

核心规则 (见 prompts/action_plan.system.md):
  - 只输出 JSON; 每个动作只含 类型 + 意图, 禁止任何选择器
  - 每个意图只含一个原子操作
  - 先输出操作步骤动作, 再输出预期断言动作
  - "xxx成功"类预期 → 否定断言 (验证不存在 "xxx失败")
  - 仅当步骤明确要求等待/按键时才生成 wait/press
技能知识注入到系统提示词.
"""
from __future__ import annotations

from typing import Any

from ..llm import LLMAdapter, PromptLoader
from ..parser import ExecutionBlock, ParsedCase
from .action_schema import PlannedAction, coerce_action
from .page_nav import (
    extract_navigable_pages,
    is_sidebar_nav_intent,
    is_sub_page_name,
    step_uses_in_page_context,
)

_DEFAULT_SYSTEM = """\
你是"动作规划器", 只负责规划"做什么", 不负责"怎么定位元素".

输出规则:
1. 只输出纯 JSON 对象, 形如 {"actions": [ {...}, {...} ]}, 禁止任何其他内容.
2. 每个动作只含 type 和 intent (可选 value/negate), 绝对禁止输出 CSS/XPath/选择器.
3. 每个 intent 只含一个原子操作 —— "点击A之后再点击B" 必须拆成两条.
4. 顺序: 先按顺序输出所有操作步骤对应的动作, 再按顺序输出所有预期结果对应的断言动作.
5. module_path 导航由系统统一完成; 步骤内页面 (如「在'XX'页面」「在XX列表页」) 须先 click 进入该页面, 再执行后续操作, 禁止省略页面导航.
6. "xxx成功" 类预期 → 改为否定断言: type=assert_text, value 取对应 "xxx失败" 文案, negate=true.
7. 输入动作: intent 要保留场景上下文 (如 "在弹窗中的收件人输入框输入张三"); value 为输入内容.
8. 上传动作 type=upload, extras.field 指定输入项字段名.
9. 不要假设具体组件库, 用语义化中文描述意图.
10. 仅当步骤明确写"等待"时才生成 type=wait; 仅当明确写"按下/回车"等才生成 type=press.
11. JSON 字符串值(intent/value)内严禁出现未转义的英文双引号 "; 需要引用文字时改用中文全角引号 “” 或省略引号. 这能避免 JSON 解析失败.

type 取值: click 点击 / hover 悬停 / fill 输入 / press 按键 / goto 跳转 / wait 等待 /
upload 上传 / assert_text 断言文本 / assert_count 断言计数 / assert_table 断言表格 / asset 资产.

断言:
- "页面包含/出现 X" → {"type":"assert_text","value":"X","negate":false}
- "页面不包含/无 X"  → {"type":"assert_text","value":"X","negate":true}
- "X 不可见/隐藏"    → {"type":"assert_text","value":"X","negate":true}
- "X 不包含 Y"       → {"type":"assert_text","intent":"在X区域内验证不包含Y","value":"Y","negate":true}
"""

_DEFAULT_USER = """\
模块路径: {{module_path}}
前 {{pre_count}} 条操作步骤由前置条件解析得到, 必须保持顺序.

操作步骤:
{{steps}}

预期结果:
{{expectations}}

请输出 {"actions": [...]} JSON."""

_BLOCK_OPS_USER = """\
模块路径: {{module_path}}
执行块: 第 {{block_no}} / {{total_blocks}} (仅操作, 本块后将有对应断言)
会话模式: {{session_mode}}
当前页面 URL: {{current_url}}
前 {{pre_count}} 条操作步骤由前置条件解析得到, 必须保持顺序.

操作步骤:
{{steps}}

请只输出操作类动作 (禁止 assert_*), 输出 {"actions": [...]} JSON."""

_BLOCK_ASSERT_USER = """\
模块路径: {{module_path}}
执行块: 第 {{block_no}} / {{total_blocks}} (仅断言, 本块操作已在上一阶段完成)

预期结果:
{{expectations}}

请只输出断言类动作 (assert_text / assert_count / assert_table), 输出 {"actions": [...]} JSON."""

# 步骤内页面语义见 page_nav.py (可导航业务页 vs 操作上下文)


def _has_page_navigation(actions: list[PlannedAction], page: str) -> bool:
    """是否已有进入该业务页的 click 意图."""
    for a in actions:
        if a.type != "click" or page not in a.intent:
            continue
        if is_sidebar_nav_intent(a.intent):
            return True
        if f"进入{page}" in a.intent:
            return True
    if is_sub_page_name(page) and _already_operating_on_page(actions, page):
        return True
    return False


def _already_operating_on_page(actions: list[PlannedAction], page: str) -> bool:
    """动作已在目标页面上操作, 无需再插侧栏导航."""
    for a in actions:
        if a.type != "click" or a.is_assert():
            continue
        intent = a.intent or ""
        if step_uses_in_page_context(intent):
            return True
        if page in intent and not is_sidebar_nav_intent(intent):
            return True
    return False


def _ensure_in_page_navigation(
    steps: list[str],
    actions: list[PlannedAction],
    *,
    preconditions: list[str] | None = None,
    current_url: str | None = None,
) -> list[PlannedAction]:
    """步骤含可导航业务页时, 若规划结果缺少进入该页的动作则补全 (不补操作上下文/子页)."""
    pages = extract_navigable_pages(steps, preconditions)
    if not pages:
        return actions

    split = len(actions)
    for i, a in enumerate(actions):
        if a.is_assert():
            split = i
            break
    ops = actions[:split]
    asserts = actions[split:]

    click_idxs = [i for i, a in enumerate(ops) if a.type == "click"]
    if not click_idxs:
        return actions

    new_ops = list(ops)
    inserted = 0
    for pi, page in enumerate(pages):
        if _has_page_navigation(new_ops, page):
            continue
        target = click_idxs[min(pi, len(click_idxs) - 1)] + inserted
        nav = PlannedAction(
            type="click",
            intent=f"点击侧栏菜单'{page}'进入{page}页面",
        )
        new_ops.insert(target, nav)
        inserted += 1
        click_idxs = [i + 1 if i >= target else i for i in click_idxs]

    return new_ops + asserts


class ActionPlanner:
    """把 ParsedCase 中的自然语言步骤规划成不含选择器的动作列表."""

    def __init__(self, llm: LLMAdapter, prompts: PromptLoader, skill_text: str = "") -> None:
        self.llm = llm
        self.prompts = prompts
        self.skill_text = skill_text

    def generate_actions(
        self,
        case: ParsedCase,
        roles: list[str] = None,
        *,
        current_url: str | None = None,
        cross_case_session: bool = False,
    ) -> tuple[list[PlannedAction], str]:
        """返回 (动作列表, 模型原始响应)."""
        # 系统提示词可由 prompts/action_plan.system.md 或 config 覆盖.
        system_base = self.prompts.system("action_plan", _DEFAULT_SYSTEM)
        # skill.md 中的组件库经验注入到最前面, 让规划阶段能理解领域约定.
        system = (self.skill_text + "\n\n" + system_base) if self.skill_text else system_base

        # 步骤和预期都保留编号, 方便模型稳定按原始顺序输出动作.
        pre_text = "\n".join(f"- {p}" for p in case.preconditions) or "(无)"
        res_text = _format_resources(case.resources)
        steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(case.steps)) or "(无)"
        exp_text = "\n".join(f"{i+1}. {e}" for i, e in enumerate(case.expectations)) or "(无)"
        roles_text = ", ".join(roles) if roles else "(未提供)"
        session_mode = (
            "跨用例连续执行 (保留浏览器状态, 可能已在上一用例结束时的页面)"
            if cross_case_session
            else "单用例独立执行"
        )
        user = self.prompts.user(
            "action_plan", _DEFAULT_USER,
            case_id=case.case_id,
            module_path=" / ".join(case.module_path),
            priority=case.priority or "(未提供)",
            preconditions=pre_text,
            resources=res_text,
            pre_count=case.precondition_step_count,
            steps=steps_text,
            expectations=exp_text,
            roles=roles_text,
            current_url=current_url or "(未知)",
            session_mode=session_mode,
        )

        result = self.llm.complete_json("action_plan", system, user)
        # 模型返回可能有别名/中文键, 统一交给 _extract_actions 做容错转换.
        actions = _extract_actions(result.data)
        actions = _ensure_in_page_navigation(
            case.steps, actions,
            preconditions=case.preconditions,
            current_url=current_url,
        )
        return actions, result.raw

    def generate_block_actions(
        self,
        case: ParsedCase,
        block: ExecutionBlock,
        roles: list[str] = None,
        block_no: int = 1,
        total_blocks: int = 1,
        *,
        current_url: str | None = None,
        preconditions: list[str] | None = None,
        cross_case_session: bool = False,
    ) -> tuple[list[PlannedAction], list[Any]]:
        """按执行块规划: 先仅操作, 再仅断言 (供交错式用例分块执行)."""
        system_base = self.prompts.system("action_plan", _DEFAULT_SYSTEM)
        system = (self.skill_text + "\n\n" + system_base) if self.skill_text else system_base
        roles_text = ", ".join(roles) if roles else "(未提供)"
        session_mode = (
            "跨用例连续执行 (保留浏览器状态, 可能已在上一用例结束时的页面)"
            if cross_case_session
            else "单用例独立执行"
        )
        actions: list[PlannedAction] = []
        raws: list[Any] = []

        if block.operations:
            steps_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(block.operations)) or "(无)"
            user = self.prompts.user(
                "action_plan_block_ops", _BLOCK_OPS_USER,
                case_id=case.case_id,
                module_path=" / ".join(case.module_path),
                block_no=block_no,
                total_blocks=total_blocks,
                pre_count=case.precondition_step_count if block_no == 1 else 0,
                steps=steps_text,
                roles=roles_text,
                current_url=current_url or "(未知)",
                session_mode=session_mode,
            )
            result = self.llm.complete_json("action_plan", system, user)
            block_ops = _extract_actions(result.data)
            block_ops = _ensure_in_page_navigation(
                block.operations, block_ops,
                preconditions=preconditions,
                current_url=current_url,
            )
            actions.extend(block_ops)
            raws.append(result.raw)

        if block.expectations:
            exp_text = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(block.expectations)) or "(无)"
            user = self.prompts.user(
                "action_plan_block_assert", _BLOCK_ASSERT_USER,
                case_id=case.case_id,
                module_path=" / ".join(case.module_path),
                block_no=block_no,
                total_blocks=total_blocks,
                expectations=exp_text,
                roles=roles_text,
            )
            result = self.llm.complete_json("action_plan", system, user)
            actions.extend(_extract_actions(result.data))
            raws.append(result.raw)

        return actions, raws


def _extract_actions(data) -> list[PlannedAction]:
    """从模型返回中提取动作数组, 兼容单动作对象和中文字段名."""
    raw_list = None
    if isinstance(data, dict):
        raw_list = data.get("actions") or data.get("动作列表") or data.get("动作")
        if raw_list is None and "type" in data:
            # 少数模型会直接返回单个动作对象, 这里包装成列表继续处理.
            raw_list = [data]
    elif isinstance(data, list):
        raw_list = data
    out: list[PlannedAction] = []
    for item in raw_list or []:
        a = coerce_action(item)
        if a is not None:
            # 非法动作被丢弃, 避免一个坏项拖垮整条用例.
            out.append(a)
    return out


def _format_resources(resources: dict) -> str:
    """把用例资源定义格式化给动作规划器, 供 upload 动作选择 value."""
    if not resources:
        return "(无)"
    lines = []
    for name, res in resources.items():
        source = getattr(res, "source", "")
        filename = getattr(res, "filename", "")
        lines.append(f"- {name}: 来源={source}, 文件名={filename}")
    return "\n".join(lines)

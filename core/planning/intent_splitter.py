"""步骤⑦ 意图拆分 —— 每个 case 一次 LLM 调用, 批量拆分规划动作 + 剥离重复菜单点击.

- 整份动作列表一次送入 intent_split LLM, 按 index 返回每条是否拆分及子步骤
- 子动作继承父的 value/extras, 清除 selector, 打 intent_split 标记
- 剥离动作列表开头与 模块路径 重复的菜单点击 (步骤⑤ 已完成导航)
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..execution.assert_or import is_or_assert
from ..llm import LLMAdapter, PromptLoader
from .action_schema import PlannedAction

# 复合连接词: 用于日志/调试, 标注哪些动作含顺序连接词
_COMPOUND_MARKERS = ("之后再", "然后", "接着", "再点击", "再输入", "并点击", "，再", ",再", "并且")

# api_call 需拆成多次投放/查询: 「获取 id1 和 id2」「N题」等
_API_MULTI_FETCH_RE = re.compile(
    r"(\d+)\s*题|"
    r"获取[^，。；\n]*和[^，。；\n]*|"
    r"[a-zA-Z]+\d+[^，。；\n]*和[^，。；\n]*[a-zA-Z]+\d+",
)

_DEFAULT_SYSTEM = """\
你收到一个用例的完整动作列表 (按 index 编号), 对每一条独立判断是否需要拆成原子步骤.
只输出 JSON: {"items": [{"index": 0, "split": true/false, "steps": [...]}, ...]}.
items 须覆盖每一个 index; [跳过拆分] 的条目 split=false 且 steps 只含原动作一条."""

_DEFAULT_USER = """\
待拆分动作列表 (index 从 0 开始, 共 {{action_count}} 条):
{{action_list}}

请对每一条动作独立判断是否需要拆分, 严格只输出 {"items": [...]} JSON."""


class IntentSplitter:
    """识别并拆分复合意图, 保证执行层一次只处理一个原子动作."""

    def __init__(self, llm: LLMAdapter, prompts: PromptLoader) -> None:
        self.llm = llm
        self.prompts = prompts

    def split_all(self, actions: list[PlannedAction]) -> list[PlannedAction]:
        out, _, _ = self.split_all_with_raw(actions)
        return out

    def split_all_with_raw(
        self, actions: list[PlannedAction],
    ) -> tuple[list[PlannedAction], list[str], Optional[str]]:
        """每个 case 一次 LLM 调用; 返回 (拆分后动作, 日志行, 模型原始响应)."""
        if not actions:
            return [], [], None

        if not _any_needs_llm_split(actions):
            return list(actions), [], None

        expanded, raw = self._split_batch(actions)
        if expanded is None:
            return list(actions), [], raw

        out: list[PlannedAction] = []
        notes: list[str] = []
        llm_out_idx = 0
        for a in actions:
            if a.intent_split or _should_skip_split(a):
                out.append(a)
                continue
            if llm_out_idx >= len(expanded):
                out.append(a)
                continue
            chunk = expanded[llm_out_idx]
            llm_out_idx += 1
            if len(chunk) > 1:
                out.extend(chunk)
                notes.append(f"意图拆分: [{a.type}] {a.intent} → {len(chunk)} 步")
            else:
                out.append(chunk[0] if chunk else a)

        return out or list(actions), notes, raw

    def _split_batch(
        self, actions: list[PlannedAction],
    ) -> tuple[Optional[list[list[PlannedAction]]], Optional[str]]:
        """一次 LLM 调用拆分整份动作列表; 返回按「需 LLM 条目」顺序的展开块列表."""
        system = self.prompts.system("intent_split", _DEFAULT_SYSTEM)
        action_list = _format_action_list(actions)
        user = self.prompts.user(
            "intent_split",
            _DEFAULT_USER,
            action_list=action_list,
            action_count=len(actions),
        )
        try:
            result = self.llm.complete_json("intent_split", system, user)
            data = result.data
            raw = result.raw
        except Exception:
            return None, None

        items_map = _parse_batch_items(data)
        if items_map is None:
            return None, raw

        expanded: list[list[PlannedAction]] = []
        for i, a in enumerate(actions):
            if a.intent_split or _should_skip_split(a):
                continue
            item = items_map.get(i)
            if not item:
                expanded.append([a])
                continue
            children = _expand_item(a, item)
            expanded.append(children)
        return expanded, raw


def strip_duplicate_menu_clicks(actions: list[PlannedAction], module_path: list[str]) -> list[PlannedAction]:
    """剥离动作列表开头与 模块路径 重复的菜单点击."""
    if not module_path:
        return actions
    menu_terms = {m.strip() for m in module_path if m.strip()}
    out = list(actions)
    while out:
        a = out[0]
        if a.type == "click" and any(_contains_term(a.intent, term) for term in menu_terms):
            out.pop(0)
            continue
        break
    return out


def _any_needs_llm_split(actions: list[PlannedAction]) -> bool:
    return any(not a.intent_split and not _should_skip_split(a) for a in actions)


def _format_action_list(actions: list[PlannedAction]) -> str:
    lines: list[str] = []
    for i, a in enumerate(actions):
        if a.intent_split:
            tag = " [已拆分]"
        elif _should_skip_split(a):
            tag = " [跳过拆分]"
        else:
            tag = ""
        line = (
            f"{i}. [{a.type}]{tag} "
            f"intent={a.intent} "
            f"value={_format_field(a.value)} "
            f"extras={_format_extras(a.extras)}"
        )
        if a.negate:
            line += " negate=true"
        lines.append(line)
    return "\n".join(lines)


def _parse_batch_items(data: object) -> Optional[dict[int, dict]]:
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    out: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict) or "index" not in item:
            continue
        try:
            idx = int(item["index"])
        except (TypeError, ValueError):
            continue
        out[idx] = item
    return out or None


def _expand_item(parent: PlannedAction, item: dict) -> list[PlannedAction]:
    steps = item.get("steps") if isinstance(item.get("steps"), list) else None
    if not steps:
        return [parent]
    split = item.get("split")
    if split is False:
        return [parent]
    children = _steps_to_children(parent, steps)
    if len(children) <= 1:
        return [parent]
    return children


def _steps_to_children(parent: PlannedAction, steps: list) -> list[PlannedAction]:
    out: list[PlannedAction] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        intent = str(s.get("intent") or s.get("意图") or "").strip()
        if not intent:
            continue
        child = parent.clone_child(intent)
        t = str(s.get("type") or s.get("类型") or "").strip()
        if t:
            child.type = t
        if "value" in s or "值" in s:
            value = s.get("value") if "value" in s else s.get("值")
            if value is not None:
                vs = str(value)
                if not _raw_has_unfilled_placeholder(vs):
                    child.value = vs
        if "negate" in s or "否定" in s:
            child.negate = bool(s.get("negate") or s.get("否定"))
        extras = s.get("extras") or s.get("extra")
        if isinstance(extras, dict):
            child.extras = dict(extras)
        out.append(child)
    return out


def _looks_compound(intent: str) -> bool:
    """用关键词做轻量预判, 避免每条动作都请求 LLM 拆分."""
    return any(mk in intent for mk in _COMPOUND_MARKERS)


def _api_call_needs_split(intent: str) -> bool:
    """合并的 api_call (如一次获取 orderId1 和 orderId2) 应送意图拆分."""
    if not intent:
        return False
    if _looks_compound(intent):
        return True
    if _API_MULTI_FETCH_RE.search(intent):
        return True
    get_pos = intent.find("获取")
    if get_pos >= 0:
        tail = intent[get_pos:]
        names = re.findall(r"([a-zA-Z]+)(\d+)", tail)
        if len(names) >= 2 and ("和" in tail or "、" in tail):
            return True
    return False


def _contains_term(intent: str, term: str) -> bool:
    cleaned = re.sub(r"[\"'“”‘’「」『』]", "", intent)
    return term in cleaned


def _should_skip_split(a: PlannedAction) -> bool:
    """或断言、bind_session、单条 api_call 等已成型动作不再送 LLM 拆分."""
    if is_or_assert(a):
        return True
    if a.type == "bind_session":
        return True
    if a.type == "api_call" and not _api_call_needs_split(a.intent or ""):
        return True
    return False


def _format_field(value: object) -> str:
    if value is None or value == "":
        return "(无)"
    return str(value)


def _format_extras(extras: object) -> str:
    if not extras:
        return "(无)"
    if isinstance(extras, dict):
        return json.dumps(extras, ensure_ascii=False)
    return str(extras)


def _raw_has_unfilled_placeholder(raw: str) -> bool:
    return "{{" in raw or "}}" in raw

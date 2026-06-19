"""步骤① 解析器 —— Markdown 用例解析.

按标题层级提取:
  #     一级模块 → module_path[0]
  ##    二级模块 → module_path[1]
  ###   三级模块 → module_path[2]
  ####  用例ID：XXX → 新用例开始
  ##### 优先级 / 前置条件 / 操作步骤 / 预期结果 / 操作步骤与预期结果 / 资源定义 / 用例依赖

列表项支持 `1. xxx` 与 `- xxx`. 没有标题时以文件名兜底作为模块路径.
资源定义从 ```yaml 块解析, source ∈ 资产/上传/本地, filename 必须存在.

用例编排支持两种格式:
  A) 操作步骤段 + 预期结果段 (先全部步骤, 后全部断言)
  B) 操作步骤与预期结果段 (每步操作后立即跟对应预期, 含 `->` 或子 bullet `预期:`)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from .schema import CaseResource, ExecutionBlock, ParsedCase

# 段标题归一: 较长键优先匹配 (操作步骤与预期结果 先于 操作步骤)
_SECTION_ALIASES: list[tuple[str, str]] = [
    ("操作步骤与预期结果", "interleaved"),
    ("操作步骤 & 预期结果", "interleaved"),
    ("操作步骤", "steps"),
    ("步骤", "steps"),
    ("预期结果", "expectations"),
    ("预期", "expectations"),
    ("前置条件", "preconditions"),
    ("优先级", "priority"),
    ("验证点", "notes"),
    ("资源定义", "resources"),
    ("资源", "resources"),
    ("用例依赖", "dependencies"),
    ("依赖", "dependencies"),
    ("备注", "notes"),
    ("目标系统", "target_system"),
    ("测试项目", "session_name"),
    ("使用角色", "role"),
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM_RE = re.compile(
    r"^(\s*)(?:\d+[.)]\s+|[-*]\s+|[-](?=[「'『\"]))(.*)$"
)
_CASE_ID_RE = re.compile(r"用例\s*ID\s*[:：]\s*(.+)$")
_ARROW_SPLIT_RE = re.compile(r"\s*(?:->|→)\s*")
_EXPECT_PREFIX_RE = re.compile(r"^预期[:：]\s*")
_INTERLEAVED_NOISE_RE = re.compile(
    r"^【?操作步骤.*?】?\s*[-=]?\s*>\s*【?预期",
    re.IGNORECASE,
)


class _InterleavedState:
    """解析「操作步骤与预期结果」段时的暂存."""

    def __init__(self) -> None:
        self.pending_op: Optional[str] = None
        self.pending_exps: list[str] = []

    def flush(self, case: ParsedCase) -> None:
        if not self.pending_op and not self.pending_exps:
            return
        case.execution_blocks.append(
            ExecutionBlock(
                operations=[self.pending_op] if self.pending_op else [],
                expectations=list(self.pending_exps),
            )
        )
        self.pending_op = None
        self.pending_exps = []

    def reset(self) -> None:
        self.pending_op = None
        self.pending_exps = []


def parse_markdown(path: str | Path) -> list[ParsedCase]:
    """把一个 Markdown 用例文件解析成结构化 ParsedCase 列表."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()

    module_path: list[str] = []
    cases: list[ParsedCase] = []
    cur: Optional[ParsedCase] = None
    cur_section: Optional[str] = None
    in_code = False
    code_lang = ""
    code_buf: list[str] = []
    interleaved = _InterleavedState()

    file_session = ""
    file_role = ""
    file_notes: list[str] = []

    def flush_code() -> None:
        nonlocal code_buf, code_lang
        if cur is not None and cur_section == "resources" and code_lang.lower() in ("yaml", "yml", ""):
            _parse_resource_yaml("\n".join(code_buf), cur)
        code_buf = []
        code_lang = ""

    def flush_interleaved() -> None:
        if cur is not None and cur_section == "interleaved":
            interleaved.flush(cur)

    def on_section_change(new_section: Optional[str]) -> None:
        nonlocal cur_section
        if cur_section == "interleaved":
            flush_interleaved()
        interleaved.reset()
        cur_section = new_section

    def on_new_case(new_case: ParsedCase) -> None:
        flush_interleaved()
        interleaved.reset()

    for raw in lines:
        fence = raw.lstrip()
        if fence.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = fence[3:].strip()
                code_buf = []
            else:
                in_code = False
                flush_code()
            continue
        if in_code:
            code_buf.append(raw)
            continue

        m = _HEADING_RE.match(raw)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if level <= 3:
                flush_interleaved()
                module_path = module_path[: level - 1]
                module_path.append(title)
                cur = None
                on_section_change(None)
            elif level == 4:
                flush_interleaved()
                cid = _extract_case_id(title)
                cur = ParsedCase(case_id=cid, module_path=list(module_path), source_path=p)
                if file_session and not cur.session_name:
                    cur.session_name = file_session
                if file_role and not cur.role:
                    cur.role = file_role
                if file_notes:
                    cur.notes = list(file_notes) + list(cur.notes)
                cases.append(cur)
                on_new_case(cur)
                on_section_change(None)
            else:
                new_sec = _resolve_section(title)
                on_section_change(new_sec)
                if cur is not None and cur_section == "priority":
                    inline = _strip_section_label(title)
                    if inline:
                        cur.priority = inline
            continue

        if cur is None or cur_section is None:
            line_content = raw.strip()
            if cur is None and cur_section == "session_name" and line_content and line_content != "---":
                file_session = line_content
            elif cur is None and cur_section == "role" and line_content and line_content != "---":
                file_role = line_content
            elif cur is None and cur_section == "notes":
                item = _LIST_ITEM_RE.match(raw)
                content = item.group(2).strip() if item else line_content
                if content and content not in ("---", "***", "___"):
                    file_notes.append(content)
            continue

        if cur_section == "interleaved":
            _append_interleaved(cur, interleaved, raw)
            continue

        item = _LIST_ITEM_RE.match(raw)
        content = item.group(2).strip() if item else raw.strip()
        if not content or content in ("---", "***", "___"):
            continue
        if cur_section == "steps" and _ARROW_SPLIT_RE.search(content):
            _append_arrow_step(cur, content)
            continue
        _append_section(cur, cur_section, content)

    flush_interleaved()
    return cases


def _append_interleaved(case: ParsedCase, state: _InterleavedState, raw: str) -> None:
    item = _LIST_ITEM_RE.match(raw)
    if not item:
        stripped = raw.strip()
        if stripped and not _INTERLEAVED_NOISE_RE.search(stripped):
            if _ARROW_SPLIT_RE.search(stripped):
                state.flush(case)
                _split_arrow_into_block(case, stripped)
            return
        return

    indent, content = len(item.group(1)), item.group(2).strip()
    if not content or _INTERLEAVED_NOISE_RE.search(content):
        return

    if indent >= 2 and _EXPECT_PREFIX_RE.match(content):
        exp = _EXPECT_PREFIX_RE.sub("", content).strip()
        if exp:
            state.pending_exps.append(exp)
        return

    if _ARROW_SPLIT_RE.search(content):
        state.flush(case)
        _split_arrow_into_block(case, content)
        return

    state.flush(case)
    state.pending_op = re.sub(r"^\d+[.)]\s*", "", content).strip()


def _split_arrow_into_block(case: ParsedCase, content: str) -> None:
    left, right = _ARROW_SPLIT_RE.split(content, maxsplit=1)
    op = re.sub(r"^\d+[.)]\s*", "", left).strip()
    exp = right.strip() if right else ""
    case.execution_blocks.append(
        ExecutionBlock(
            operations=[op] if op else [],
            expectations=[exp] if exp else [],
        )
    )


def _append_arrow_step(case: ParsedCase, content: str) -> None:
    """操作步骤段内联 `->` 也按交错块解析."""
    _split_arrow_into_block(case, content)


def _extract_case_id(title: str) -> str:
    m = _CASE_ID_RE.search(title)
    if m:
        return m.group(1).strip()
    return title.strip()


def _resolve_section(title: str) -> Optional[str]:
    cleaned = re.sub(r"^\d+[.)]\s*", "", title).strip()
    for key, field in _SECTION_ALIASES:
        if cleaned.startswith(key):
            return field
    return None


def _strip_section_label(title: str) -> str:
    cleaned = re.sub(r"^\d+[.)]\s*", "", title).strip()
    for key, _ in _SECTION_ALIASES:
        if cleaned.startswith(key):
            return cleaned[len(key):].strip(" :：")
    return ""


def _append_section(case: ParsedCase, section: str, content: str) -> None:
    if section == "priority":
        if not case.priority:
            case.priority = content
    elif section == "preconditions":
        case.preconditions.append(content)
    elif section == "steps":
        case.steps.append(content)
    elif section == "expectations":
        case.expectations.append(content)
    elif section == "dependencies":
        case.dependencies.append(content)
    elif section == "notes":
        case.notes.append(content)
    elif section == "target_system":
        if not case.target_system:
            case.target_system = content
    elif section == "session_name":
        if not case.session_name:
            case.session_name = content
    elif section == "role":
        if not case.role:
            case.role = content


def _parse_resource_yaml(block: str, case: ParsedCase) -> None:
    try:
        data = yaml.safe_load(block)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for name, spec in data.items():
        if not isinstance(spec, dict):
            continue
        source = str(spec.get("来源") or spec.get("source") or "").strip()
        filename = str(spec.get("文件名") or spec.get("filename") or "").strip()
        if source not in ("资产", "上传", "本地"):
            raise ValueError(f"资源 {name} 的来源必须是 资产/上传/本地, 实际: {source!r}")
        if not filename:
            raise ValueError(f"资源 {name} 缺少文件名")
        case.resources[str(name)] = CaseResource(name=str(name), source=source, filename=filename)

"""步骤内「在XX页面」的通用语义判断 —— 区分可导航业务页 vs 操作上下文.

不绑定具体业务名词; 仅依据中文步骤/前置条件的表述模式.
"""
from __future__ import annotations

import re

# 带引号的模块/列表页名: 在'订单列表'页面
QUOTED_PAGE_RE = re.compile(r"在[「\"']([^「\"'']+)[」\"'](?:页面|页)")

# 操作上下文: 在详情页 / 在订单详情页 / 在弹窗中 … (非侧栏菜单)
IN_PAGE_CONTEXT_RE = re.compile(
    r"在(?:弹窗|对话框|抽屉|浮层|表格中|列表行中|筛选区中|"
    r"(?:[^，。；\s'「」]{0,12})?详情页)"
)

# 无引号业务页 (排除上下文短语): 在用户管理页面
NAV_UNQUOTED_PAGE_RE = re.compile(
    r"在(?!弹窗|对话框|抽屉|浮层|表格|列表行|筛选区|"
    r"(?:[^，。；\s'「」]{0,12})?详情页)"
    r"([^「\"'\s']+?)(?:页面|页)"
)

# 前置已声明处于某页: 已在XX页 / 且在XX页 / 当前在XX页
ALREADY_ON_PAGE_RE = re.compile(r"(?:已在|且在|当前在).{0,40}(?:页面|页)")

# 规划/执行层识别的侧栏导航意图
SIDEBAR_NAV_INTENT_RE = re.compile(r"侧栏|菜单|导航|Tab|tab|进入.{0,12}页面")


def step_uses_in_page_context(step: str) -> bool:
    """步骤是否在描述操作上下文 (非要先点侧栏进入的模块)."""
    return bool(IN_PAGE_CONTEXT_RE.search(step))


def is_sub_page_name(name: str) -> bool:
    """子页/详情类名称 —— 通常由行内按钮进入, 不是侧栏一级菜单."""
    name = (name or "").strip()
    if not name:
        return False
    if name.endswith("详情"):
        return True
    return name in ("详情", "弹窗", "对话框", "抽屉", "浮层")


def preconditions_indicate_on_page(preconditions: list[str] | None) -> bool:
    """前置条件是否声明已在某业务页 (跨用例会话应保留页面状态)."""
    text = " ".join(preconditions or [])
    return bool(ALREADY_ON_PAGE_RE.search(text))


def should_preserve_page_on_case_start(
    preconditions: list[str] | None,
    steps: list[str] | None = None,
) -> bool:
    """跨用例连续执行时, 是否应保留当前页 (勿 reload)."""
    if preconditions_indicate_on_page(preconditions):
        return True
    for step in steps or []:
        if step_uses_in_page_context(step):
            return True
    return False


def extract_navigable_pages(
    steps: list[str],
    preconditions: list[str] | None = None,
) -> list[str]:
    """从步骤提取需要侧栏/菜单导航的业务页名 (排除操作上下文与子页名)."""
    on_page = preconditions_indicate_on_page(preconditions)
    pages: list[str] = []
    for step in steps:
        if step_uses_in_page_context(step):
            continue
        for m in QUOTED_PAGE_RE.finditer(step):
            pages.append(m.group(1))
        m2 = NAV_UNQUOTED_PAGE_RE.search(step)
        if m2:
            pages.append(m2.group(1))
    out: list[str] = []
    for p in pages:
        if is_sub_page_name(p):
            continue
        if on_page and p == "详情":
            continue
        if p not in out:
            out.append(p)
    return out


def is_sidebar_nav_intent(intent: str) -> bool:
    return bool(SIDEBAR_NAV_INTENT_RE.search(intent or ""))

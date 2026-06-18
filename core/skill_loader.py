"""步骤㉒ 组件库知识 (技能) —— 教 AI 认识 UI 组件 + 框架专属选择器.

技能.md: YAML frontmatter 定义组件 HTML 结构、框架探测规则和框架选择器,
正文描述操作语义. 加载策略:
  - 选择类组件(select/tree/cascader)保留完整 HTML(对规则匹配影响最大),
    其他组件只保留名称索引.
  - 框架探测(framework_detect) + 框架选择器(framework_selectors):
    执行开始时探测页面使用的组件库, 加载对应选择器传给 DOM 快照脚本.
  - 注入到步骤⑥ 动作规划的系统提示词.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

# 保留完整 HTML 的"选择类"组件类别
_FULL_HTML_CATEGORIES = {"select", "tree", "cascader"}


def load_skill_text(path: str | Path) -> str:
    """读取 skill.md 并整理成适合注入 LLM system prompt 的文本."""
    p = Path(path)
    if not p.exists():
        return ""
    raw = p.read_text(encoding="utf-8")
    front, body = _split_frontmatter(raw)
    meta = yaml.safe_load(front) if front else {}
    components = (meta or {}).get("components", []) if isinstance(meta, dict) else []

    lines: list[str] = ["# UI 组件知识 (技能)", "", "## 组件索引"]
    full_html_blocks: list[str] = []
    for c in components:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        category = c.get("category", "")
        lines.append(f"- {name} (类型: {category})")
        if category in _FULL_HTML_CATEGORIES and c.get("html"):
            full_html_blocks.append(f"### {name}\n```html\n{c['html'].rstrip()}\n```")

    if full_html_blocks:
        lines.append("")
        lines.append("## 选择类组件完整结构")
        lines.extend(full_html_blocks)

    if body.strip():
        lines.append("")
        lines.append(body.strip())

    return "\n".join(lines)


def load_framework_selectors(path: str | Path) -> dict[str, dict[str, str]]:
    """从 skill.md 中解析框架专属选择器.

    返回: {框架名: {container_sel, dropdown_sel, option_sel, dialog_sel, form_sel}}
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = p.read_text(encoding="utf-8")
    front, _ = _split_frontmatter(raw)
    meta = yaml.safe_load(front) if front else {}
    fw_selectors = (meta or {}).get("framework_selectors", {})
    if not isinstance(fw_selectors, dict):
        return {}

    result = {}
    for name, sels in fw_selectors.items():
        if not isinstance(sels, dict):
            continue
        result[name] = {
            "container_sel": sels.get("container_sel", ""),
            "dropdown_sel": sels.get("dropdown_sel", ""),
            "option_sel": sels.get("option_sel", ""),
            "dialog_sel": sels.get("dialog_sel", ""),
            "form_sel": sels.get("form_sel", ""),
        }
    return result


def detect_framework(page: Any, path: str | Path) -> str:
    """根据 skill.md 中定义的探测规则识别前端框架, 返回框架名或 'default'.

    skill.md frontmatter 中需定义:
      framework_detect:
        - name: ant-design
          check: '.ant-modal, .ant-select-dropdown'
        - name: element-plus
          check: '.el-dialog, .el-select-dropdown'
    """
    p = Path(path)
    if not p.exists():
        return 'default'
    raw = p.read_text(encoding="utf-8")
    front, _ = _split_frontmatter(raw)
    meta = yaml.safe_load(front) if front else {}
    detect_rules = (meta or {}).get("framework_detect", [])
    if not isinstance(detect_rules, list):
        return 'default'

    # 构建探测 JS: 遍历每个框架的 check 选择器, 看页面是否存在匹配元素
    checks = []
    for rule in detect_rules:
        if not isinstance(rule, dict):
            continue
        name = rule.get("name", "")
        check = rule.get("check", "")
        if name and check:
            checks.append({"name": name, "sel": check})

    if not checks:
        return 'default'

    js = """
    () => {
      const checks = __CHECKS__;
      for (const c of checks) {
        try { if (document.querySelector(c.sel)) return c.name; }
        catch {}
      }
      return 'default';
    }
    """.replace('__CHECKS__', str(checks).replace("'", '"'))

    try:
        result = page.evaluate(js)
    except Exception:
        return 'default'

    return result if isinstance(result, str) else 'default'


def get_framework_selectors(path: str | Path, page: Any) -> Optional[dict[str, str]]:
    """探测框架并返回对应选择器. 整合 detect + load 一步完成.

    Args:
        path: skill.md 路径.
        page: Playwright page 对象.

    Returns:
        匹配到的框架选择器字典, 无匹配时返回 None (调用方使用默认值).
    """
    framework = detect_framework(page, path)
    if framework == 'default':
        return None
    all_selectors = load_framework_selectors(path)
    return all_selectors.get(framework)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """切出 --- 包裹的 YAML frontmatter 与正文."""
    t = text.lstrip()
    if not t.startswith("---"):
        return "", text
    parts = t.split("---", 2)
    if len(parts) >= 3:
        return parts[1], parts[2]
    return "", text

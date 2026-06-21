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

import json
from pathlib import Path
from typing import Any, Optional

import yaml

# 保留完整 HTML 的"选择类"组件类别
_FULL_HTML_CATEGORIES = {"select", "tree", "cascader"}


def load_skill_frontmatter(path: str | Path) -> dict[str, Any]:
    """读取 skill.md 的 YAML frontmatter 元数据."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = p.read_text(encoding="utf-8")
    front, _ = _split_frontmatter(raw)
    meta = yaml.safe_load(front) if front else {}
    return meta if isinstance(meta, dict) else {}


def load_entrypoints(path: str | Path) -> dict[str, dict[str, Any]]:
    """解析 skill.md frontmatter 中的 entrypoints 声明."""
    meta = load_skill_frontmatter(path)
    eps = meta.get("entrypoints") or {}
    if not isinstance(eps, dict):
        return {}
    return {k: v for k, v in eps.items() if isinstance(v, dict)}


def invoke_entrypoint(
    path: str | Path,
    name: str,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """按 frontmatter entrypoints 声明调用技能入口.

    支持:
      - kind=python + module + callable (进程内 import 调用)
      - kind=python + script + args (子进程, JSON 入出参协议)
    """
    import importlib
    import json
    import subprocess
    import sys
    import tempfile

    eps = load_entrypoints(path)
    ep = eps.get(name)
    if not ep:
        raise KeyError(f"entrypoint not found: {name}")

    kind = str(ep.get("kind") or "python")
    if kind != "python":
        raise ValueError(f"unsupported entrypoint kind: {kind}")

    if ep.get("module") and ep.get("callable"):
        mod = importlib.import_module(str(ep["module"]))
        fn = getattr(mod, str(ep["callable"]), None)
        if not callable(fn):
            raise AttributeError(f"callable not found: {ep['module']}.{ep['callable']}")
        return fn(*args, **kwargs)

    script_tpl = ep.get("script")
    if script_tpl:
        base_dir = Path(path).parent
        script = Path(str(script_tpl).replace("{baseDir}", str(base_dir)))
        if not script.is_file():
            raise FileNotFoundError(script)

        with tempfile.NamedTemporaryFile("w", suffix=".in.json", delete=False, encoding="utf-8") as fin:
            json.dump({"args": list(args), "kwargs": kwargs}, fin, ensure_ascii=False)
            in_path = fin.name
        out_path = in_path.replace(".in.json", ".out.json")

        cmd_args = []
        for arg in ep.get("args") or []:
            s = str(arg).replace("{baseDir}", str(base_dir))
            s = s.replace("{input_json}", in_path).replace("{output_json}", out_path)
            cmd_args.append(s)

        proc = subprocess.run(
            [sys.executable, str(script), *cmd_args],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or f"entrypoint failed: {name}")

        try:
            with open(out_path, encoding="utf-8") as fout:
                payload = json.load(fout)
        finally:
            Path(in_path).unlink(missing_ok=True)
            Path(out_path).unlink(missing_ok=True)

        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    raise ValueError(f"invalid entrypoint config: {name}")


def format_skills_for_decider(path: str | Path) -> str:
    """将 skill.md entrypoints 格式化为 L3 元素决策 system prompt 片段."""
    eps = load_entrypoints(path)
    if not eps:
        return ""
    lines = [
        "你可以请求以下技能 (输出 use_skill JSON, 由系统自动调用):",
        "",
        "**2A 节点纠偏**: choose_best_input_target, choose_best_click_target, "
        "choose_best_checkbox_target, find_switch_in_row",
        "**2B selector 构建**: build_dropdown_option_selector, build_el_select_trigger_selector, "
        "build_checkbox_selector, build_radio_selector, build_tree_checkbox_selector, build_tree_node_selector, build_date_picker_selector",
        "",
        "能力说明:",
    ]
    for name, conf in eps.items():
        desc = (conf.get("description") or name) if isinstance(conf, dict) else name
        lines.append(f"- {name}: {desc}")
    lines.append("")
    lines.append("下拉选项/复选框/树节点等复杂组件优先用格式二B; 内层 span 误选时用格式二A.")
    return "\n".join(lines)


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
        const parts = String(c.sel || '').split(',').map(s => s.trim()).filter(Boolean);
        for (const sel of parts) {
          try { if (document.querySelector(sel)) return c.name; }
          catch {}
        }
      }
      return 'default';
    }
    """.replace("__CHECKS__", json.dumps(checks, ensure_ascii=False))

    try:
        result = page.evaluate(js)
    except Exception:
        return 'default'

    return result if isinstance(result, str) else 'default'


def get_framework_selectors(
    path: str | Path, page: Any,
) -> tuple[Optional[str], Optional[dict[str, str]]]:
    """探测框架并返回 (框架名, 选择器). 无匹配时返回 (None, None).

    Args:
        path: skill.md 路径.
        page: Playwright page 对象.

    Returns:
        (framework_name, selectors); framework_name 为 skill.md 中 framework_detect.name.
    """
    framework = detect_framework(page, path)
    if framework == 'default':
        return None, None
    all_selectors = load_framework_selectors(path)
    return framework, all_selectors.get(framework)


def load_component_structures(path: str | Path) -> dict[str, Any]:
    """读取 skill.md frontmatter 中的 component_structures."""
    meta = load_skill_frontmatter(path)
    structs = meta.get("component_structures") or {}
    return structs if isinstance(structs, dict) else {}


def load_component_class_features(path: str | Path) -> dict[str, list[str]]:
    """从 component_structures 提取各库 class_features 前缀."""
    structs = load_component_structures(path)
    result: dict[str, list[str]] = {}
    for lib_name, lib_conf in structs.items():
        if not isinstance(lib_conf, dict):
            continue
        feats = lib_conf.get("class_features") or []
        if isinstance(feats, list):
            result[str(lib_name)] = [str(f).lower() for f in feats if str(f).strip()]
    return result


def get_component_structure(
    path: str | Path,
    component_library: str,
    component_type: str,
) -> Optional[dict[str, str]]:
    """从 skill.md component_structures 按需查找 html + click_target."""
    lib_key = (component_library or "").strip().lower()
    comp_key = (component_type or "").strip().lower()
    if not lib_key or not comp_key:
        return None
    structs = load_component_structures(path)
    lib_conf = structs.get(lib_key)
    if not isinstance(lib_conf, dict):
        for k, v in structs.items():
            if lib_key in k.lower() or k.lower() in lib_key:
                lib_conf = v
                break
    if not isinstance(lib_conf, dict):
        if lib_key != "generic":
            return get_component_structure(path, "generic", component_type)
        return None
    comp_keys = [comp_key]
    if comp_key == "select_trigger":
        comp_keys.append("el_select_trigger")
    elif comp_key == "el_select_trigger":
        comp_keys.append("select_trigger")
    comp_conf = None
    for ck in comp_keys:
        comp_conf = lib_conf.get(ck)
        if isinstance(comp_conf, dict):
            break
    if not isinstance(comp_conf, dict):
        if lib_key != "generic":
            return get_component_structure(path, "generic", component_type)
        return None
    html = (comp_conf.get("html") or "").strip()
    click_target = (comp_conf.get("click_target") or "").strip()
    if html or click_target:
        return {"html": html, "click_target": click_target}
    return None


def _split_frontmatter(text: str) -> tuple[str, str]:
    """切出 --- 包裹的 YAML frontmatter 与正文."""
    t = text.lstrip()
    if not t.startswith("---"):
        return "", text
    parts = t.split("---", 2)
    if len(parts) >= 3:
        return parts[1], parts[2]
    return "", text

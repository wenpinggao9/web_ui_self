"""步骤⑧ 语义DOM抽取 —— 给大模型一个能看懂的页面素描.

在浏览器执行 JS 遍历 DOM, 提取对 UI 自动化有意义的元素列表.
【重点2】弹窗/表单优先: 检测到弹窗(dialog)或表单(form)时, 把其内部节点排到最前,
避免被截断, 让定位/就绪检查优先看弹窗、表单内的状态.

支持框架专属选择器: 从 skill.md 的 framework_selectors 加载,
识别到对应框架后传入, 提高下拉/弹窗/表格等元素的采集成功率.
"""
from __future__ import annotations

from typing import Any, Optional

# 旧版 querySelector 快照已移除; 抽取走 dom bridge (traverse + 下拉/option 专项).

# ---- DOM 稳定等待 ----

_DOM_STABLE_JS = r"""
(quietMs) => new Promise((resolve) => {
  let timer = null;
  const obs = new MutationObserver(() => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(done, quietMs);
  });
  function done() { obs.disconnect(); resolve(true); }
  obs.observe(document.body, { childList: true, subtree: true, attributes: true });
  timer = setTimeout(done, quietMs);
})
"""


def wait_for_dom_stable(page: Any, quiet_ms: int = 200, timeout_ms: int = 3000) -> None:
    """注入变更观察器, DOM 安静 quiet_ms 后返回; 超时则放弃等待."""
    try:
        page.evaluate(f"(q) => Promise.race([{_DOM_STABLE_JS}(q), new Promise(r=>setTimeout(r,{timeout_ms}))])", quiet_ms)
    except Exception:
        pass


def _snapshot_items(
    page: Any,
    dialog_first: bool,
    stable: bool,
    selectors: Optional[dict[str, str]] = None,
    *,
    profile: str = "locate",
) -> list[dict]:
    """遍历 + iframe + 下拉/option 专项 + normalize."""
    from .v3_bridge import collect_snapshot_items
    return collect_snapshot_items(
        page, dialog_first, stable, selectors, profile=profile,
    )


def extract_items(
    page: Any,
    profile: str = "locate",
    dialog_first: bool = True,
    stable: bool = True,
    selectors: Optional[dict[str, str]] = None,
) -> list[dict]:
    """统一 DOM 抽取. profile=locate(定位) | post_verify(操作后校验/断言)."""
    return _snapshot_items(
        page, dialog_first, stable, selectors, profile=profile,
    )


def extract_semantic_dom(
    page: Any,
    limit: int = 80,
    dialog_first: bool = True,
    stable: bool = True,
    selectors: Optional[dict[str, str]] = None,
    *,
    items: Optional[list[dict]] = None,
    profile: str = "locate",
) -> str:
    """返回紧凑文本. 有弹窗/表单时把其内部元素排到最前."""
    if items is None:
        items = _snapshot_items(page, dialog_first, stable, selectors, profile=profile)
    return _render(items, limit)


class DomIndex:
    """带编号的 DOM 摘要 + 每个编号对应的选择器, 供 L3 元素决策使用."""
    def __init__(self, numbered_text: str, selectors: list[dict]) -> None:
        self.numbered_text = numbered_text
        self.selectors = selectors

    def __len__(self) -> int:
        return len(self.selectors)


def format_indexed_dom_line(i: int, it: dict) -> str:
    """带 [索引] 的单行摘要 (后校验/定位/语义断言共用)."""
    return f"[{i}] {_render_line(it)}"


def compact_dom_lines(
    items: list[dict],
    max_nodes: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> str:
    """将 semantic_items 压成带 [索引] 的多行文本 (post_verify 格式)."""
    slice_items = items if max_nodes is None else items[:max_nodes]
    lines: list[str] = []
    used = 0
    for i, it in enumerate(slice_items):
        line = format_indexed_dom_line(i, it)
        if max_chars is not None:
            add_len = len(line) + (1 if lines else 0)
            if used + add_len > max_chars:
                lines.append(
                    f"... 已达字符上限, 已输出 {len(lines)} 行, 页面共 {len(items)} 个节点"
                )
                break
            used += add_len
        lines.append(line)
    if max_nodes is not None and len(items) > max_nodes:
        lines.append(f"... 共 {len(items)} 个节点, 仅展示前 {max_nodes} 个")
    return "\n".join(lines)


def dom_index_from_items(items: list[dict], limit: int = 80) -> DomIndex:
    """从已抽取的 semantic_items 构建 L3 DomIndex (不再读页)."""
    sliced = items[:limit]
    lines: list[str] = []
    sel_list: list[dict] = []
    for i, it in enumerate(sliced):
        lines.append(format_indexed_dom_line(i, it))
        sel_list.append(build_locator_info(it))
    return DomIndex("\n".join(lines), sel_list)


def extract_semantic_items(
    page: Any,
    dialog_first: bool = True,
    stable: bool = True,
    selectors: Optional[dict[str, str]] = None,
    *,
    items: Optional[list[dict]] = None,
    profile: str = "locate",
) -> list[dict]:
    """返回原始语义 DOM 条目列表, 供 L3 纠偏脚本使用."""
    if items is not None:
        return items
    return _snapshot_items(page, dialog_first, stable, selectors, profile=profile)


def extract_dom_index(
    page: Any = None,
    limit: int = 80,
    dialog_first: bool = True,
    stable: bool = True,
    selectors: Optional[dict[str, str]] = None,
    *,
    items: Optional[list[dict]] = None,
    profile: str = "locate",
) -> DomIndex:
    """返回带编号的 DOM 摘要; 可传入 items 避免重复读页."""
    if items is None:
        if page is None:
            raise ValueError("extract_dom_index 需要 page 或 items")
        items = _snapshot_items(
            page, dialog_first, stable, selectors, profile=profile,
        )
    return dom_index_from_items(items, limit=limit)


def build_locator_info(it: dict) -> dict:
    """从元素属性构建最佳 Playwright 定位信息 (语义 API 优先)."""
    tag = it.get("tag") or "*"
    text = (it.get("text") or "").strip()
    placeholder = (it.get("placeholder") or "").strip()
    role = (it.get("role") or "").strip()
    nth = it.get("_idx", 0)
    in_dialog = bool(it.get("in_dialog"))
    frame_loc = it.get("_frame_locator")

    def _meta(info: dict) -> dict:
        if frame_loc:
            info["_frame_locator"] = frame_loc
        return info

    if tag == "option" and text:
        return _meta({
            "method": "role", "role": "option", "name": text, "exact": False,
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'role=option[name="{text}"]',
        })
    if placeholder:
        return _meta({
            "method": "placeholder", "name": placeholder, "exact": False,
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'placeholder:"{placeholder}"',
        })
    if it.get("testid"):
        tid = it["testid"]
        return _meta({
            "method": "testid", "name": tid, "nth": nth, "in_dialog": in_dialog,
            "selector": f'testid:"{tid}"',
        })
    if role in ("button", "menuitem", "tab", "link", "combobox") and text:
        return _meta({
            "method": "role", "role": role, "name": text,
            "exact": role in ("tab", "menuitem"),
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'role={role}[name="{text}"]',
        })
    cls = (it.get("class") or "").lower()
    if role == "radio" and text:
        name_escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return _meta({
            "method": "role", "role": "radio", "name": text, "exact": False,
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'role=radio[name="{name_escaped}"]',
        })
    if "ant-radio-wrapper" in cls and text:
        name_escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return _meta({
            "method": "css",
            "selector": f'label.ant-radio-wrapper:has-text("{name_escaped}")',
            "nth": nth, "in_dialog": in_dialog,
        })
    if text and tag in ("button", "a", "label", "span", "li"):
        if tag == "button":
            return _meta({
                "method": "role", "role": "button", "name": text, "exact": False,
                "nth": nth, "in_dialog": in_dialog,
                "selector": f'role=button[name="{text}"]',
            })
        if tag == "a":
            return _meta({
                "method": "role", "role": "link", "name": text, "exact": False,
                "nth": nth, "in_dialog": in_dialog,
                "selector": f'role=link[name="{text}"]',
            })
        return _meta({
            "method": "text", "name": text, "exact": True,
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'text:"{text}"',
        })

    sel = build_selector(it)
    return _meta({"method": "css", "selector": sel, "nth": nth, "in_dialog": in_dialog})


def build_selector(it: dict) -> str:
    """从元素属性按优先级构建 Playwright 选择器字符串.
    优先级: #id → [data-testid] → tag[placeholder] → tag[name] → tag:has-text → text=.
    """
    tag = it.get("tag") or "*"
    if tag == "container":
        tag = "*"
    elif tag == "dropdown":
        tag = "*"
    elif tag == "option":
        text = (it.get("text") or "").strip()
        if text:
            return f'text="{text}"'
        tag = "*"
    if it.get("id"):
        return f'#{it["id"]}'
    if it.get("testid"):
        return f'[data-testid="{it["testid"]}"]'
    if it.get("placeholder"):
        return f'{tag}[placeholder="{it["placeholder"]}"]'
    name = it.get("name")
    if name and tag in ("input", "textarea", "select"):
        return f'{tag}[name="{name}"]'
    text = (it.get("text") or "").strip()
    if text and tag in ("button", "a", "label", "span", "li"):
        return f'{tag}:has-text("{text}")'
    if text:
        return f'text="{text}"'
    role = it.get("role")
    if role and name:
        return f'[role="{role}"][aria-label="{name}"]'
    return tag


def _render(items: list[dict], limit: int) -> str:
    """把元素条目渲染成紧凑文本."""
    lines: list[str] = []
    dup_counts: dict[str, int] = {}
    for it in items:
        k = _dup_key(it)
        dup_counts[k] = dup_counts.get(k, 0) + 1
    dups = {k: v for k, v in dup_counts.items() if v > 1}
    substr_conflicts = _find_substring_conflicts(items)

    if dups or substr_conflicts:
        lines.append("⚠ 重复/冲突元素 (使用这些定位器会命中多个元素, 必须加 scope/nth/css 前缀来区分):")
        for k, cnt in dups.items():
            lines.append(f"  ×{cnt}  {k}")
        for conflict in substr_conflicts:
            lines.append(f"  ⚡ 子串冲突: {conflict}")
        lines.append("")

    for it in items[:limit]:
        lines.append(_render_line(it))
    return "\n".join(lines)


def _render_line(it: dict) -> str:
    """渲染单个元素摘要行."""
    tag = it.get("tag") or ""
    if tag == "container":
        parts = ["container(.el-select)"]
        if it.get("haspopup"):
            parts.append(f"haspopup={it['haspopup']}")
        head = " ".join(parts) + ">"
    elif tag == "dropdown":
        parts = ["dropdown"]
        if it.get("role"):
            parts.append(f"role={it['role']}")
        if it.get("id"):
            parts.append(f'id="{it["id"]}"')
        head = " ".join(parts) + ">"
    elif tag == "option":
        parts = ["option"]
        if it.get("selected"):
            parts.append("[已选]")
        head = " ".join(parts) + ">"
    else:
        parts = [f"<{it['tag']}"]
        if it.get("id"):
            parts.append(f'id="{it["id"]}"')
        for k in ("role", "name", "testid", "type", "placeholder", "haspopup"):
            v = it.get(k)
            if v:
                parts.append(f'{k}="{v}"')
        val = it.get("value")
        if val is not None and str(val).strip() and tag in ("input", "textarea", "select"):
            parts.append(f'value="{str(val).strip()[:80]}"')
        idx = it.get("_idx", 0)
        if idx > 0:
            parts.append(f"#{idx}")
        head = " ".join(parts) + ">"
    text = it.get("text") or ""
    suffix_parts = []
    if it.get("in_dialog"):
        suffix_parts.append("[弹窗]")
    elif it.get("in_form"):
        suffix_parts.append("[表单]")
    if it.get("scope"):
        suffix_parts.append(f"[scope={it['scope']}]")
    if it.get("hidden", False):
        suffix_parts.append("[hidden]")
    elif not it.get("in_viewport", True):
        suffix_parts.append("[off-screen]")
    suffix = " " + " ".join(suffix_parts) if suffix_parts else ""
    return f"{head} {text}{suffix}".rstrip()


def _dup_key(it: dict) -> str:
    """构造重复元素检测 key."""
    if it.get("tag") == "container":
        return f"container text={it.get('text','')}"
    parts = []
    for k in ("tag", "role", "name", "type", "placeholder"):
        v = it.get(k)
        if v:
            parts.append(f'{k}="{v}"')
    txt = it.get("text") or ""
    if txt:
        parts.append(f'text="{txt}"')
    return " ".join(parts)


def _find_substring_conflicts(items: list) -> list[str]:
    """检测 get_by_text/placeholder 子串匹配冲突."""
    conflicts = []
    seen_pairs = set()

    def collect_values(field: str):
        return [(it.get(field) or "", it) for it in items if (it.get(field) or "") and len(it.get(field) or "") >= 2]

    for field in ("placeholder", "name", "text"):
        values = collect_values(field)
        for i, (v1, _it1) in enumerate(values):
            for v2, _it2 in values[i + 1:]:
                if v1 == v2:
                    continue
                if v1 in v2 or v2 in v1:
                    short, long = (v1, v2) if len(v1) < len(v2) else (v2, v1)
                    pair_key = f"{field}:{short}|{long}"
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    conflicts.append(f'{field}="{short}" 会命中 {field}="{long}"')
    return conflicts

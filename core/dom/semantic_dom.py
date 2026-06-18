"""步骤⑧ 语义DOM抽取 —— 给大模型一个能看懂的页面素描.

在浏览器执行 JS 遍历 DOM, 提取对 UI 自动化有意义的元素列表.
【重点2】弹窗/表单优先: 检测到弹窗(dialog)或表单(form)时, 把其内部节点排到最前,
避免被截断, 让定位/就绪检查优先看弹窗、表单内的状态.

支持框架专属选择器: 从 skill.md 的 framework_selectors 加载,
识别到对应框架后传入, 提高下拉/弹窗/表格等元素的采集成功率.
"""
from __future__ import annotations

from typing import Any, Optional

from .parent_index import attach_parent_indices

# JS 模板, 占位符会被实际选择器替换
_SNAPSHOT_JS_TEMPLATE = """
() => {
  const SEL = [
    'a,button,input,textarea,select,[role],[data-testid]',
    '[onclick]',
    '[tabindex="0"],[tabindex="-1"]',
    'label',
    'summary',
  ].join(',');
  const CONTAINER_SEL = '__CONTAINER_SEL__';
  const DROPDOWN_SEL = '__DROPDOWN_SEL__';
  const OPTION_SEL = '__OPTION_SEL__';
  const DIALOG_SEL = '__DIALOG_SEL__';
  const FORM_SEL = '__FORM_SEL__';
  const items = [];
  const seen = new Map();

  function closestMatch(el, sel) {
    let cur = el;
    while (cur && cur !== document.body) {
      if (cur.matches && cur.matches(sel)) return true;
      cur = cur.parentElement;
    }
    return false;
  }

  function findScope(el) {
    let cur = el.parentElement;
    let firstClass = '';
    while (cur && cur !== document.body) {
      if (cur.id && !cur.id.startsWith('el-id-')) return '#' + cur.id;
      const role = cur.getAttribute('role');
      if (role && ['tabpanel','dialog','menu','listbox','navigation','region','form'].includes(role)) {
        const id = (cur.id && !cur.id.startsWith('el-id-')) ? cur.id : '';
        const label = cur.getAttribute('aria-label') || cur.getAttribute('aria-labelledby') || '';
        if (id) return '#' + id;
        return `[role=${role}${label ? ' aria-label="' + label + '"' : ''}]`;
      }
      if (!firstClass && cur.className && typeof cur.className === 'string') {
        const classes = cur.className.trim().split(/\s+/).filter(c => c.length > 2);
        if (classes.length > 0) firstClass = '.' + classes.slice(0, 2).join('.');
      }
      cur = cur.parentElement;
    }
    return firstClass;
  }

  function isVisible(el) { return !!el.tagName; }
  function isInViewport(el) {
    const rect = el.getBoundingClientRect();
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const vw = window.innerWidth || document.documentElement.clientWidth;
    return rect.top >= 0 && rect.left >= 0 && rect.bottom <= vh && rect.right <= vw;
  }
  function isHidden(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return true;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return true;
    if (el.offsetParent === null && el.tagName.toLowerCase() !== 'body') return true;
    return false;
  }

  function parentElementId(el) {
    let p = el.parentElement;
    while (p && p !== document.body) {
      if (p.id) return p.id;
      p = p.parentElement;
    }
    return '';
  }

  document.querySelectorAll(SEL).forEach(el => {
    if (!isVisible(el)) return;
    const item = {
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      name: el.getAttribute('aria-label') || el.getAttribute('name') || '',
      id: (el.id && !el.id.startsWith('el-id-')) ? el.id : '',
      _id: el.id || '',
      _parent_id: parentElementId(el),
      testid: el.getAttribute('data-testid') || '',
      text: (el.innerText || el.value || '').trim().slice(0, 40),
      placeholder: el.getAttribute('placeholder') || '',
      type: el.getAttribute('type') || '',
      haspopup: el.getAttribute('aria-haspopup') || '',
      class: (el.className && typeof el.className === 'string') ? el.className.slice(0, 160) : '',
      readOnly: !!el.readOnly,
      zIndex: (() => {
        let cur = el, z = null;
        while (cur && cur !== document.body) {
          const st = window.getComputedStyle(cur);
          if (st && st.zIndex && st.zIndex !== 'auto') { z = st.zIndex; break; }
          cur = cur.parentElement;
        }
        return z;
      })(),
      scope: findScope(el),
      in_viewport: isInViewport(el),
      hidden: isHidden(el),
      in_dialog: closestMatch(el, DIALOG_SEL),
      in_form: closestMatch(el, FORM_SEL),
    };
    const key = JSON.stringify({
      tag: item.tag, role: item.role, placeholder: item.placeholder,
      text: item.text, scope: item.scope, id: item.id,
    });
    const count = seen.get(key) || 0;
    if (count >= 3) return;
    seen.set(key, count + 1);
    item._idx = count;
    items.push(item);
  });

  document.querySelectorAll(CONTAINER_SEL).forEach(el => {
    if (!isVisible(el)) return;
    let innerId = '';
    const innerInput = el.querySelector('input');
    if (innerInput) innerId = innerInput.id || '';
    const item = {
      tag: 'container', role: el.getAttribute('role') || '', name: '',
      id: (el.id && !el.id.startsWith('el-id-')) ? el.id : '',
      _id: el.id || '', _parent_id: parentElementId(el), testid: el.getAttribute('data-testid') || '',
      text: (el.innerText || '').trim().slice(0, 40),
      placeholder: '', type: '', haspopup: el.getAttribute('aria-haspopup') || '',
      class: (el.className && typeof el.className === 'string') ? el.className.slice(0, 160) : '',
      readOnly: false,
      zIndex: null,
      scope: findScope(el), inner_id: innerId,
      in_viewport: isInViewport(el), hidden: isHidden(el),
      in_dialog: closestMatch(el, DIALOG_SEL), in_form: closestMatch(el, FORM_SEL),
    };
    const key = 'container:' + JSON.stringify({tag: 'container', text: item.text, scope: item.scope});
    const count = seen.get(key) || 0;
    if (count >= 3) return;
    seen.set(key, count + 1);
    item._idx = count;
    items.push(item);
  });

  document.querySelectorAll(DROPDOWN_SEL).forEach(el => {
    if (!isVisible(el)) return;
    const item = {
      tag: 'dropdown', role: el.getAttribute('role') || '', name: '',
      id: (el.id && !el.id.startsWith('el-id-')) ? el.id : '',
      _id: el.id || '', _parent_id: parentElementId(el), testid: el.getAttribute('data-testid') || '',
      text: (el.innerText || '').trim().slice(0, 80),
      placeholder: '', type: '', haspopup: '',
      class: (el.className && typeof el.className === 'string') ? el.className.slice(0, 160) : '',
      readOnly: false,
      zIndex: null,
      scope: findScope(el),
      in_viewport: isInViewport(el), hidden: isHidden(el),
      in_dialog: closestMatch(el, DIALOG_SEL), in_form: closestMatch(el, FORM_SEL),
    };
    const key = 'dropdown:' + JSON.stringify({role: item.role, id: item.id});
    const count = seen.get(key) || 0;
    if (count >= 2) return;
    seen.set(key, count + 1);
    item._idx = count;
    items.push(item);
  });

  document.querySelectorAll(OPTION_SEL).forEach(el => {
    if (!isVisible(el)) return;
    const item = {
      tag: 'option', role: el.getAttribute('role') || '', name: '',
      id: (el.id && !el.id.startsWith('el-id-')) ? el.id : '',
      _id: el.id || '', _parent_id: parentElementId(el), testid: el.getAttribute('data-testid') || '',
      text: (el.innerText || el.textContent || '').trim().slice(0, 40),
      placeholder: '', type: '', haspopup: '',
      class: (el.className && typeof el.className === 'string') ? el.className.slice(0, 160) : '',
      readOnly: false,
      zIndex: null,
      scope: findScope(el),
      in_viewport: isInViewport(el), hidden: isHidden(el),
      in_dialog: closestMatch(el, DIALOG_SEL), in_form: closestMatch(el, FORM_SEL),
      selected: el.getAttribute('aria-selected') === 'true' || el.classList.contains('is-selected') || el.classList.contains('ant-select-item-option-selected'),
    };
    const key = 'option:' + JSON.stringify({tag: 'option', text: item.text, role: item.role});
    const count = seen.get(key) || 0;
    if (count >= 5) return;
    seen.set(key, count + 1);
    item._idx = count;
    items.push(item);
  });

  const hasDialog = document.querySelector(DIALOG_SEL) !== null;
  return { items: items, has_dialog: hasDialog };
}
"""


# 默认选择器 (兼容所有框架的通用兜底)
_DEFAULT_SELECTORS = {
    'container_sel': '.el-select, .ant-select, [aria-haspopup]',
    'dropdown_sel': '.ant-select-dropdown, .el-select-dropdown, [role="listbox"], [role="menu"]',
    'option_sel': '.ant-select-item-option, .el-select-dropdown__item, [role="option"]',
    'dialog_sel': '[role="dialog"], .el-dialog, .el-message-box, .ant-modal, .el-drawer',
    'form_sel': 'form, .el-form, .ant-form',
}


def _build_snapshot_js(
    container_sel: str = None,
    dropdown_sel: str = None,
    option_sel: str = None,
    dialog_sel: str = None,
    form_sel: str = None,
) -> str:
    """构建 DOM 快照 JS 脚本, 支持注入框架专属选择器."""
    sels = {
        '__CONTAINER_SEL__': container_sel or _DEFAULT_SELECTORS['container_sel'],
        '__DROPDOWN_SEL__': dropdown_sel or _DEFAULT_SELECTORS['dropdown_sel'],
        '__OPTION_SEL__': option_sel or _DEFAULT_SELECTORS['option_sel'],
        '__DIALOG_SEL__': dialog_sel or _DEFAULT_SELECTORS['dialog_sel'],
        '__FORM_SEL__': form_sel or _DEFAULT_SELECTORS['form_sel'],
    }
    js = _SNAPSHOT_JS_TEMPLATE
    for key, val in sels.items():
        js = js.replace(key, val.replace('"', '\\"'))
    return js


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
) -> list[dict]:
    """执行浏览器侧快照脚本, 返回原始元素条目列表."""
    if stable:
        wait_for_dom_stable(page)
    sels = selectors or _DEFAULT_SELECTORS
    js = _build_snapshot_js(**sels)
    try:
        payload = page.evaluate(js) or {}
    except Exception:
        return []
    items = payload.get("items") or []
    has_dialog = payload.get("has_dialog", False)
    if dialog_first:
        items = _reorder_dialog_form_first(items, has_dialog)
    attach_parent_indices(items)
    return items


def extract_semantic_dom(
    page: Any,
    limit: int = 80,
    dialog_first: bool = True,
    stable: bool = True,
    selectors: Optional[dict[str, str]] = None,
) -> str:
    """返回紧凑文本. 有弹窗/表单时把其内部元素排到最前."""
    items = _snapshot_items(page, dialog_first, stable, selectors)
    return _render(items, limit)


class DomIndex:
    """带编号的 DOM 摘要 + 每个编号对应的选择器, 供 L5 元素决策使用."""
    def __init__(self, numbered_text: str, selectors: list[dict]) -> None:
        self.numbered_text = numbered_text
        self.selectors = selectors

    def __len__(self) -> int:
        return len(self.selectors)


def extract_semantic_items(
    page: Any,
    dialog_first: bool = True,
    stable: bool = True,
    selectors: Optional[dict[str, str]] = None,
) -> list[dict]:
    """返回原始语义 DOM 条目列表, 供 L5 纠偏脚本使用."""
    return _snapshot_items(page, dialog_first, stable, selectors)


def extract_dom_index(
    page: Any,
    limit: int = 80,
    dialog_first: bool = True,
    stable: bool = True,
    selectors: Optional[dict[str, str]] = None,
) -> DomIndex:
    """返回带编号的 DOM 摘要, 每行前缀 [i], 并给出第 i 个元素的选择器."""
    items = _snapshot_items(page, dialog_first, stable, selectors)[:limit]
    lines: list[str] = []
    sel_list: list[dict] = []
    for i, it in enumerate(items):
        lines.append(f"[{i}] {_render_line(it)}")
        sel_list.append(build_locator_info(it))
    return DomIndex("\n".join(lines), sel_list)


def build_locator_info(it: dict) -> dict:
    """从元素属性构建最佳 Playwright 定位信息 (语义 API 优先)."""
    tag = it.get("tag") or "*"
    text = (it.get("text") or "").strip()
    placeholder = (it.get("placeholder") or "").strip()
    role = (it.get("role") or "").strip()
    nth = it.get("_idx", 0)
    in_dialog = bool(it.get("in_dialog"))

    if tag == "option" and text:
        return {
            "method": "role", "role": "option", "name": text, "exact": False,
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'role=option[name="{text}"]',
        }
    if placeholder:
        return {
            "method": "placeholder", "name": placeholder, "exact": False,
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'placeholder:"{placeholder}"',
        }
    if it.get("testid"):
        tid = it["testid"]
        return {
            "method": "testid", "name": tid, "nth": nth, "in_dialog": in_dialog,
            "selector": f'testid:"{tid}"',
        }
    if role in ("button", "menuitem", "tab", "link", "combobox") and text:
        return {
            "method": "role", "role": role, "name": text,
            "exact": role in ("tab", "menuitem"),
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'role={role}[name="{text}"]',
        }
    if text and tag in ("button", "a", "label", "span", "li"):
        if tag == "button":
            return {
                "method": "role", "role": "button", "name": text, "exact": False,
                "nth": nth, "in_dialog": in_dialog,
                "selector": f'role=button[name="{text}"]',
            }
        if tag == "a":
            return {
                "method": "role", "role": "link", "name": text, "exact": False,
                "nth": nth, "in_dialog": in_dialog,
                "selector": f'role=link[name="{text}"]',
            }
        return {
            "method": "text", "name": text, "exact": True,
            "nth": nth, "in_dialog": in_dialog,
            "selector": f'text:"{text}"',
        }

    sel = build_selector(it)
    return {"method": "css", "selector": sel, "nth": nth, "in_dialog": in_dialog}


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


def _reorder_dialog_form_first(items: list[dict], has_dialog: bool) -> list[dict]:
    """弹窗 > 表单 > 其它."""
    def rank(it: dict) -> int:
        if it.get("in_dialog"):
            return 0
        if it.get("in_form"):
            return 1
        return 2
    return sorted(items, key=rank)


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

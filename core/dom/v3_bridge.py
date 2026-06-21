"""语义 DOM 抽取桥接: 遍历脚本 + iframe + normalize + 下拉/option 专项补充."""
from __future__ import annotations

from typing import Any, Optional

from .parent_index import attach_parent_indices
from .semantic_dom import wait_for_dom_stable
from .v3_scripts import V3_LOCATE_SCRIPT, V3_POST_VERIFY_SCRIPT

MAX_TEXT_LENGTH = 200

_DEFAULT_SELECTORS = {
    "container_sel": ".el-select, .ant-select, [aria-haspopup]",
    "dropdown_sel": ".ant-select-dropdown, .el-select-dropdown, [role=\"listbox\"], [role=\"menu\"]",
    "option_sel": ".ant-select-item-option, .el-select-dropdown__item, [role=\"option\"]",
    "dialog_sel": '[role="dialog"], .el-dialog, .el-message-box, .ant-modal, .el-drawer',
    "form_sel": "form, .el-form, .ant-form",
}

_IFRAME_FRAME_LOCATOR_JS = """el => {
    if (el.id && el.id.trim()) return '#' + el.id;
    if (el.name && el.name.trim()) return 'iframe[name="' + el.name.replace(/"/g, '\\\\"') + '"]';
    if (el.src) {
        const s = (el.src || '').split('?')[0];
        const parts = s.split('/').filter(Boolean);
        const part = parts.length > 0 ? parts[parts.length - 1].slice(0, 30) : null;
        if (part && part.length > 1) return 'iframe[src*="' + part.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"') + '"]';
    }
    return null;
}"""

_COMPONENT_SUPPLEMENT_TEMPLATE = """
() => {
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
        const classes = cur.className.trim().split(/\\s+/).filter(c => c.length > 2);
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

  document.querySelectorAll(CONTAINER_SEL).forEach(el => {
    if (!isVisible(el)) return;
    let innerId = '';
    const innerInput = el.querySelector('input');
    if (innerInput) innerId = innerInput.id || '';
    const item = {
      tag: 'container', role: el.getAttribute('role') || '', name: '',
      id: (el.id && !el.id.startsWith('el-id-')) ? el.id : '',
      _id: el.id || '', _parent_id: parentElementId(el), testid: el.getAttribute('data-testid') || '',
      text: (el.innerText || '').trim().slice(0, 200),
      value: '', placeholder: '', type: '', haspopup: el.getAttribute('aria-haspopup') || '',
      class: (el.className && typeof el.className === 'string') ? el.className.slice(0, 160) : '',
      readOnly: false, zIndex: null,
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
      text: (el.innerText || '').trim().slice(0, 200),
      placeholder: '', type: '', haspopup: '',
      class: (el.className && typeof el.className === 'string') ? el.className.slice(0, 160) : '',
      readOnly: false, zIndex: null, scope: findScope(el),
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
      text: (el.innerText || el.textContent || '').trim().slice(0, 200),
      value: (el.value || '').trim().slice(0, 80),
      placeholder: '', type: '', haspopup: '',
      class: (el.className && typeof el.className === 'string') ? el.className.slice(0, 160) : '',
      readOnly: false, zIndex: null, scope: findScope(el),
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


def _build_component_supplement_js(selectors: Optional[dict[str, str]] = None) -> str:
    sels = {**_DEFAULT_SELECTORS, **(selectors or {})}
    js = _COMPONENT_SUPPLEMENT_TEMPLATE
    mapping = {
        "__CONTAINER_SEL__": sels["container_sel"],
        "__DROPDOWN_SEL__": sels["dropdown_sel"],
        "__OPTION_SEL__": sels["option_sel"],
        "__DIALOG_SEL__": sels["dialog_sel"],
        "__FORM_SEL__": sels["form_sel"],
    }
    for key, val in mapping.items():
        js = js.replace(key, val.replace('"', '\\"'))
    return js


def normalize_v3_item(raw: dict) -> dict:
    """原始遍历条目 → 本项目 semantic_items 字段."""
    aria = raw.get("aria") if isinstance(raw.get("aria"), dict) else {}
    el_id = str(raw.get("id") or "")
    pub_id = el_id if el_id and not el_id.startswith("el-id-") else ""
    name = (
        raw.get("name")
        or raw.get("aria-label")
        or aria.get("aria-label")
        or ""
    )
    tag = (raw.get("tag") or "").lower()
    item: dict[str, Any] = {
        "tag": tag,
        "role": raw.get("role") or "",
        "name": str(name),
        "id": pub_id,
        "_id": el_id,
        "_parent_id": str(raw.get("_parentId") or raw.get("_parent_id") or ""),
        "testid": raw.get("testId") or raw.get("testid") or "",
        "text": str(raw.get("text") or "").strip()[:MAX_TEXT_LENGTH],
        "value": str(raw.get("value") or "").strip()[:80],
        "placeholder": raw.get("placeholder") or "",
        "type": raw.get("type") or "",
        "haspopup": raw.get("haspopup") or "",
        "class": str(raw.get("class") or "")[:160],
        "readOnly": bool(raw.get("readOnly")),
        "zIndex": raw.get("zIndex"),
        "scope": raw.get("scope") or "",
        "in_viewport": raw.get("in_viewport", True),
        "hidden": raw.get("hidden", False),
        "in_dialog": bool(raw.get("in_dialog")),
        "in_form": bool(raw.get("in_form")),
    }
    fl = raw.get("_frame_locator")
    if fl:
        item["_frame_locator"] = fl
    if raw.get("inner_id"):
        item["inner_id"] = raw.get("inner_id")
    if raw.get("selected") is not None:
        item["selected"] = raw.get("selected")
    return item


def _item_dedup_key(it: dict) -> tuple:
    return (
        it.get("tag"), it.get("id"), it.get("role"),
        it.get("text"), it.get("placeholder"), it.get("value"), it.get("scope"),
    )


def _merge_item_lists(primary: list[dict], extra: list[dict]) -> list[dict]:
    seen = {_item_dedup_key(it) for it in primary}
    out = list(primary)
    for it in extra:
        k = _item_dedup_key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _assign_dup_indices(items: list[dict]) -> None:
    counts: dict[tuple, int] = {}
    for it in items:
        k = _item_dedup_key(it)
        n = counts.get(k, 0)
        it["_idx"] = n
        counts[k] = n + 1


def _reorder_dialog_form_first(items: list[dict], has_dialog: bool) -> list[dict]:
    def rank(it: dict) -> int:
        if it.get("in_dialog"):
            return 0
        if it.get("in_form"):
            return 1
        return 2
    if not has_dialog and not any(it.get("in_form") for it in items):
        return items
    return sorted(items, key=rank)


def _evaluate_v3(frame: Any, profile: str) -> list[dict]:
    script = V3_POST_VERIFY_SCRIPT if profile == "post_verify" else V3_LOCATE_SCRIPT
    try:
        raw = frame.evaluate(script) or []
    except Exception:
        return []
    return raw if isinstance(raw, list) else []


def _evaluate_component(frame: Any, supplement_js: str) -> tuple[list[dict], bool]:
    try:
        payload = frame.evaluate(supplement_js) or {}
    except Exception:
        return [], False
    if not isinstance(payload, dict):
        return [], False
    items = payload.get("items") or []
    return (items if isinstance(items, list) else []), bool(payload.get("has_dialog"))


def collect_snapshot_items(
    page: Any,
    dialog_first: bool,
    stable: bool,
    selectors: Optional[dict[str, str]] = None,
    *,
    profile: str = "locate",
) -> list[dict]:
    """遍历 + iframe + 下拉/option 专项 + normalize."""
    if stable:
        wait_for_dom_stable(page)
    supplement_js = _build_component_supplement_js(selectors)
    merged: list[dict] = []
    has_dialog = False

    def absorb(frame: Any, frame_locator: Optional[str] = None) -> None:
        nonlocal has_dialog
        for raw in _evaluate_v3(frame, profile):
            item = normalize_v3_item(raw)
            if frame_locator:
                item["_frame_locator"] = frame_locator
            merged.append(item)
        comp_items, comp_dialog = _evaluate_component(frame, supplement_js)
        if comp_dialog:
            has_dialog = True
        for it in comp_items:
            if frame_locator:
                it["_frame_locator"] = frame_locator
        merged[:] = _merge_item_lists(merged, comp_items)

    absorb(page.main_frame if hasattr(page, "main_frame") else page)
    main_frame = getattr(page, "main_frame", None)
    child_frames = getattr(page, "frames", None) or []
    iframe_nth = 0
    for frame in child_frames:
        if main_frame is not None and frame == main_frame:
            continue
        frame_locator: Optional[str] = None
        try:
            iframe_el = frame.frame_element()
            frame_locator = iframe_el.evaluate(_IFRAME_FRAME_LOCATOR_JS)
        except Exception:
            pass
        if not frame_locator:
            frame_locator = f"iframe >> nth={iframe_nth}"
        iframe_nth += 1
        absorb(frame, frame_locator)

    _assign_dup_indices(merged)
    if dialog_first:
        merged = _reorder_dialog_form_first(merged, has_dialog)
    attach_parent_indices(merged)
    return merged

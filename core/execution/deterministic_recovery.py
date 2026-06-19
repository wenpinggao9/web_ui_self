"""V3 风格确定性恢复: LLM readiness 之前补 fill / radio / 必填检测."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..planning import PlannedAction

_OPTION_INTENT_RE = re.compile(r"选择|点击.*(?:单选|radio|选项|审核原因|原因)", re.I)
_SUBMIT_WORDS = ("提交", "保存", "确定", "确认", "登录", "下一步", "结算", "立即支付", "支付", "完成")
_AUDIT_REASON_HINTS = (
    "不良导向", "敏感信息", "非大学", "非目标", "题目不完整", "多题", "水印", "无任何问题",
)

_DETECT_UNFILLED_JS = r"""
(maxItems) => {
  const out = [];
  const seen = new Set();
  const controls = Array.from(document.querySelectorAll(
    'input,textarea,select,[role="combobox"]'
  ));
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g,' ').trim();
  const firstText = (...vals) => {
    for (const v of vals) { if (v && String(v).trim()) return String(v).trim(); }
    return '';
  };
  const pseudoContentIsRequiredStar = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const starCodes = new Set([0x002a, 0xff0a, 0x2731, 0x2605, 0x22c6, 0xfe61]);
    try {
      for (const pseudo of ['::before', '::after']) {
        const raw = window.getComputedStyle(el, pseudo).content;
        if (!raw || raw === 'none' || raw === 'normal') continue;
        const s = raw.replace(/^["']|["']$/g, '').trim();
        if (!s) continue;
        if (s === '*') return true;
        const cp = s.codePointAt(0);
        if (s.length <= 2 && starCodes.has(cp)) return true;
      }
    } catch (e) {}
    return false;
  };
  const formItemLooksRequired = (formItem) => {
    if (!formItem) return false;
    const formCls = (formItem.className || '').toString();
    if (/required|is-required|ant-form-item-required|el-form-item--required/i.test(formCls)) return true;
    for (const sel of ['.el-form-item__label', 'label', '.ant-form-item-label']) {
      const nodes = formItem.querySelectorAll(sel);
      for (const lb of nodes) {
        if (!lb) continue;
        const t = text(lb);
        if (t && /\*/.test(t)) return true;
        if (pseudoContentIsRequiredStar(lb)) return true;
      }
    }
    return false;
  };
  const isRequired = (el) => {
    if (!el) return false;
    const ariaReq = (el.getAttribute('aria-required') || '').toLowerCase();
    if (ariaReq === 'true' || ariaReq === '1') return true;
    if (el.required) return true;
    const cls = (el.className || '').toString();
    if (/required|is-required|ant-form-item-required/i.test(cls)) return true;
    const formItem = el.closest('.el-form-item, .ant-form-item, .form-item, [class*="form-item"]');
    if (formItem && formItemLooksRequired(formItem)) return true;
    return false;
  };
  const isFilled = (el) => {
    if (!el) return true;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden' || el.disabled || el.readOnly) return true;
    if (type === 'checkbox' || type === 'radio') return !!el.checked;
    let v = '';
    if ('value' in el) v = String(el.value || '').trim();
    if (!v && (el.tagName || '').toLowerCase() === 'select') {
      v = String(el.options?.[el.selectedIndex]?.value || '').trim();
    }
    return !!v;
  };
  const resolveLabel = (el) => {
    const formItem = el.closest('.el-form-item, .ant-form-item, .form-item, [class*="form-item"]');
    const lb = formItem ? formItem.querySelector('label,.el-form-item__label,.ant-form-item-label') : null;
    return firstText(
      text(lb).replace('*', '').trim(),
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('name'),
      el.id,
      (el.tagName || '').toLowerCase()
    );
  };
  for (const el of controls) {
    if (!isRequired(el) || isFilled(el)) continue;
    const key = resolveLabel(el);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(key);
    if (out.length >= maxItems) break;
  }
  return out;
}
"""

_RADIO_CHECK_JS = r"""
(labelText) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const target = norm(labelText);
  if (!target) return { checked: false, found: false };
  const matchText = (t) => {
    const n = norm(t);
    if (!n) return false;
    return n.includes(target) || target.includes(n);
  };
  for (const radio of document.querySelectorAll('input[type="radio"]')) {
    const wrap = radio.closest('.ant-radio-wrapper, .el-radio, label, [class*="radio"]');
    const labelEl = wrap || radio.parentElement;
    const t = norm(labelEl ? labelEl.innerText : '');
    if (!matchText(t)) continue;
    return { checked: !!radio.checked, found: true, text: t };
  }
  for (const wrap of document.querySelectorAll('.ant-radio-wrapper, .el-radio, label')) {
    const t = norm(wrap.innerText || '');
    if (!matchText(t)) continue;
    const radio = wrap.querySelector('input[type="radio"]');
    if (radio) return { checked: !!radio.checked, found: true, text: t };
  }
  return { checked: false, found: false };
}
"""

_RADIO_CLICK_JS = r"""
(labelText) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const target = norm(labelText);
  if (!target) return { ok: false, reason: 'empty_label' };
  const matchText = (t) => {
    const n = norm(t);
    if (!n) return false;
    return n.includes(target) || target.includes(n);
  };
  const tryClick = (el) => {
    if (!el) return false;
    el.click();
    return true;
  };
  for (const wrap of document.querySelectorAll('.ant-radio-wrapper, .el-radio, label')) {
    const t = norm(wrap.innerText || '');
    if (!matchText(t)) continue;
    const radio = wrap.querySelector('input[type="radio"]');
    if (radio && radio.checked) return { ok: true, already: true, text: t };
    if (tryClick(wrap.closest('.ant-radio-wrapper') || wrap)) {
      return { ok: true, clicked: true, text: t };
    }
  }
  for (const radio of document.querySelectorAll('input[type="radio"]')) {
    const wrap = radio.closest('.ant-radio-wrapper, .el-radio, label');
    const t = norm(wrap ? wrap.innerText : '');
    if (!matchText(t)) continue;
    if (radio.checked) return { ok: true, already: true, text: t };
    if (tryClick(wrap || radio)) return { ok: true, clicked: true, text: t };
  }
  return { ok: false, reason: 'not_found' };
}
"""


@dataclass
class DeterministicRecoveryResult:
    messages: list[str] = field(default_factory=list)
    fill_recovered: int = 0
    radio_recovered: bool = False


def detect_unfilled_required_fields(page: Any, max_items: int = 8) -> list[str]:
    try:
        rows = page.evaluate(_DETECT_UNFILLED_JS, int(max_items))
        if not isinstance(rows, list):
            return []
        return [str(x).strip() for x in rows if str(x or "").strip()]
    except Exception:
        return []


def _fill_key(item: dict) -> str:
    ph = (item.get("placeholder") or "").strip()
    name = (item.get("name") or "").strip()
    fid = (item.get("id") or "").strip()
    al = (item.get("ariaLabel") or "").strip()
    if ph:
        return f"ph:{ph}"
    if name:
        return f"n:{name}"
    if fid:
        return f"id:{fid}"
    if al:
        return f"al:{al}"
    return ""


def _build_recovery_selector(field: dict) -> Optional[str]:
    ph = (field.get("placeholder") or "").strip()
    name = (field.get("name") or "").strip()
    fid = (field.get("id") or "").strip()
    if ph:
        return f'[placeholder="{ph}"]'
    if name:
        return f'[name="{name}"]'
    if fid:
        return f"#{fid}"
    return None


def recover_lost_fill_values(page: Any, fill_history: list[dict], console: Any = None) -> int:
    """基于 fill 历史重填被 Vue 重渲染清空的输入框."""
    if not fill_history:
        return 0
    expected_map: dict[str, str] = {}
    for item in fill_history:
        key = _fill_key(item)
        if key:
            expected_map[key] = str(item.get("value") or "")

    if not expected_map:
        return 0

    try:
        current_fields = page.evaluate(
            """(fillKeys) => {
                const keySet = new Set(fillKeys);
                const results = [];
                document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]),textarea'
                ).forEach(el => {
                    const ph = (el.getAttribute('placeholder') || '').trim();
                    const name = (el.getAttribute('name') || '').trim();
                    const id = (el.id || '').trim();
                    const al = (el.getAttribute('aria-label') || '').trim();
                    const parts = [];
                    if (ph) parts.push('ph:' + ph);
                    if (name) parts.push('n:' + name);
                    if (id) parts.push('id:' + id);
                    if (al) parts.push('al:' + al);
                    const key = parts.join('|') || null;
                    if (key && keySet.has(key)) {
                        results.push({
                            key, placeholder: ph, name, id,
                            value: (el.value || '').trim(),
                        });
                    }
                });
                return results;
            }""",
            list(expected_map.keys()),
        )
    except Exception:
        return 0

    recovered = 0
    for field in current_fields or []:
        key = field.get("key", "")
        if key not in expected_map:
            continue
        expected = expected_map[key]
        current = field.get("value", "")
        if current == expected:
            continue
        if current and expected and not expected.startswith(current):
            continue
        sel = _build_recovery_selector(field)
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="attached", timeout=2000)
            loc.fill(expected)
            loc.evaluate("""el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""")
            page.wait_for_timeout(150)
            recovered += 1
            if console:
                console.print(
                    f"  [cyan]↺ fill恢复[/cyan] {key} → {expected[:40]!r}"
                )
        except Exception:
            continue
    return recovered


def _is_submit_action(action: PlannedAction) -> bool:
    return action.type == "click" and any(w in (action.intent or "") for w in _SUBMIT_WORDS)


def extract_label_from_intent(intent: str) -> str:
    m = re.search(r"[「']([^」']+)[」']", intent or "")
    if m:
        return m.group(1).strip()
    for hint in _AUDIT_REASON_HINTS:
        if hint in (intent or ""):
            return hint
    return ""


def is_option_selection_click(action: PlannedAction, label: str) -> bool:
    intent = action.intent or ""
    if _OPTION_INTENT_RE.search(intent):
        return True
    if label and any(h in label or h in intent for h in _AUDIT_REASON_HINTS):
        return True
    return False


def resolve_expected_radio_label(
    *,
    last_click_label: Optional[str],
    api_context: dict[str, Any],
    prior_actions: list[PlannedAction],
    case_steps: list[str],
    case_notes: list[str],
) -> Optional[str]:
    reason = str(api_context.get("reason") or "").strip()
    if reason:
        return reason
    if last_click_label and any(h in last_click_label for h in _AUDIT_REASON_HINTS):
        return last_click_label
    for act in reversed(prior_actions):
        if act.type != "click" or getattr(act, "is_recovery", False):
            continue
        label = extract_label_from_intent(act.intent or "")
        if label:
            return label
        if act.value:
            return str(act.value).strip()
    for line in case_steps:
        label = extract_label_from_intent(line)
        if label and ("选择" in line or "点击" in line):
            return label
    for note in case_notes:
        m = re.search(r"「([^」]+(?:不良导向|敏感信息)[^」]*)」", note)
        if m:
            return m.group(1).strip()
    return None


def recover_expected_radio(page: Any, label: str, console: Any = None) -> bool:
    if not label:
        return False
    try:
        state = page.evaluate(_RADIO_CHECK_JS, label) or {}
        if state.get("checked"):
            return False
        if not state.get("found"):
            pass  # 仍尝试 click
        result = page.evaluate(_RADIO_CLICK_JS, label) or {}
        if not result.get("ok"):
            return False
        if console:
            if result.get("already"):
                console.print(f"  [dim]↺ radio已选中[/dim] {label[:40]}")
            else:
                console.print(f"  [cyan]↺ radio恢复[/cyan] 选择 {label[:60]}")
        return bool(result.get("clicked"))
    except Exception:
        return False


def run_deterministic_pre_readiness(
    dispatcher: Any,
    next_action: PlannedAction,
    *,
    prior_actions: Optional[list[PlannedAction]] = None,
    case_steps: Optional[list[str]] = None,
    case_notes: Optional[list[str]] = None,
) -> DeterministicRecoveryResult:
    """LLM readiness 前执行确定性恢复."""
    page = dispatcher.page
    console = getattr(dispatcher, "console", None)
    result = DeterministicRecoveryResult()
    prior = prior_actions or []

    n = recover_lost_fill_values(page, getattr(dispatcher, "_fill_history", []), console)
    result.fill_recovered = n
    if n:
        result.messages.append(f"重填 {n} 个丢失输入")

    if _is_submit_action(next_action):
        expected = resolve_expected_radio_label(
            last_click_label=getattr(dispatcher, "_last_click_label", None),
            api_context=getattr(dispatcher, "api_context", {}) or {},
            prior_actions=prior,
            case_steps=case_steps or [],
            case_notes=case_notes or [],
        )
        if expected:
            if recover_expected_radio(page, expected, console):
                result.radio_recovered = True
                result.messages.append(f"补选 radio: {expected[:40]}")

    return result


def record_fill_history(dispatcher: Any, meta: dict, value: str) -> None:
    if not hasattr(dispatcher, "_fill_history"):
        dispatcher._fill_history = []
    entry = {
        "placeholder": meta.get("placeholder") or "",
        "name": meta.get("name") or "",
        "id": meta.get("id") or "",
        "ariaLabel": meta.get("ariaLabel") or "",
        "value": value,
    }
    key = _fill_key(entry)
    if not key:
        return
    history: list[dict] = dispatcher._fill_history
    for i, item in enumerate(history):
        if _fill_key(item) == key:
            history[i] = entry
            return
    history.append(entry)


def remember_option_click(dispatcher: Any, action: PlannedAction, label: str) -> None:
    if not label:
        return
    if is_option_selection_click(action, label):
        dispatcher._expected_radio_label = label


__all__ = [
    "DeterministicRecoveryResult",
    "detect_unfilled_required_fields",
    "recover_lost_fill_values",
    "run_deterministic_pre_readiness",
    "record_fill_history",
    "remember_option_click",
    "resolve_expected_radio_label",
    "is_option_selection_click",
]

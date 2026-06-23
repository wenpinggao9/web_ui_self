"""通用断言作用域 —— 从 intent 解析校验范围, 从 DOM 划分区域, 做结构化文本断言.

不绑定具体业务文案; 业务等价关系由语义断言 (prompts/semantic_assert.system.md) 兜底.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

_ALL_ROWS_RE = re.compile(r"所有|每条|每个|全部")
_LIST_NEGATE_RE = re.compile(r"不存在|不含|没有|勿出现|不出现")
_SCOPE_HEADER_RE = re.compile(r"最上方|顶部|页头|标题区|标题|头部")
_SCOPE_LEFT_RE = re.compile(r"左侧|左边")
_SCOPE_RIGHT_RE = re.compile(r"右侧|右边")
_SCOPE_MAIN_RE = re.compile(r"主内容|内容区|详情区")
_SCOPE_FORM_RE = re.compile(r"表单|选项区|选项")
_SCOPE_TABLE_RE = re.compile(r"列表|表格|任务")
_POSITIONAL_WORDS_RE = re.compile(
    r"^(?:详情页|页面|最上方|顶部|标题区|标题|头部|左侧|右侧|列表|区域|"
    r"展示|包含|显示|验证)+",
)
# 「学段学科信息」等摘要性描述, 不是页面上的字段标签
_META_FIELD_HINT_RE = re.compile(r"(信息|内容|文案|说明|情况|描述)$")


@dataclass
class AssertScope:
    """从自然语言 intent 解析出的断言范围."""

    region_keys: list[str] = field(default_factory=lambda: ["main"])
    exclude_nav: bool = False
    all_table_rows: bool = False
    negate_table_rows: bool = False
    field_hint: Optional[str] = None
    explicit_region: bool = False


def parse_assert_scope(intent: str, *, value: str = "", negate: bool = False) -> AssertScope:
    scope = AssertScope()
    if not intent:
        return scope

    if _SCOPE_HEADER_RE.search(intent):
        scope.region_keys = ["header", "main_top"]
        scope.exclude_nav = True
        scope.explicit_region = True
    elif _SCOPE_LEFT_RE.search(intent):
        scope.region_keys = ["main_left", "main"]
        scope.exclude_nav = True
        scope.explicit_region = True
    elif _SCOPE_RIGHT_RE.search(intent):
        scope.region_keys = ["form", "main_right", "main"]
        scope.explicit_region = True
    elif _SCOPE_FORM_RE.search(intent):
        scope.region_keys = ["form", "main_right"]
        scope.explicit_region = True
    elif _SCOPE_MAIN_RE.search(intent):
        scope.region_keys = ["main"]
        scope.exclude_nav = True
        scope.explicit_region = True

    if _ALL_ROWS_RE.search(intent) and (
        _SCOPE_TABLE_RE.search(intent) or "行" in intent
    ):
        scope.all_table_rows = True

    if negate and _SCOPE_TABLE_RE.search(intent) and _LIST_NEGATE_RE.search(intent):
        scope.negate_table_rows = True

    scope.field_hint = extract_field_hint(intent, value)
    return scope


def _is_meta_field_hint(hint: str) -> bool:
    """类别/摘要描述 (如学段学科信息), 非页面可见字段名."""
    if not hint:
        return True
    if _META_FIELD_HINT_RE.search(hint):
        return True
    if hint in ("标题", "标题区", "页头", "顶部"):
        return True
    return False


def extract_field_hint(intent: str, value: str) -> Optional[str]:
    """从 intent 提取被校验的字段/属性名 (区别于 value 本身)."""
    tail = re.sub(r"^验证", "", intent or "").strip()
    if not tail or not value:
        return None

    m = re.search(rf"(.+?)(?:是|为)\s*{re.escape(value)}\s*$", tail)
    if m:
        hint = _clean_field_subject(m.group(1))
        return None if _is_meta_field_hint(hint) else hint

    # 「字段:值」如 学段学科:大学数学 — value 已单独传入, 字段名取冒号前
    m_colon = re.search(rf"(.+?)[：:]\s*{re.escape(value)}\s*$", tail)
    if m_colon:
        hint = _clean_field_subject(m_colon.group(1))
        return None if _is_meta_field_hint(hint) else hint

    m2 = re.search(r"(?:展示|包含|显示)(.+)$", tail)
    if m2:
        subj = m2.group(1).strip("'\"「」")
        if subj and subj != value and value not in subj:
            hint = _clean_field_subject(subj)
            return None if _is_meta_field_hint(hint) else hint
    return None


def _clean_field_subject(subject: str) -> Optional[str]:
    s = subject.strip()
    for _ in range(6):
        ns = _POSITIONAL_WORDS_RE.sub("", s).strip()
        if ns == s:
            break
        s = ns
    s = re.sub(r"(包含|展示|显示)$", "", s).strip()
    return s or None


def scope_label(scope: AssertScope) -> str:
    labels = {
        "header": "页头区",
        "main_top": "主区上部",
        "main_left": "主区左侧",
        "main_right": "主区右侧",
        "main": "主内容区",
        "form": "表单区",
        "nav": "导航区",
    }
    names = [labels.get(k, k) for k in scope.region_keys]
    return "/".join(names) if names else "主内容区"


def extract_page_regions(page: Any) -> dict[str, str]:
    """按 DOM 结构划分页面区域文本 (nav / header / main / form 等)."""
    script = """() => {
      const clip = (el, n) => ((el && el.innerText) || '').replace(/\\s+/g, ' ').trim().slice(0, n);
      const nav = document.querySelector(
        'nav, aside, [role="navigation"], [role="complementary"], .ant-layout-sider'
      );
      const main = document.querySelector(
        'main, [role="main"], .ant-layout-content, .ant-pro-page-container'
      ) || document.body;
      const forms = [...document.querySelectorAll('form')];
      const formEl = forms.find(f => f.querySelector(
        'input[type=radio], input[type=checkbox], .ant-radio, .ant-checkbox'
      )) || forms[0] || null;
      const header = main.querySelector(
        'header, .ant-page-header, .page-header, [class*="page-header"]'
      );
      const kids = [...main.children].filter(
        el => el !== formEl && !(nav && nav.contains(el))
      );
      const left = kids.find(el => formEl && el.compareDocumentPosition(formEl) & 4) || kids[0];
      const right = formEl || kids[kids.length - 1];
      const topParts = kids.slice(0, Math.min(2, kids.length));
      return {
        nav: clip(nav, 1200),
        header: clip(header || topParts[0], 1500),
        main_top: clip(topParts.map(e => e).reduce((a, b) => a || b, null), 2000),
        main_left: clip(left, 3500),
        main_right: clip(right, 2500),
        form: clip(formEl, 2500),
        main: clip(main, 5000),
        body: clip(document.body, 6000),
      };
    }"""
    try:
        data = page.evaluate(script)
        if isinstance(data, dict):
            return {str(k): str(v or "") for k, v in data.items()}
    except Exception:
        pass
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    return {"body": body, "main": body, "nav": "", "header": "", "form": "", "main_left": body}


def items_flat_text(items: list[dict]) -> str:
    """从 semantic_items 拼接可见文本 (替代 body_text)."""
    parts: list[str] = []
    seen: set[str] = set()
    for it in items:
        for field in ("text", "value", "placeholder", "name"):
            v = str(it.get(field) or "").strip()
            if not v or v in seen:
                continue
            seen.add(v)
            parts.append(v)
    return "\n".join(parts)


def _is_nav_item(it: dict) -> bool:
    role = str(it.get("role") or "").lower()
    if role in ("menuitem", "navigation"):
        return True
    scope = str(it.get("scope") or "")
    return "navigation" in scope or role == "menu"


def filter_items_for_scope(items: list[dict], scope: AssertScope) -> list[dict]:
    """按断言范围过滤 semantic_items (替代 regions 切片)."""
    out = list(items)
    if scope.exclude_nav:
        out = [it for it in out if not _is_nav_item(it)]
    if not scope.explicit_region:
        return out
    keys = set(scope.region_keys)
    if keys & {"form", "main_right"}:
        form_items = [it for it in out if it.get("in_form")]
        if form_items:
            return form_items
    if keys & {"header", "main_top"}:
        return [it for it in out if not it.get("in_form") and not _is_nav_item(it)]
    if keys & {"main_left", "main"}:
        return [it for it in out if not _is_nav_item(it)]
    return out


def get_scoped_items_text(items: list[dict], scope: AssertScope) -> str:
    filtered = filter_items_for_scope(items, scope)
    return items_flat_text(filtered)


def try_field_value_assert_items(
    scope: AssertScope, items: list[dict], field_hint: str, value: str,
) -> Optional[tuple[bool, str]]:
    """在 semantic_items 限定范围内匹配「字段 + 值」."""
    if not field_hint or not value:
        return None
    text = get_scoped_items_text(items, scope)
    if not text:
        return False, f"字段断言: {scope_label(scope)} 无文本"

    labels = [field_hint]
    for token in re.findall(r"[\u4e00-\u9fff]{2,}", field_hint):
        if token not in labels:
            labels.append(token)

    for label in labels:
        pat = rf"{re.escape(label)}[^\n]{{0,30}}[：:\s]*[^\n]{{0,60}}{re.escape(value)}"
        m = re.search(pat, text)
        if m:
            excerpt = m.group(0).replace("\n", " ")[:60]
            return True, f"字段断言: {scope_label(scope)} {label!r}→{value!r} ({excerpt})"

    return None


def _normalize_match_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _text_contains(target: str, haystack: str) -> bool:
    if not target or not haystack:
        return False
    if target in haystack:
        return True
    return _normalize_match_text(target) in _normalize_match_text(haystack)


def try_scoped_literal_items(
    scope: AssertScope, items: list[dict], target: str,
) -> Optional[tuple[bool, str]]:
    """在 semantic_items 限定区域内做字面包含."""
    if not target or not scope.explicit_region:
        return None
    text = get_scoped_items_text(items, scope)
    if _text_contains(target, text):
        return True, f"区域断言: {scope_label(scope)} 含 {target!r}"
    return False, f"区域断言: {scope_label(scope)} 不含 {target!r}"


def _xpath_page_contains_text(page: Any, target: str) -> bool:
    """页面级文本搜索 (对齐 V3 assert 兜底, 不绑定具体组件)."""
    truncated = (target or "").strip()[:50]
    if not truncated:
        return False
    try:
        if "'" not in truncated:
            sel = f"xpath=//*[contains(text(), '{truncated}')]"
        elif '"' not in truncated:
            sel = f'xpath=//*[contains(text(), "{truncated}")]'
        else:
            return False
        loc = page.locator(sel)
        count = min(loc.count(), 5)
        for i in range(count):
            try:
                t = loc.nth(i).evaluate(
                    "el => (el.textContent || el.innerText || '').trim()",
                )
                if _text_contains(target, str(t or "")):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def try_live_scoped_text(
    page: Any,
    scope: AssertScope,
    target: str,
    *,
    negate: bool = False,
) -> Optional[tuple[bool, str]]:
    """字面/scoped 未命中缓存 DOM 时, 从活页作用域文本或页面级 XPath 再判一次."""
    if not target:
        return None
    haystack = ""
    try:
        regions = extract_page_regions(page)
        if scope.explicit_region or scope.field_hint or scope.exclude_nav:
            haystack = get_scoped_text(regions, scope)
        if not haystack:
            haystack = regions.get("main") or regions.get("body") or ""
    except Exception:
        haystack = ""
    if not haystack:
        try:
            haystack = (page.inner_text("body") or "")[:6000]
        except Exception:
            haystack = ""

    if negate:
        present = _text_contains(target, haystack) or _xpath_page_contains_text(page, target)
        return (
            (not present),
            f"live否定断言: 页面{'仍包含' if present else '不包含'} {target!r}",
        )

    if _text_contains(target, haystack):
        label = scope_label(scope) if scope.explicit_region else "页面"
        return True, f"live区域断言: {label} 含 {target!r}"
    if _xpath_page_contains_text(page, target):
        return True, f"live页面搜索: 含 {target!r}"
    return None


def build_semantic_text_summary_from_items(
    items: list[dict], scope: AssertScope,
) -> str:
    """语义断言用文本摘要: 从 semantic_items 按范围拼接."""
    parts: list[str] = []
    scoped = get_scoped_items_text(items, scope).replace("\n", " ").strip()
    if scoped:
        parts.append(f"[断言区域] {scoped[:2000]}")
    if scope.exclude_nav:
        nav_text = items_flat_text([it for it in items if _is_nav_item(it)])[:400]
        if nav_text:
            parts.append(f"[导航区-仅供参考勿作断言依据] {nav_text}")
    flat = items_flat_text(items).replace("\n", " ").strip()
    if flat and flat not in scoped:
        parts.append(f"[全文节选] {flat[:800]}")
    return " | ".join(parts)[:4000] if parts else flat[:2000]


def get_scoped_text(regions: dict[str, str], scope: AssertScope) -> str:
    parts: list[str] = []
    for key in scope.region_keys:
        t = regions.get(key, "").strip()
        if t:
            parts.append(t)
    if parts:
        return "\n".join(parts)
    return regions.get("main") or regions.get("body") or ""


def try_field_value_assert(
    scope: AssertScope, regions: dict[str, str], field_hint: str, value: str,
) -> Optional[tuple[bool, str]]:
    """在限定区域内匹配「字段标签 + 值」模式 (如 学段学科: 大学数学)."""
    if not field_hint or not value:
        return None
    text = get_scoped_text(regions, scope)
    if not text:
        return False, f"字段断言: {scope_label(scope)} 无文本"

    labels = [field_hint]
    for token in re.findall(r"[\u4e00-\u9fff]{2,}", field_hint):
        if token not in labels:
            labels.append(token)

    for label in labels:
        pat = rf"{re.escape(label)}[^\n]{{0,30}}[：:\s]*[^\n]{{0,60}}{re.escape(value)}"
        m = re.search(pat, text)
        if m:
            excerpt = m.group(0).replace("\n", " ")[:60]
            return True, f"字段断言: {scope_label(scope)} {label!r}→{value!r} ({excerpt})"

    return None


def try_scoped_literal(
    scope: AssertScope, regions: dict[str, str], target: str,
) -> Optional[tuple[bool, str]]:
    """在限定区域内做字面包含 (避免侧栏/导航误匹配)."""
    if not target or not scope.explicit_region:
        return None
    text = get_scoped_text(regions, scope)
    if target in text:
        return True, f"区域断言: {scope_label(scope)} 含 {target!r}"
    return False, f"区域断言: {scope_label(scope)} 不含 {target!r}"


def build_semantic_text_summary(
    body_text: str, regions: dict[str, str], scope: AssertScope,
) -> str:
    """语义断言用文本摘要: 优先拼接断言相关区域."""
    parts: list[str] = []
    scoped = get_scoped_text(regions, scope).replace("\n", " ").strip()
    if scoped:
        parts.append(f"[断言区域] {scoped[:2000]}")
    if scope.exclude_nav and regions.get("nav"):
        parts.append(f"[导航区-仅供参考勿作断言依据] {regions['nav'][:400]}")
    flat = body_text.replace("\n", " ").strip()
    if flat and flat not in scoped:
        parts.append(f"[全文节选] {flat[:800]}")
    return " | ".join(parts)[:4000] if parts else flat[:2000]


def format_scope_note_for_semantic(scope: AssertScope) -> str:
    if not scope.explicit_region and not scope.field_hint:
        return ""
    lines = [f"断言范围: {scope_label(scope)}"]
    if scope.exclude_nav:
        lines.append("须排除: 侧栏导航、菜单项、与断言字段无关的表单选项文案")
    if scope.field_hint:
        lines.append(f"关注字段/属性: {scope.field_hint}")
    lines.append("value 为业务概念简称时, 不要求页面出现与 value 完全相同的标题; 但须在断言范围内有语义等价展示")
    return "\n".join(lines)


def should_disable_semantic_fallback(scope: AssertScope) -> bool:
    """列表「所有行」类断言由结构化行扫描负责, 不走语义兜底."""
    return scope.all_table_rows or scope.negate_table_rows

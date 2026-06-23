"""L5 本地兜底: LLM 未命中时用文本/属性启发式选 index (对齐 V3 _fallback_resolve)."""
from __future__ import annotations

import re
from typing import Optional


def _fill_field_match_score(fill_field: str, placeholder: str, aria_label: str) -> float:
    """字段名与 placeholder/aria 的匹配分 (支持整词与半词, 适配中文)."""
    fl = (fill_field or "").strip().lower()
    if not fl:
        return 0.0
    ph = (placeholder or "").lower()
    al = (aria_label or "").lower()
    score = 0.0
    if fl in ph:
        score += 5.0
    if fl in al:
        score += 3.0
    if len(fl) >= 4:
        half = len(fl) // 2
        a, b = fl[:half], fl[half:]
        if len(a) >= 2 and a in ph:
            score += 2.0
        if len(b) >= 2 and b in ph:
            score += 2.0
        if len(a) >= 2 and a in al:
            score += 1.5
        if len(b) >= 2 and b in al:
            score += 1.5
    return score


def fallback_resolve_index(
    items: list[dict],
    intent: str,
    action_type: str = "",
) -> Optional[int]:
    """返回 items 中的 index; 未匹配则 None."""
    if not items or not intent:
        return None

    at = (action_type or "").lower()
    dropdown_keywords = ["下拉框", "下拉菜单", "dropdown", "下拉", "菜单"]
    is_dropdown = any(kw in intent for kw in dropdown_keywords)
    expand_trigger_keywords = ["展开按钮", "展开下拉", "下拉框展开", "下拉菜单展开"]
    option_keywords = ["下拉选项", "选项"]
    is_dropdown_trigger = is_dropdown and any(kw in intent for kw in expand_trigger_keywords)
    is_dropdown_option = (
        is_dropdown
        and any(kw in intent for kw in option_keywords)
        and not is_dropdown_trigger
    )

    dropdown_field_label = None
    if is_dropdown:
        for pattern in (
            r"点击\s*[\"']?(.+?)[\"']?\s*下拉(?:框|菜单|栏)",
            r"展开\s*[\"']?(.+?)[\"']?\s*下拉(?:框|菜单|栏)",
        ):
            m = re.search(pattern, intent)
            if m:
                lbl = (m.group(1) or "").strip().strip('"\'')
                if 1 <= len(lbl) <= 30:
                    dropdown_field_label = lbl
                    break

    fill_field_label = None
    if at == "fill":
        for pattern in (
            r"在\s*[\"']?(.+?)[\"']?\s*输入框中(?:填写|输入)",
            r"在\s*[\"']?(.+?)[\"']?\s*(?:填写|输入)",
            r"向\s*[\"']?(.+?)[\"']?\s*输入框",
        ):
            m = re.search(pattern, intent)
            if m:
                lbl = (m.group(1) or "").strip().strip('"\'')
                if "的" in lbl:
                    lbl = lbl.split("的")[-1].strip()
                lbl = re.sub(r"(输入框|输入栏|文本框|字段)$", "", lbl).strip()
                if 1 <= len(lbl) <= 40:
                    fill_field_label = lbl
                    break

    click_text_anchor = None
    if at == "click":
        for pat in (
            r"点击\s*[\"'“”‘’「」]?\s*([^\"'“”‘’「」\s]{2,80}?)\s*[\"'“”‘’「」]?\s*(?:字样|文本|名称|按钮|链接)?(?:\s|$)",
        ):
            m = re.search(pat, intent)
            if m:
                cand = (m.group(1) or "").strip().strip('"\'“”‘’「」')
                cand = re.sub(r"(字样|文本|名称|按钮|链接)$", "", cand).strip()
                if 2 <= len(cand) <= 80:
                    click_text_anchor = cand
                    break

    if click_text_anchor:
        anchor_lower = click_text_anchor.lower()
        for idx, element in enumerate(items):
            text = (element.get("text") or "").strip().lower()
            name = (element.get("name") or "").strip().lower()
            test_id = (element.get("testId") or "").strip().lower()
            aria = element.get("aria") or {}
            aria_label = (
                (aria.get("aria-label") or "").strip().lower()
                if isinstance(aria, dict) else ""
            )
            if (
                text == anchor_lower or anchor_lower in text
                or name == anchor_lower or anchor_lower in name
                or test_id == anchor_lower or anchor_lower in test_id
                or aria_label == anchor_lower or anchor_lower in aria_label
            ):
                return idx

    button_text_keywords: list[str] = []
    for qm in re.findall(r"[「'\"']([^'\"」「」]+)['\"'」]", intent):
        if 2 <= len(qm) <= 200:
            button_text_keywords.append(qm)

    if button_text_keywords:
        intent_keywords = button_text_keywords
    else:
        intent_keywords = [
            kw for kw in re.split(r"[\s,，。、]+", intent.lower()) if len(kw) > 1
        ]

    best_idx: Optional[int] = None
    best_score = 0.0

    for idx, element in enumerate(items):
        score = 0.0
        tag = (element.get("tag") or "").lower()
        text_lower = (element.get("text") or "").strip().lower()
        role = (element.get("role") or "").lower()
        test_id = (element.get("testId") or "").lower()
        placeholder = (element.get("placeholder") or "").lower()
        class_name = (element.get("class") or "").lower()
        aria = element.get("aria") or {}
        aria_label = (
            (aria.get("aria-label") or "").strip().lower()
            if isinstance(aria, dict) else ""
        )

        if is_dropdown_trigger:
            if tag in ("input", "textarea") or role in ("combobox", "textbox"):
                score += 1.5
            if "select" in class_name or role == "combobox":
                score += 1.0
            if dropdown_field_label:
                fl = dropdown_field_label.lower()
                if fl in placeholder:
                    score += 3.0
                if fl in aria_label:
                    score += 1.5
                if fl in text_lower:
                    score += 0.4
        elif is_dropdown_option:
            if tag == "li" or role in ("menuitem", "option"):
                score += 1.5
        elif is_dropdown:
            if tag == "li" or role == "menuitem":
                score += 1.0

        if at == "fill":
            if tag in ("input", "textarea") or role == "textbox":
                score += 1.2
            if fill_field_label:
                fl = fill_field_label.lower()
                if fl in placeholder:
                    score += 3.0
                if fl in aria_label:
                    score += 1.5
                if fl in text_lower:
                    score += 0.4

        for keyword in intent_keywords:
            kl = keyword.lower()
            if text_lower == kl:
                score += 2.0
            elif kl in text_lower:
                score += 0.5
            if kl in test_id:
                score += 0.3
            if kl in placeholder:
                score += 0.4
            if kl in aria_label:
                score += 0.2
            if kl in class_name:
                score += 0.4

        if any(kw in intent for kw in ("创建", "新建", "新增")):
            if any(x in class_name for x in ("create", "new", "add")):
                score += 1.2
            if "button" in class_name or "btn" in class_name:
                score += 0.6
            if at == "click" and tag in ("button", "a"):
                score += 0.4

        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx is not None and best_score > 0:
        if at == "fill" and fill_field_label:
            best = items[best_idx]
            ph = (best.get("placeholder") or "").lower()
            aria = best.get("aria") or {}
            al = (
                (aria.get("aria-label") or "").lower()
                if isinstance(aria, dict) else ""
            )
            if _fill_field_match_score(fill_field_label, ph, al) < 0.3:
                return None
        return best_idx
    return None

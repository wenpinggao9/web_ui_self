"""执行期断言/点击结果回填 —— 供 codegen 生成与 run.py 一致的 Playwright 代码."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..planning import PlannedAction
from .script_helpers import measure_list_count


def set_codegen_assert(action: PlannedAction, spec: dict[str, Any]) -> None:
    extras = dict(action.extras or {})
    extras["codegen_assert"] = spec
    action.extras = extras


def set_codegen_post_wait(action: PlannedAction, spec: dict[str, Any]) -> None:
    extras = dict(action.extras or {})
    extras["codegen_post_wait"] = spec
    action.extras = extras


def _url_path(url: str) -> str:
    return urlparse(url or "").path or ""


def record_literal(action: PlannedAction, text: str, *, negate: bool = False) -> None:
    kind = "negate_literal" if negate else "literal"
    set_codegen_assert(action, {"kind": kind, "text": text})


def record_url_from_page(action: PlannedAction, page: Any) -> None:
    path = _url_path(getattr(page, "url", "") or "")
    if path and path != "/":
        set_codegen_assert(action, {"kind": "url_contains", "fragment": path})


def record_post_click_wait(action: PlannedAction, url_before: str, url_after: str) -> None:
    """点击后 URL 变化时记录, 脚本侧等待同一 path 片段."""
    if (url_before or "") == (url_after or ""):
        return
    path = _url_path(url_after)
    if path and path != "/":
        set_codegen_post_wait(action, {"kind": "url_contains", "fragment": path})


def record_or_branch(action: PlannedAction, page: Any, branches: list[Any], body_text: str) -> None:
    """与 try_or_branches 判定顺序一致, 记录获胜分支当时页面状态."""
    from .assert_or import try_or_heuristic

    for raw in branches:
        if not isinstance(raw, dict):
            continue
        branch_intent = str(raw.get("intent") or raw.get("desc") or "").strip()
        branch_value = str(raw.get("value") or "").strip()
        if branch_value and branch_value in body_text:
            record_literal(action, branch_value)
            return
        hit = try_or_heuristic(page, branch_intent)
        if hit is not None:
            record_pass_state(action, page, body_text, branch_value or None)
            return


def record_or_heuristic(action: PlannedAction, page: Any, intent: str) -> None:
    from .assert_or import try_or_heuristic

    if try_or_heuristic(page, intent) is not None:
        record_pass_state(action, page, "", None)


def record_pass_state(
    action: PlannedAction,
    page: Any,
    body_text: str,
    literal_target: str | None,
) -> None:
    """按 run.py 判定顺序, 用当时页面状态回填可执行断言 (无业务词典)."""
    target = (literal_target or (action.value or "")).strip()
    body = body_text or ""
    try:
        if not body:
            body = page.inner_text("body")
    except Exception:
        body = ""

    if action.negate:
        if target and target not in body:
            record_literal(action, target, negate=True)
        return

    if target and target in body:
        record_literal(action, target)
        return

    n, _ = measure_list_count(page)
    if n > 0:
        set_codegen_assert(action, {"kind": "list_count", "op": ">", "threshold": 0})
        return

    path = _url_path(getattr(page, "url", "") or "")
    if path and path != "/":
        set_codegen_assert(action, {"kind": "url_contains", "fragment": path})
        return

    if target:
        record_literal(action, target)


def record_semantic_pass(action: PlannedAction, page: Any, body_text: str) -> None:
    """语义断言通过: 回填与当时页面状态一致的可执行检查."""
    record_pass_state(action, page, body_text, (action.value or "").strip() or None)


def record_control_mode(action: PlannedAction, stats: dict[str, Any], *, want_single: bool) -> None:
    r = int(stats.get("radio", 0))
    c = int(stats.get("checkbox", 0))
    if want_single:
        set_codegen_assert(action, {
            "kind": "control_single",
            "radio_min": 2,
            "checkbox_max": 0,
            "radio": r,
            "checkbox": c,
        })
    else:
        set_codegen_assert(action, {
            "kind": "control_multi",
            "checkbox_min": 2,
            "checkbox": c,
            "radio": r,
        })


def record_assert_count(action: PlannedAction, op: str, threshold: int, source: str) -> None:
    set_codegen_assert(action, {"kind": "list_count", "op": op, "threshold": threshold})


def should_skip_or_branch(action: PlannedAction) -> bool:
    extras = action.extras or {}
    gid = extras.get("or_group")
    if not gid:
        return False
    return not extras.get("or_winner") and not extras.get("codegen_assert")

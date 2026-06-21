"""步骤㉑ Playwright 代码生成 —— 生成可独立运行的 Python 脚本.

每个用例执行完成后, 生成一个 Python 脚本, 包含:
  - 浏览器启动
  - 登录流程
  - 所有操作步骤 (含选择器)
保存到用例目录 playwright_<用例编号>.py.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .execution.assert_codegen import should_skip_or_branch
from .execution.dispatcher import _parse_count_spec
from .execution.script_helpers import min_count_for_compare, parse_table_row_click
from .locating.playwright_api import infer_from_selector, info_to_python_expr, normalize_info
from .planning import PlannedAction

_API_HELPER = '''
def _run_api_preconditions(intents, project_root, case_file):
    """执行 API 前置 (投放/造数), 返回 orderId 等变量上下文."""
    import sys
    from pathlib import Path
    root = Path(project_root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from core.api_client import APIClient
    from core.api_runner import ApiRunner
    from core.business_loader import BusinessLoader
    biz = BusinessLoader()
    if not biz.discover(case_file):
        raise RuntimeError(f"无法从用例路径加载业务配置: {case_file}")
    profile = biz.build_system_profile()
    runner = ApiRunner(APIClient(profile), profile)
    ctx = runner.run_preconditions(intents)
    print("API 前置完成:", ctx)
    return ctx
'''

_BIND_SESSION_HELPER = '''
def _run_bind_session(page, api_ctx, case_id, intent, prev_click=None):
    """根据业务配置解析实体 ID 并写入 api_ctx['ops']."""
    import sys
    from pathlib import Path
    root = Path(PROJECT_ROOT)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from core.api_client import APIClient
    from core.api_runner import ApiRunner
    from core.business_loader import BusinessLoader
    from core.execution.session_ops import execute_bind_session
    biz = BusinessLoader()
    if not biz.discover(CASE_FILE):
        raise RuntimeError(f"无法从用例路径加载业务配置: {CASE_FILE}")
    knowledge = biz.get_knowledge()
    profile = biz.build_system_profile()
    runner = ApiRunner(APIClient(profile), profile)
    runner.context.update(api_ctx)
    ok, msg, _ = execute_bind_session(
        page,
        api_ctx,
        api_runner=runner,
        case_id=case_id,
        prev_click=prev_click,
        intent=intent,
        session_ops_cfg=knowledge.get("session_ops"),
        page_capture=knowledge.get("page_capture"),
    )
    print(msg, flush=True)
    return ok, msg
'''

_POPUP_IMPORT = '''
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.execution.popup_recovery import (
    try_dismiss_blocking_dialog as _dismiss_blocking_dialog,
)
from core.execution.script_helpers import (
    compare_count,
    count_real_table_rows,
    measure_list_count,
    min_count_for_compare,
    wait_after_nav_click,
    wait_before_assert,
    wait_for_list_count_at_least,
    wait_for_url_fragment,
    get_scoped_page_text,
    locate_button_in_table_row,
    wait_for_table_row_button,
)

_DIALOG_VISIBLE = '[role="dialog"]:visible, .ant-modal-wrap:visible'

def _dismiss_blocking_dialog_if_present(page, timeout=10000):
    """若页面存在阻断弹窗 (如红线标准), 勾选并确认; 非用例主步骤."""
    if page.locator(_DIALOG_VISIBLE).count():
        _dismiss_blocking_dialog(page, timeout)
'''

_STEP_LOG_HELPER = '''
def _step_log(step_no: int, action: str, intent: str) -> None:
    """控制台输出当前步骤."""
    print(f"▶ Step {step_no:02d} [{action}] {intent}", flush=True)
'''

_ASSERT_TABLE_HELPER = '''
def _assert_table_cell(page, row_key, key_col, target_col, expected):
    """断言表格中某行某列的值."""
    tables = page.locator("table")
    for ti in range(tables.count()):
        table = tables.nth(ti)
        headers = [h.strip() for h in table.locator("thead th, thead td").all_inner_texts()]
        if not headers or key_col not in headers or target_col not in headers:
            continue
        key_idx = headers.index(key_col)
        col_idx = headers.index(target_col)
        body_rows = table.locator("tbody tr")
        for ri in range(body_rows.count()):
            cells = [c.strip() for c in body_rows.nth(ri).locator("td").all_inner_texts()]
            if key_idx >= len(cells) or row_key not in cells[key_idx]:
                continue
            actual = cells[col_idx] if col_idx < len(cells) else ""
            assert expected in actual or actual == expected, (
                f"表格断言失败: 行 {row_key!r} 列 {target_col!r} "
                f"期望 {expected!r} 实际 {actual!r}"
            )
            return
    raise AssertionError(
        f"表格断言失败: 未找到行 {row_key!r} (列 {key_col!r}) 或列 {target_col!r}"
    )
'''


def generate_spec(
    case_id: str,
    actions: list[PlannedAction],
    base_url: str,
    out_dir: str | Path,
    login_config: dict[str, Any] | None = None,
    api_context: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
    case_file: str | Path | None = None,
    popup_dismiss_used: bool = False,
    popup_recoveries: list[PlannedAction] | None = None,
    popup_dismiss_before_intents: list[str] | None = None,
    idempotent_skip_intents: list[str] | None = None,
) -> Path:
    """根据已执行动作生成可独立运行的 Playwright Python 脚本."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    path = out_dir / f"playwright_{_safe(case_id)}.py"
    api_intents = [a.intent for a in actions if a.type == "api_call"]
    has_bind = any(a.type == "bind_session" for a in actions)
    root = Path(project_root) if project_root else None
    case_path = Path(case_file) if case_file else None
    if root and case_path:
        try:
            case_rel = case_path.relative_to(root)
        except ValueError:
            case_rel = case_path
    else:
        case_rel = case_path or Path("")
    dismiss_before = list(popup_dismiss_before_intents or [])
    idempotent_skip = set(idempotent_skip_intents or [])
    need_runtime_import = _needs_runtime_import(
        actions, popup_dismiss_used, popup_recoveries, dismiss_before, idempotent_skip,
    )
    ts = _TEMPLATE.format(
        case_id=case_id,
        case_id_safe=_safe(case_id),
        step_log_helper=_STEP_LOG_HELPER,
        api_helper=_API_HELPER if api_intents else "",
        bind_helper=_BIND_SESSION_HELPER if has_bind else "",
        popup_import=_POPUP_IMPORT if need_runtime_import else "",
        assert_table_helper=_ASSERT_TABLE_HELPER,
        project_root=_py_str(str(root or "")),
        case_file_expr=f"str(PROJECT_ROOT / {_py_str(str(case_rel))})" if root else _py_str(str(case_file or "")),
        api_setup=_gen_api_setup(api_intents, bool(api_intents) or has_bind),
        login_code=_gen_login(login_config or {}),
        popup_preamble=_gen_popup_preamble(popup_recoveries or []),
        ui_steps=_gen_ui_steps(
            actions, api_context or {}, case_id=case_id,
            runtime_api=bool(api_intents) or has_bind,
            popup_dismiss_before=dismiss_before,
            popup_dismiss_used=popup_dismiss_used,
            idempotent_skip=idempotent_skip,
        ),
    )
    path.write_text(ts, encoding="utf-8")
    return path


def _extract_login_username(script_path: Path) -> str:
    """从已生成的用例脚本 login() 中提取账号 (用于套件判断是否需要重新登录)."""
    try:
        text = script_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(
        r"get_by_placeholder\([^)]+\)\.fill\((['\"])(.+?)\1\)",
        text,
    )
    return m.group(2) if m else ""


def generate_suite_script(
    batch_dir: str | Path,
    case_ids: list[str],
    project_root: str | Path | None = None,
) -> Path | None:
    """生成批次套件脚本: 共享浏览器会话; 同账号仅登录一次, 切换角色时重新登录."""
    batch_dir = Path(batch_dir)
    root = Path(project_root) if project_root else batch_dir.parent.parent.parent
    entries: list[tuple[str, Path]] = []
    for cid in case_ids:
        script = batch_dir / _safe(cid) / f"playwright_{_safe(cid)}.py"
        if script.is_file():
            entries.append((cid, script))

    if len(entries) < 2:
        return None

    rel_scripts: list[tuple[str, str, Path, str]] = []
    for cid, script in entries:
        try:
            rel = script.relative_to(root)
        except ValueError:
            rel = script
        rel_scripts.append((cid, _safe(cid), rel, _extract_login_username(script)))

    load_lines = []
    run_lines = []
    for cid, mod_safe, rel, login_user in rel_scripts:
        var = f"_mod_{mod_safe}"
        load_lines.append(
            f"            {var} = _load_case_module(PROJECT_ROOT / {_py_str(str(rel))})"
        )
        run_lines.append(f"            def _run_{mod_safe}():")
        if login_user:
            run_lines.append("                global page")
            run_lines.append(
                f"                page = _get_page_for_user(browser, {_py_str(login_user)}, "
                f"{var}.login, case_id={_py_str(cid)})"
            )
        run_lines.append(f"                {var}.run_steps(page)")
        run_lines.append(
            f"            if not _run_one_case({_py_str(cid)}, _run_{mod_safe}):"
        )
        run_lines.append("                _print_suite_summary()")
        run_lines.append("                sys.exit(1)")

    try:
        suite_rel = (batch_dir / "playwright_suite.py").relative_to(root)
        suite_run = str(suite_rel)
    except ValueError:
        suite_run = str(batch_dir / "playwright_suite.py")

    content = f'''\
"""批次连续执行套件 — 同浏览器多账号隔离.

运行: 在项目根目录执行 python {suite_run}

说明: 每个账号独立 browser context (新窗口会话), 同账号仅登录一次;
切换账号时新建 context 并 goto 登录页, 与 run.py 角色切换一致.
单独跑 Case 2+ 脚本会因缺少前置数据/页面状态失败; 请用本套件或 run.py.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path({_py_str(str(root))})

_USER_SESSIONS: dict[str, tuple] = {{}}
_CASE_RESULTS: list[tuple[str, bool]] = []


def _load_case_module(script_path: Path):
    spec = spec_from_file_location(script_path.stem, script_path)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get_page_for_user(browser, account: str, login_fn, *, case_id: str = ""):
    """每个账号独立 context; 同账号复用已登录页, 换账号时新开 context 再登录."""
    if account in _USER_SESSIONS:
        _ctx, pg = _USER_SESSIONS[account]
        pg.bring_to_front()
        return pg
    print(f"▶ 登录 ({{case_id}}, {{account!r}})", flush=True)
    ctx = browser.new_context()
    pg = ctx.new_page()
    login_fn(pg)
    _USER_SESSIONS[account] = (ctx, pg)
    return pg


def _run_one_case(case_id: str, action) -> bool:
    global page
    print(f"▶ 用例 {{case_id}} 开始", flush=True)
    try:
        action()
        print(f"用例 {{case_id}} 执行完成 ✅ 通过", flush=True)
        _CASE_RESULTS.append((case_id, True))
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        print(f"用例 {{case_id}} 执行失败 ❌", flush=True)
        _CASE_RESULTS.append((case_id, False))
        if page is not None:
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in case_id)
            page.screenshot(path=f"error_{{safe}}.png")
        return False


def _print_suite_summary() -> None:
    passed = sum(1 for _, ok in _CASE_RESULTS if ok)
    total = len(_CASE_RESULTS)
    print(f"批次套件汇总: 通过 {{passed}}/{{total}}", flush=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = None
        try:
{chr(10).join(load_lines)}
{chr(10).join(run_lines)}
            _print_suite_summary()
        except Exception:
            import traceback
            traceback.print_exc()
            _print_suite_summary()
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
'''
    out = batch_dir / "playwright_suite.py"
    out.write_text(content, encoding="utf-8")
    return out


def _py_str(value: str) -> str:
    """生成安全的 Python 字符串字面量 (自动转义引号与特殊字符)."""
    return repr(value)


def _gen_api_setup(intents: list[str], enabled: bool) -> str:
    if not enabled:
        return "    api_ctx = {}"
    intent_list = ", ".join(_py_str(i) for i in intents)
    return "\n".join([
        "    api_ctx = _run_api_preconditions(",
        f"        [{intent_list}],",
        "        project_root=PROJECT_ROOT,",
        "        case_file=CASE_FILE,",
        "    )",
    ])


def _value_expr(text: str | None, api_context: dict[str, Any], runtime_api: bool) -> str:
    """把 value 中的 ${var} 转为 Python 表达式或字面量."""
    raw = (text or "").strip()
    if not raw:
        return _py_str("")
    m = re.fullmatch(r"\$\{(\w+)\}", raw)
    if m and runtime_api:
        return f"api_ctx[{_py_str(m.group(1))}]"
    out = raw
    for k, v in api_context.items():
        out = out.replace(f"${{{k}}}", str(v))
    if runtime_api:
        for key in re.findall(r"\$\{(\w+)\}", out):
            out = out.replace(f"${{{key}}}", f"{{api_ctx[{_py_str(key)}]}}")
    return _py_str(out) if "${" not in out and "api_ctx[" not in out else out


def _apply_api_context(text: str | None, api_context: dict[str, Any], runtime_api: bool = False) -> str:
    """非断言 fill 等: 替换为字面量字符串."""
    if not text:
        return ""
    out = text
    for k, v in api_context.items():
        out = out.replace(f"${{{k}}}", str(v))
    if runtime_api:
        for key in re.findall(r"\$\{(\w+)\}", out):
            out = out.replace(f"${{{key}}}", f"{{api_ctx[{_py_str(key)}]}}")
    return out


def _gen_login(cfg: dict[str, Any]) -> str:
    """生成登录代码."""
    username = str(cfg.get("username") or "")
    verify_code = str(cfg.get("verify_code") or "")
    password = str(cfg.get("password") or verify_code or "")
    username_placeholder = str(cfg.get("username_placeholder") or "请输入手机号")
    password_placeholder = str(
        cfg.get("code_placeholder")
        or cfg.get("password_placeholder")
        or "请输入验证码"
    )
    login_tab = str(cfg.get("login_tab_text") or "")
    base_url = str(cfg.get("base_url") or "")

    lines = []
    lines.append('    print("▶ 登录", flush=True)')
    lines.append(f"    page.goto({_py_str(base_url)}, wait_until=\"domcontentloaded\")")
    if login_tab:
        lines.append(f"    page.get_by_text({_py_str(login_tab)}).click()")
        lines.append("    page.wait_for_load_state(\"domcontentloaded\")")
    lines.append(
        f"    page.get_by_placeholder({_py_str(username_placeholder)}).fill({_py_str(username)})"
    )
    lines.append(
        f"    page.get_by_placeholder({_py_str(password_placeholder)}).fill({_py_str(password)})"
    )
    # Ant Design 登录按钮常为「登 录」, 优先点主按钮
    lines.append("    page.locator(\"button.ant-btn-primary\").first.click()")
    lines.append("    page.wait_for_load_state(\"domcontentloaded\")")
    return "\n".join(lines)


def _gen_step_log_line(step_no: int, action: PlannedAction, prefix: str = "    ") -> str:
    return f"{prefix}_step_log({step_no}, {_py_str(action.type)}, {_py_str(action.intent or '')})"


def _gen_locator(info: dict | str) -> str:
    """将定位信息转为 Playwright Python 表达式 (语义 API 优先)."""
    if isinstance(info, str):
        info = infer_from_selector(info)
    return info_to_python_expr("page", normalize_info(info))


def _gen_click_loc_expr(
    action: PlannedAction,
    loc_info: dict | None,
    selector: str | None,
) -> str:
    """优先用执行期回填的更具体文案 (如 领取 → 领取题目)."""
    value = (action.value or "").strip()
    if value and action.type == "click":
        info = normalize_info(loc_info) if loc_info else {}
        if info.get("method") == "role":
            role = repr(info.get("role", "button"))
            exact = ", exact=True" if info.get("exact") else ""
            nth = info.get("nth", 0) or 0
            base = f"page.get_by_role({role}, name={_py_str(value)}{exact})"
            return f"{base}.nth({nth})" if nth else f"{base}.first"
        return f"page.get_by_text({_py_str(value)}, exact=False).first"
    if loc_info or selector:
        return _gen_locator(loc_info or infer_from_selector(selector or ""))
    if value:
        return f"page.get_by_text({_py_str(value)}, exact=False).first"
    return ""


def _is_table_row_click(action: PlannedAction) -> bool:
    info = normalize_info(action.locator_info or {})
    if info.get("method") == "table_row":
        return True
    sel = (action.selector or info.get("selector") or "").strip()
    return sel.startswith(("ant_table_row[", "table_row["))


def _gen_table_row_click_step(
    action: PlannedAction,
    api_context: dict[str, Any],
    runtime_api: bool,
    prefix: str = "    ",
    *,
    after_popup_dismiss: bool = False,
    idempotent: bool = False,
) -> list[str]:
    """行内表格按钮: 调用 locate_button_in_table_row, 禁止把 row_note 当 CSS."""
    extras = dict(action.extras or {})
    parsed = parse_table_row_click(action.intent or "", extras)
    if not parsed:
        return []
    button, row_hint, status_hint = parsed
    row_key = str(extras.get("row_key") or row_hint or "").strip()
    row_key = _apply_api_context(row_key, api_context, runtime_api)
    if not row_key or not button:
        return []

    key_col = str(extras.get("row_key_column") or "工单ID").strip()
    status_col = str(extras.get("status_column") or "").strip()
    kwargs = [
        f"button_label={_py_str(button)}",
        f"row_keys=[{_py_str(row_key)}]",
        f"key_col={_py_str(key_col)}",
    ]
    if status_hint:
        kwargs.append(f"status_filter={_py_str(status_hint)}")
        if status_col:
            kwargs.append(f"status_column={_py_str(status_col)}")

    lines: list[str] = []
    if after_popup_dismiss:
        lines.append(f"{prefix}# 运行期曾遇阻断弹窗, 若存在则先关闭 (非用例主步骤)")
        lines.append(f"{prefix}_dismiss_blocking_dialog_if_present(page)")
    lines.append(
        f"{prefix}_btn, _row_note = wait_for_table_row_button(page, {', '.join(kwargs)})"
    )
    lines.append(f"{prefix}assert _btn is not None, f'行内定位失败: {{_row_note}}'")
    if idempotent:
        lines.append(f"{prefix}if _btn.is_enabled():")
        lines.append(f"{prefix}    _btn.click()")
        lines.append(f"{prefix}elif count_real_table_rows(page) > 0:")
        lines.append(f"{prefix}    pass  # 幂等: 列表已有数据, 跳过本步")
        lines.append(f"{prefix}else:")
        lines.append(f"{prefix}    expect(_btn).to_be_enabled(timeout=10000)")
        lines.append(f"{prefix}    _btn.click()")
    else:
        lines.append(f"{prefix}expect(_btn).to_be_visible(timeout=10000)")
        lines.append(f"{prefix}_btn.click()")
    return lines


def _needs_runtime_import(
    actions: list[PlannedAction],
    popup_dismiss_used: bool,
    popup_recoveries: list[PlannedAction] | None,
    popup_dismiss_before: list[str],
    idempotent_skip: set[str],
) -> bool:
    """脚本是否需要导入运行期恢复/计数辅助 (仅依据运行期记录)."""
    if popup_dismiss_used or popup_recoveries or popup_dismiss_before or idempotent_skip:
        return True
    if any(getattr(a, "is_recovery", False) for a in actions):
        return True
    if any(a.type == "assert_count" for a in actions):
        return True
    if any(a.type == "bind_session" for a in actions):
        return True
    if any(_is_table_row_click(a) for a in actions if a.type == "click"):
        return True
    if any(a.is_assert() for a in actions):
        return True
    return False


def _click_label_for_codegen(action: PlannedAction) -> str | None:
    label = (action.value or "").strip()
    if not label:
        m = re.search(r"[「']([^」']+)[」']", action.intent or "")
        if m:
            label = m.group(1).strip()
    return label or None


def _gen_click_label_assign(action: PlannedAction, prefix: str = "    ") -> list[str]:
    label = _click_label_for_codegen(action)
    if not label:
        return []
    return [f"{prefix}_last_click_label = {_py_str(label)}"]


def _is_popup_recovery_action(a: PlannedAction) -> bool:
    return bool(getattr(a, "is_recovery", False))


def _gen_popup_preamble(popup_recoveries: list[PlannedAction]) -> str:
    """登录后仅插入 LLM 弹窗恢复 (规则关弹窗在具体操作前由 _wait_and_dismiss 处理)."""
    if not popup_recoveries:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for rec in popup_recoveries:
        key = f"{rec.type}:{rec.intent}"
        if key in seen:
            continue
        seen.add(key)
        lines.extend(_gen_conditional_recovery_action(rec, api_context={}, runtime_api=False, indent="    "))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _gen_conditional_recovery_action(
    a: PlannedAction,
    api_context: dict[str, Any],
    runtime_api: bool,
    indent: str = "    ",
) -> list[str]:
    """将单条恢复动作生成为「仅当弹窗可见时执行」."""
    lines = [f"{indent}# [条件恢复] {a.intent}"]
    inner = _gen_action_lines(a, api_context, runtime_api, indent=f"{indent}    ", scope="dialog")
    if not inner:
        return lines
    lines.append(f"{indent}dialog = page.locator('[role=\"dialog\"]:visible, .ant-modal-wrap:visible').first")
    lines.append(f"{indent}if dialog.count():")
    lines.extend(inner)
    return lines


def _gen_action_lines(
    a: PlannedAction,
    api_context: dict[str, Any],
    runtime_api: bool,
    indent: str,
    scope: str = "page",
) -> list[str]:
    """生成单条动作的 Playwright 代码行 (scope=dialog 时在弹窗内定位)."""
    root = "dialog" if scope == "dialog" else "page"
    value = _apply_api_context(a.value, api_context, runtime_api)
    loc_info = a.locator_info or (infer_from_selector(a.selector) if a.selector else None)
    lines: list[str] = []

    if a.type == "click" and a.value and a.value.strip():
        lines.append(f"{indent}{root}.get_by_text({_py_str(a.value)}, exact=False).first.click()")
        return lines
    if loc_info or a.selector:
        loc = _gen_locator_for_root(loc_info or infer_from_selector(a.selector or ""), root)
        if a.type == "click":
            lines.append(f"{indent}{loc}.click()")
        elif a.type == "fill":
            lines.append(f"{indent}{loc}.fill({_py_str(value)})")
        elif a.type == "hover":
            lines.append(f"{indent}{loc}.hover()")
    return lines


def _gen_locator_for_root(info: dict | str, root: str) -> str:
    if isinstance(info, str):
        info = infer_from_selector(info)
    expr = info_to_python_expr("page", normalize_info(info))
    if root == "dialog" and expr.startswith("page."):
        return "dialog." + expr[5:]
    return expr.replace("page.", f"{root}.", 1) if root != "page" else expr


def _gen_click_step(
    prefix: str,
    loc_expr: str,
    *,
    after_popup_dismiss: bool = False,
    idempotent: bool = False,
) -> list[str]:
    """生成点击步骤: 可选先条件关弹窗、等可点、幂等跳过."""
    lines: list[str] = []
    if after_popup_dismiss:
        lines.append(f"{prefix}# 运行期曾遇阻断弹窗, 若存在则先关闭 (非用例主步骤)")
        lines.append(f"{prefix}_dismiss_blocking_dialog_if_present(page)")
    lines.append(f"{prefix}_btn = {loc_expr}")
    if idempotent:
        lines.append(f"{prefix}if _btn.is_enabled():")
        lines.append(f"{prefix}    _btn.click()")
        lines.append(f"{prefix}elif count_real_table_rows(page) > 0:")
        lines.append(f"{prefix}    pass  # 幂等: 列表已有数据, 跳过本步")
        lines.append(f"{prefix}else:")
        lines.append(f"{prefix}    expect(_btn).to_be_enabled(timeout=10000)")
        lines.append(f"{prefix}    _btn.click()")
    elif after_popup_dismiss:
        lines.append(f"{prefix}expect(_btn).to_be_visible(timeout=10000)")
        lines.append(f"{prefix}expect(_btn).to_be_enabled(timeout=10000)")
        lines.append(f"{prefix}_btn.click()")
    else:
        lines.append(f"{prefix}expect(_btn).to_be_visible(timeout=10000)")
        lines.append(f"{prefix}_btn.click()")
    return lines


def _gen_post_wait_lines(spec: dict[str, Any], indent: str = "    ") -> list[str]:
    kind = spec.get("kind")
    if kind == "url_contains":
        frag = _py_str(str(spec.get("fragment", "")))
        return [f"{indent}wait_for_url_fragment(page, {frag})"]
    return []


def _finish_step_lines(
    lines: list[str],
    action: PlannedAction,
    prefix: str = "    ",
    *,
    dismiss_after_nav: bool = False,
) -> None:
    post = (action.extras or {}).get("codegen_post_wait")
    if action.type == "click" and post:
        lines.extend(_gen_post_wait_lines(post, prefix))
    if dismiss_after_nav:
        lines.append(f"{prefix}# 导航后若出现阻断弹窗则关闭 (非用例主步骤)")
        lines.append(f"{prefix}_dismiss_blocking_dialog_if_present(page)")
    lines.append("")


def _gen_assert_count_lines(action: PlannedAction, prefix: str = "    ") -> list[str]:
    op, threshold = _parse_count_spec(action)
    return [f"{prefix}# [{action.type}] {action.intent}", *_gen_list_count_lines(op, threshold, prefix)]


def _advancing_action_type(action: PlannedAction) -> bool:
    return action.type in {"click", "fill", "press", "goto", "upload"}


def _needs_assert_wait(prev: PlannedAction | None) -> bool:
    if not prev or getattr(prev, "is_recovery", False) or prev.type in ("api_call", "bind_session"):
        return False
    return _advancing_action_type(prev)


def _gen_assert_wait_preamble(indent: str = "    ") -> list[str]:
    return [f"{indent}page = wait_before_assert(page)"]


def _gen_list_count_lines(op: str, threshold: int, indent: str = "    ") -> list[str]:
    sym = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "="}.get(op, op)
    min_n = min_count_for_compare(op, threshold)
    if op in (">", ">=", "==") and min_n >= 1:
        measure = f"wait_for_list_count_at_least(page, {min_n})"
    else:
        measure = "measure_list_count(page)"
    return [
        f"{indent}_n, _count_src = {measure}",
        f"{indent}assert compare_count(_n, {threshold}, {_py_str(op)}), "
        f"f'计数断言失败({{_count_src}}): 实际 {{_n}} {sym} {threshold}'",
    ]


def _gen_codegen_assert_lines(
    spec: dict[str, Any],
    api_context: dict[str, Any],
    *,
    indent: str = "    ",
    runtime_api: bool = False,
) -> list[str]:
    """根据执行期回填的 codegen_assert 生成可执行 Playwright 断言."""
    kind = spec.get("kind")
    if kind == "literal":
        text = _value_expr(str(spec.get("text", "")), api_context, runtime_api)
        return [f'{indent}assert {text} in page.inner_text("body")']
    if kind == "negate_literal":
        text = _value_expr(str(spec.get("text", "")), api_context, runtime_api)
        return [f'{indent}assert {text} not in page.inner_text("body")']
    if kind in ("button_disabled", "button_enabled"):
        text = str(spec.get("text") or "")
        want_disabled = kind == "button_disabled"
        name = _py_str(text)
        return [
            f"{indent}_btn = page.get_by_role('button', name={name})",
            f"{indent}if _btn.count() == 0:",
            f"{indent}    _btn = page.get_by_role('link', name={name})",
            f"{indent}assert _btn.count() > 0, '未找到按钮 ' + {name}",
            f"{indent}assert _btn.first.is_disabled() is {want_disabled!r}, "
            f"'按钮 disabled 状态不符'",
        ]
    if kind == "semantic_only":
        intent = str(spec.get("intent") or "")
        return [
            f"{indent}# [语义断言] run.py 已校验, 脚本不重复断言: {intent!r}",
        ]
    if kind == "contains_all":
        texts = [str(t) for t in spec.get("texts") or []]
        regions = spec.get("regions") or ["main", "body"]
        region_expr = ", ".join(_py_str(k) for k in regions)
        lines = [f"{indent}_scope_text = get_scoped_page_text(page, [{region_expr}])"]
        for t in texts:
            te = _value_expr(t, api_context, runtime_api)
            lines.append(
                f"{indent}assert {te} in _scope_text, "
                f"f'区域断言缺少 {_py_str(t)}'"
            )
        return lines
    if kind in ("list_count", "table_rows", "body_total"):
        return _gen_list_count_lines(str(spec.get("op", ">")), int(spec.get("threshold", 0)), indent)
    if kind == "url_contains":
        frag = _py_str(str(spec.get("fragment", "")))
        return [f"{indent}assert {frag} in page.url"]
    if kind == "control_single":
        rmin = int(spec.get("radio_min", 2))
        cmax = int(spec.get("checkbox_max", 0))
        return [
            f"{indent}_stats = page.evaluate(\"\"\"() => {{",
            f"{indent}  const root = document.querySelector('form') || document.body;",
            f"{indent}  const radios = root.querySelectorAll('input[type=radio], .ant-radio-input');",
            f"{indent}  const checks = root.querySelectorAll(",
            f"{indent}    'input[type=checkbox]:not(.ant-checkbox-input), .ant-checkbox-input'",
            f"{indent}  );",
            f"{indent}  return {{ radio: radios.length, checkbox: checks.length }};",
            f'{indent}}}""")',
            f"{indent}assert int(_stats['radio']) >= {rmin} and int(_stats['checkbox']) <= {cmax}, "
            f"f'控件断言(单选): radio={{_stats[\"radio\"]}} checkbox={{_stats[\"checkbox\"]}}'",
        ]
    if kind == "control_multi":
        cmin = int(spec.get("checkbox_min", 2))
        return [
            f"{indent}_stats = page.evaluate(\"\"\"() => {{",
            f"{indent}  const root = document.querySelector('form') || document.body;",
            f"{indent}  const checks = root.querySelectorAll(",
            f"{indent}    'input[type=checkbox]:not(.ant-checkbox-input), .ant-checkbox-input'",
            f"{indent}  );",
            f"{indent}  return {{ checkbox: checks.length }};",
            f'{indent}}}""")',
            f"{indent}assert int(_stats['checkbox']) >= {cmin}, "
            f"f'控件断言(多选): checkbox={{_stats[\"checkbox\"]}}'",
        ]
    if kind == "control_mode":
        intent = spec.get("intent", "")
        return [f"{indent}# 控件断言(执行期通过): {intent!r}", f"{indent}# TODO: 需手动补充控件校验"]
    return [f"{indent}# 未知断言回填: {spec!r}"]


def _gen_assert_text_lines(
    action: PlannedAction,
    api_context: dict[str, Any],
    *,
    indent: str = "    ",
    runtime_api: bool = False,
) -> list[str]:
    if should_skip_or_branch(action):
        return [f"{indent}# (或断言非获胜分支, 已跳过)"]
    cg = (action.extras or {}).get("codegen_assert")
    if cg:
        lines = _gen_codegen_assert_lines(cg, api_context, indent=indent, runtime_api=runtime_api)
        return lines
    val = (action.value or "").strip()
    target = _value_expr(val, api_context, runtime_api) if val else _py_str(action.intent or "")
    if action.negate:
        return [f'{indent}assert {target} not in page.inner_text("body")']
    return [f'{indent}assert {target} in page.inner_text("body")']


def _gen_bind_session_lines(action: PlannedAction, case_id: str, indent: str = "    ") -> list[str]:
    return [
        f"{indent}ok, msg = _run_bind_session(page, api_ctx, {_py_str(case_id)}, {_py_str(action.intent or '')}, _last_click_label)",
        f"{indent}assert ok, msg",
    ]


def _gen_ui_steps(
    actions: list[PlannedAction],
    api_context: dict[str, Any],
    case_id: str = "",
    runtime_api: bool = False,
    popup_dismiss_before: list[str] | None = None,
    popup_dismiss_used: bool = False,
    idempotent_skip: set[str] | None = None,
) -> str:
    """生成 UI 操作步骤."""
    dismiss_before = set(popup_dismiss_before or [])
    idempotent = idempotent_skip or set()
    lines: list[str] = ["    _last_click_label = None"]

    def _click_needs_dismiss(intent: str) -> bool:
        """运行期曾关弹窗: 后续各 click 前均做条件检查."""
        return popup_dismiss_used or intent in dismiss_before

    def _click_needs_dismiss_after_nav(action: PlannedAction) -> bool:
        """导航类点击后弹窗才出现 (如进待前审页后的红线标准)."""
        if not (action.extras or {}).get("codegen_post_wait"):
            return False
        return popup_dismiss_used or action.intent in dismiss_before

    for i, a in enumerate(actions):
        prev = actions[i - 1] if i > 0 else None
        assert_wait = a.is_assert() and _needs_assert_wait(prev)
        if a.type == "api_call":
            if runtime_api:
                lines.append(f"    # Step {i + 1}: API 调用 - {a.intent} (见上方 api_ctx)")
            else:
                lines.append(f"    # Step {i + 1}: API 调用 - {a.intent}")
                lines.append("    # (无 API 配置, 请先跑 run.py 或手动投放数据)")
            lines.append(_gen_step_log_line(i + 1, a))
            lines.append("")
            continue

        if a.type == "bind_session":
            lines.append(f"    # Step {i + 1}: [bind_session] {a.intent}")
            lines.append(_gen_step_log_line(i + 1, a))
            lines.extend(_gen_bind_session_lines(a, case_id))
            lines.append("")
            continue

        is_popup_recovery = _is_popup_recovery_action(a)
        if is_popup_recovery:
            lines.append(f"    # Step {i + 1}: [recovery] {a.intent}")
            lines.append(_gen_step_log_line(i + 1, a))
            lines.extend(_gen_conditional_recovery_action(a, api_context, runtime_api))
            lines.append("")
            continue

        if a.intent in dismiss_before:
            pass  # 关弹窗逻辑合并进 _gen_click_step

        lines.append(f"    # Step {i + 1}: [{a.type}] {a.intent}")
        lines.append(_gen_step_log_line(i + 1, a))
        value = _apply_api_context(a.value, api_context, runtime_api)
        extras = dict(a.extras or {})

        loc_info = a.locator_info or (infer_from_selector(a.selector) if a.selector else None)

        prefix = "    "
        if assert_wait:
            lines.extend(_gen_assert_wait_preamble(prefix))
        if a.type == "assert_count":
            cg = (a.extras or {}).get("codegen_assert")
            if cg:
                lines.extend(_gen_codegen_assert_lines(cg, api_context, indent=prefix, runtime_api=runtime_api))
            else:
                lines.extend(_gen_assert_count_lines(a, prefix=prefix))
            _finish_step_lines(lines, a, prefix, dismiss_after_nav=_click_needs_dismiss_after_nav(a))
            continue

        if not loc_info and not a.selector:
            if a.type == "goto":
                lines.append(f"{prefix}page.goto({_py_str(value)})")
            elif a.type == "wait":
                if value.isdigit():
                    lines.append(f"{prefix}page.wait_for_timeout({value})")
                else:
                    lines.append(f"{prefix}page.wait_for_timeout(1000)")
            elif a.type == "assert_text":
                lines.extend(_gen_assert_text_lines(a, api_context, indent=prefix, runtime_api=runtime_api))
            elif a.type == "assert_table":
                lines.extend(_gen_assert_table_lines(a, api_context, indent=prefix, runtime_api=runtime_api))
            elif a.type == "click" and value.strip():
                loc_expr = _gen_click_loc_expr(a, None, None)
                lines.extend(_gen_click_step(
                    prefix, loc_expr,
                    after_popup_dismiss=_click_needs_dismiss(a.intent),
                    idempotent=a.intent in idempotent,
                ))
                lines.extend(_gen_click_label_assign(a, prefix))
            else:
                lines.append(f"{prefix}# TODO: {a.type} - {a.intent}")
            _finish_step_lines(lines, a, prefix, dismiss_after_nav=_click_needs_dismiss_after_nav(a))
            continue

        # 有执行期选择器时优先用定位器; 否则用 value 文本兜底
        if loc_info or a.selector:
            if a.type == "click" and _is_table_row_click(a):
                lines.extend(_gen_table_row_click_step(
                    a, api_context, runtime_api, prefix=prefix,
                    after_popup_dismiss=_click_needs_dismiss(a.intent),
                    idempotent=a.intent in idempotent,
                ))
                lines.extend(_gen_click_label_assign(a, prefix))
            elif a.type == "click":
                loc_expr = _gen_click_loc_expr(a, loc_info, a.selector)
                lines.extend(_gen_click_step(
                    prefix, loc_expr,
                    after_popup_dismiss=_click_needs_dismiss(a.intent),
                    idempotent=a.intent in idempotent,
                ))
                lines.extend(_gen_click_label_assign(a, prefix))
            else:
                loc = _gen_locator(loc_info or infer_from_selector(a.selector or ""))
                if a.type == "fill":
                    lines.append(f"{prefix}{loc}.fill({_py_str(value)})")
                elif a.type == "press":
                    lines.append(f"{prefix}{loc}.press({_py_str(value or 'Enter')})")
                elif a.type == "hover":
                    lines.append(f"{prefix}{loc}.hover()")
                elif a.type == "upload":
                    lines.append(f"{prefix}{loc}.set_input_files({_py_str(value)})")
                elif a.type == "assert_text":
                    lines.extend(_gen_assert_text_lines(a, api_context, indent=prefix, runtime_api=runtime_api))
                elif a.type == "assert_table":
                    lines.extend(_gen_assert_table_lines(a, api_context, indent=prefix, runtime_api=runtime_api))
                else:
                    lines.append(f"{prefix}# TODO: {a.type} - {a.intent}")
        elif a.type == "click" and value.strip():
            loc_expr = _gen_click_loc_expr(a, None, None)
            lines.extend(_gen_click_step(
                prefix, loc_expr,
                after_popup_dismiss=_click_needs_dismiss(a.intent),
                idempotent=a.intent in idempotent,
            ))
            lines.extend(_gen_click_label_assign(a, prefix))
        elif a.type == "fill" and value.strip():
            lines.append(f"{prefix}# TODO: fill 无选择器 - {a.intent}")

        _finish_step_lines(lines, a, prefix, dismiss_after_nav=_click_needs_dismiss_after_nav(a))
    return "\n".join(lines)


def _gen_assert_table_lines(
    action: PlannedAction,
    api_context: dict[str, Any],
    indent: str = "    ",
    runtime_api: bool = False,
) -> list[str]:
    extras = action.extras or {}
    row_key = _value_expr((action.value or extras.get("row_key") or "").strip(), api_context, runtime_api)
    key_col = str(extras.get("row_key_column") or "工单ID")
    target_col = str(extras.get("column") or "")
    expected = _value_expr(str(extras.get("expected") or extras.get("cell_value") or ""), api_context, runtime_api)
    if not row_key or not target_col or not expected:
        return [f"{indent}# TODO: assert_table 参数不完整 - {action.intent}"]
    return [
        f"{indent}_assert_table_cell(page, "
        f"{row_key}, {_py_str(key_col)}, {_py_str(target_col)}, {expected})",
    ]


_TEMPLATE = '''\
"""自动生成的 Playwright Python 脚本 —— 用例 {case_id}
选择器取自实际执行时由定位链解析出的结果.
独立运行: python <本脚本路径>
连续套件: 使用同批次 playwright_suite.py (同账号仅登录一次, 换角色时重新登录)
"""
from pathlib import Path
import re
import sys
from playwright.sync_api import sync_playwright, expect

PROJECT_ROOT = Path({project_root})
CASE_FILE = {case_file_expr}
{step_log_helper}{api_helper}{bind_helper}{popup_import}{assert_table_helper}
def login(page):
{login_code}

def run_steps(page):
{api_setup}

{popup_preamble}{ui_steps}

def run(page, fresh_session=True):
    print("▶ 用例 {case_id} 开始", flush=True)
    if fresh_session:
        login(page)
    run_steps(page)

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            run(page)
            print("用例 {case_id} 执行完成 ✅ 通过", flush=True)
        except Exception:
            import traceback
            traceback.print_exc()
            page.screenshot(path="error_{case_id_safe}.png")
            print("用例 {case_id} 执行失败 ❌", flush=True)
            sys.exit(1)
        finally:
            browser.close()

if __name__ == "__main__":
    main()
'''


def _safe(name: str) -> str:
    """将 case_id 转成安全文件名片段."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)

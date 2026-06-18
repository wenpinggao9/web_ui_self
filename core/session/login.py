"""步骤④ 自动登录 —— 给所有用例铺好登录态.

流程:
1. force=False 且当前页与 base_url 同域且 URL 不像登录页 → 跳过 (复用登录态)
2. 跳转到 base_url, 等 domcontentloaded (≤20s)
3. 若 URL 已不在登录路由 → 跳过 (之前已登录被重定向)
4. 按优先级尝试多种选择器: (可选)切登录 tab → 填用户名 → 填密码 → 点登录
5. 等网络空闲 (≤5s)

登录页识别 url_hints_login: 只看 hash fragment 首末段 和 path 后缀, 避免 query 误判.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_DEFAULT_USERNAME_SEL = [
    "input[name='username']", "#username", "input[type='text']",
]
_DEFAULT_PASSWORD_SEL = [
    "input[name='password']", "#password", "input[type='password']",
]
# Ant Design 登录按钮文案常为「登 录」(中间有空格), 不能只匹配「登录」
_DEFAULT_SUBMIT_SEL = [
    "button.ant-btn-primary",
    "button:has-text('登 录')",
    "button:has-text('登录')",
    "button[type='submit']",
]
_LOGIN_SUBMIT_NAMES = ("登 录", "登录", "Log in", "Sign in")


def url_hints_login(url: str) -> bool:
    """识别 hash 路由 (#/login) 与 path 路由 (/login) 的登录页."""
    if not url:
        return False
    parsed = urlparse(url)
    # hash 路由: 取 fragment 首末段
    frag = parsed.fragment or ""
    frag_path = frag.split("?")[0].strip("/")
    if frag_path:
        segs = frag_path.split("/")
        if _is_login_token(segs[0]) or _is_login_token(segs[-1]):
            return True
    # path 路由: 取 path 后缀
    path = (parsed.path or "").strip("/")
    if path:
        segs = path.split("/")
        if _is_login_token(segs[-1]):
            return True
    return False


def _is_login_token(seg: str) -> bool:
    return seg.lower() in ("login", "signin", "sign-in", "登录")


def login(
    page: Any,
    settings: dict[str, Any],
    force: bool = True,
    # 【多系统扩展】可选: 验证码登录 + 角色占位符
    verify_code: str = "",
    phone_placeholder: str = "",
    code_placeholder: str = "",
) -> Any:
    """自动登录.

    Args:
        page: Playwright 页面对象
        settings: 配置字典 (来自 config.yaml 的 target/profile)
        force: True=强制登录, False=尝试复用会话
        verify_code: 验证码 (测试环境写死时用, 优先级高于 password)
        phone_placeholder: 手机号输入框占位符文本
        code_placeholder: 验证码输入框占位符文本
    """
    base_url = settings.get("base_url") or ""
    username = str(settings.get("username") or settings.get("phone") or "")
    password = str(settings.get("password") or "")
    login_cfg = _resolve_login_cfg(settings)
    verify_code = verify_code or str(settings.get("verify_code") or "")

    # 验证码模式: 如果传了 verify_code, 用验证码登录逻辑
    if verify_code:
        return _login_with_verify(
            page, base_url, force,
            phone=username, verify_code=verify_code,
            phone_placeholder=phone_placeholder or login_cfg.get("username_placeholder", ""),
            code_placeholder=(
                code_placeholder
                or login_cfg.get("code_placeholder", "")
                or login_cfg.get("password_placeholder", "")
            ),
            submit_name=login_cfg.get("submit_name", "登录"),
            settings=settings,
        )

    # 1. 会话复用判断
    if not force:
        cur = page.url or ""
        if _same_host(cur, base_url) and not url_hints_login(cur):
            return page

    # 2. 跳转登录页
    page.goto(base_url, wait_until="domcontentloaded", timeout=20000)

    # 3. 已被重定向出登录路由 → 已登录
    if not url_hints_login(page.url):
        _wait_idle(page)
        return page

    # 4. 切登录 tab (可选)
    tab_text = login_cfg.get("login_tab_text")
    if tab_text:
        _try_click(page, [f"text={tab_text}", f"button:has-text('{tab_text}')"])

    # 填用户名 / 密码 / 点登录
    _fill_first(page, login_cfg.get("username_selectors") or _DEFAULT_USERNAME_SEL,
                username, placeholder_hint=login_cfg.get("username_placeholder"))
    _fill_first(page, login_cfg.get("password_selectors") or _DEFAULT_PASSWORD_SEL,
                password, placeholder_hint=login_cfg.get("password_placeholder"))
    _click_login_submit(
        page,
        login_cfg.get("submit_name", "登录"),
        list(login_cfg.get("submit_selectors") or []),
    )

    # 5. 等网络空闲
    _wait_idle(page)
    return page


def _resolve_login_cfg(settings: dict[str, Any]) -> dict[str, Any]:
    """合并 settings.login 与业务知识扁平注入的 login_page 字段."""
    cfg = dict(settings.get("login") or {})
    for key in (
        "username_placeholder", "password_placeholder", "code_placeholder",
        "submit_name", "login_tab_text", "login_url",
        "username_selectors", "password_selectors", "submit_selectors",
    ):
        if key in settings and key not in cfg:
            cfg[key] = settings[key]
    return cfg


def _resolve_login_url(base_url: str, settings: dict[str, Any] | None = None) -> str:
    """拼接登录页完整 URL; settings 可含 login_url 相对路径."""
    cfg = _resolve_login_cfg(settings or {})
    rel = str(cfg.get("login_url") or "").strip()
    if not rel:
        return base_url
    if rel.startswith("http"):
        return rel
    base = (base_url or "").rstrip("/")
    if not rel.startswith("/"):
        rel = "/" + rel
    # base_url 已含 /video 时避免重复拼接
    if base.endswith(rel.rstrip("/")) or rel in base:
        return base
    return base + rel


def _fill_login_input(
    page: Any,
    value: str,
    placeholder: str,
    fallbacks: tuple[str, ...] = (),
) -> bool:
    """填登录表单输入框: 精确 placeholder → 模糊 placeholder → 通用选择器."""
    if _fill_first(page, [], value, placeholder_hint=placeholder):
        return True
    for hint in fallbacks:
        try:
            page.get_by_placeholder(hint, exact=False).first.fill(value, timeout=3000)
            return True
        except Exception:
            continue
    return _fill_first(page, _DEFAULT_USERNAME_SEL, value, placeholder_hint=None)


def _wait_post_login(page: Any, timeout_ms: int = 15000) -> None:
    """提交登录后等待 URL 离开登录路由; 超时说明登录未成功."""
    try:
        page.wait_for_function(
            "() => !/\\/login\\b|登录/i.test(location.pathname + location.hash)",
            timeout=timeout_ms,
        )
    except Exception as e:
        raise RuntimeError(f"登录后仍停留在登录页: {page.url}") from e
    _wait_idle(page)


def _login_with_verify(
    page: Any,
    base_url: str,
    force: bool,
    phone: str,
    verify_code: str,
    phone_placeholder: str = "",
    code_placeholder: str = "",
    submit_name: str = "登录",
    settings: dict[str, Any] | None = None,
) -> Any:
    """手机号 + 验证码登录 (如 VIP 视频系统)."""
    # 会话复用判断
    if not force:
        cur = page.url or ""
        if _same_host(cur, base_url) and not url_hints_login(cur):
            return page

    # 跳转 (优先使用 login_url 直达登录页)
    page.goto(_resolve_login_url(base_url, settings or {}), wait_until="domcontentloaded", timeout=20000)
    # 等待登录表单渲染 (手机号输入框出现)
    try:
        page.wait_for_selector("input[placeholder]", timeout=5000)
    except Exception:
        pass  # 非阻塞, 继续尝试填值

    # 已登录 (URL 不在登录路由)
    if not url_hints_login(page.url):
        _wait_idle(page)
        return page

    if not phone:
        raise RuntimeError("验证码登录缺少手机号 (检查项目配置 roles.username)")

    # 填手机号
    if not _fill_login_input(page, phone, phone_placeholder, fallbacks=("手机号",)):
        raise RuntimeError(f"验证码登录: 未能填入手机号 (placeholder={phone_placeholder!r})")

    # 填验证码 (测试环境写死, 不点「获取验证码」)
    if not _fill_login_input(page, verify_code, code_placeholder, fallbacks=("验证码",)):
        raise RuntimeError(f"验证码登录: 未能填入验证码 (placeholder={code_placeholder!r})")

    # 点登录并等待离开登录页 (Ant Design: <button class="ant-btn-primary"><span>登 录</span>)
    cfg = _resolve_login_cfg(settings or {})
    if not _click_login_submit(page, submit_name, list(cfg.get("submit_selectors") or [])):
        raise RuntimeError("验证码登录: 未能点击登录按钮")
    _wait_post_login(page)
    return page


# ---------- 辅助 ----------
def _same_host(a: str, b: str) -> bool:
    try:
        return urlparse(a).netloc == urlparse(b).netloc and bool(urlparse(a).netloc)
    except Exception:
        return False


def _wait_idle(page: Any) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _fill_first(page: Any, selectors: list[str], value: str, placeholder_hint: str | None = None) -> bool:
    cands = list(selectors)
    if placeholder_hint:
        cands = [f"input[placeholder='{placeholder_hint}']"] + cands
    for sel in cands:
        try:
            loc = page.locator(sel).first
            loc.fill(value, timeout=2000)
            return True
        except Exception:
            continue
    # 占位符兜底
    if placeholder_hint:
        try:
            page.get_by_placeholder(placeholder_hint).first.fill(value, timeout=2000)
            return True
        except Exception:
            pass
    return False


def _click_login_submit(page: Any, submit_name: str, extra_selectors: list[str] | None = None) -> bool:
    """点击登录提交按钮; 兼容 Ant Design「登 录」与连续「登录」两种文案."""
    names = []
    for n in (submit_name, *_LOGIN_SUBMIT_NAMES):
        if n and n not in names:
            names.append(n)
        # Ant Design 常在两字间加空格
        if n == "登录" and "登 录" not in names:
            names.append("登 录")
        if n == "登 录" and "登录" not in names:
            names.append("登录")

    sels = list(extra_selectors or []) + _DEFAULT_SUBMIT_SEL
    if _click_first(page, sels):
        return True
    for name in names:
        try:
            page.get_by_role("button", name=name, exact=True).first.click(timeout=3000)
            return True
        except Exception:
            pass
        try:
            page.get_by_role("button", name=name, exact=False).first.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def _click_first(page: Any, selectors: list[str], role_name: str | None = None) -> bool:
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=2000)
            return True
        except Exception:
            continue
    if role_name:
        try:
            page.get_by_role("button", name=role_name).first.click(timeout=2000)
            return True
        except Exception:
            pass
    return False


def _try_click(page: Any, selectors: list[str]) -> bool:
    return _click_first(page, selectors)

"""页面就绪守卫: DOM 抽取前确保页面稳定 (对齐 V3)."""
from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


class PageNotReadyError(Exception):
    """页面未就绪."""


class PageReadyGuard:
    PREPARATION_KEYWORDS = {
        "登录", "登入", "login", "signin",
        "授权", "认证", "auth",
        "重置密码", "reset password",
        "首次", "first time",
        "欢迎", "welcome",
    }
    LOGIN_PAGE_INDICATORS = {
        "登录", "登入", "login", "signin", "sign in",
        "用户名", "username", "密码", "password",
        "忘记密码", "forgot password",
    }
    LOADING_INDICATORS = {
        "loading", "loading...",
        "加载中", "页面加载中", "请稍候", "请稍等",
        "initializing", "初始化中",
    }

    @staticmethod
    def ensure_ready(page: Any, intent: str, max_wait_time: int = 5000) -> None:
        if PageReadyGuard._is_preparation_phase(intent):
            return
        try:
            page.wait_for_load_state("load", timeout=max_wait_time)
            time.sleep(0.3)
        except Exception as exc:
            logger.warning("等待页面 load 超时: %s", exc)
        PageReadyGuard._retry_if_loading(page, intent, max_wait_time=max_wait_time)
        if not PageReadyGuard._page_matches_intent(page, intent):
            raise PageNotReadyError(
                f"页面未就绪: 检测到登录页但 intent 为业务操作: {intent}"
            )
        if PageReadyGuard._has_blocking_dialog(page):
            logger.warning("检测到阻塞性弹窗, 页面可能未就绪")

    @staticmethod
    def _retry_if_loading(page: Any, intent: str, max_wait_time: int = 5000) -> None:
        if PageReadyGuard._is_preparation_phase(intent):
            return
        deadline = time.time() + max(max_wait_time, 1000) / 500.0
        retry = 0
        while time.time() < deadline:
            is_loading, reason = PageReadyGuard._is_loading_state(page)
            if not is_loading:
                return
            retry += 1
            logger.info("页面仍在加载(%s), 等待重试 #%d", reason, retry)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=1000)
            except Exception:
                pass
            time.sleep(0.5)

    @staticmethod
    def _is_loading_state(page: Any) -> tuple[bool, str]:
        try:
            current_url = (page.url or "").lower()
            if "/skip/login" in current_url:
                return True, "URL 仍在 /skip/login"
            try:
                visible_text = (page.inner_text("body") or "").strip()
            except Exception:
                visible_text = ""
            text_lower = visible_text.lower()
            for kw in PageReadyGuard.LOADING_INDICATORS:
                if kw in text_lower:
                    return True, f"含加载关键词: {kw}"
            if 0 < len(visible_text) <= 30 and ("loading" in text_lower or "加载" in visible_text):
                return True, "可见文本过少且疑似加载壳"
            return False, ""
        except Exception:
            return False, ""

    @staticmethod
    def _is_preparation_phase(intent: str) -> bool:
        intent_lower = (intent or "").lower()
        return any(k.lower() in intent_lower for k in PageReadyGuard.PREPARATION_KEYWORDS)

    @staticmethod
    def _page_matches_intent(page: Any, intent: str) -> bool:
        try:
            html_content = page.content().lower()
            try:
                visible_text = page.inner_text("body").lower()
            except Exception:
                visible_text = html_content
            current_url = (page.url or "").lower()
            url_is_login = (
                "/login" in current_url or "/signin" in current_url
                or current_url.endswith("/login") or current_url.endswith("/signin")
            )
            has_login_keywords = any(
                ind in html_content or ind in visible_text
                for ind in PageReadyGuard.LOGIN_PAGE_INDICATORS
            )
            has_login_form = False
            try:
                pw = page.locator("input[type='password']")
                if pw.count() > 0:
                    pw.first.wait_for(state="visible", timeout=500)
                    has_login_form = True
            except Exception:
                pass
            is_login_page = url_is_login or (has_login_keywords and has_login_form)
            if is_login_page and not PageReadyGuard._is_preparation_phase(intent):
                return False
            return True
        except Exception as exc:
            logger.warning("页面内容匹配检查失败: %s", exc)
            return True

    @staticmethod
    def _has_blocking_dialog(page: Any) -> bool:
        try:
            for selector in (
                "div[role='dialog']", ".el-dialog", ".ant-modal",
                ".modal", "[class*='dialog']", "[class*='modal']",
            ):
                try:
                    dialog = page.locator(selector).first
                    dialog.wait_for(state="visible", timeout=500)
                    text = dialog.inner_text(timeout=500).lower()
                    if any(k in text for k in ("登录", "登入", "授权", "login", "auth")):
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

"""Profile 管理器 —— 系统配置 (profiles) + 项目会话 (sessions) 的加载与解析.

核心概念:
  - **profile**: 系统定义 (base_url, APIs, 枚举值, TID 池, 导航, 技能), 长期不变.
  - **session**: 当前测试项目的具体配置 (target_system, 角色账号), 每次测试可能不同.

旧 target 段作为 default profile, 完全向后兼容.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ApiTemplate:
    """一个 API 端点定义."""
    method: str                         # GET / POST
    url: str                            # 相对路径或完整 URL
    type: str = "http"                  # http | db
    base_url: str = ""                  # 可选, 覆盖 profile.api_base_url
    body: Optional[dict[str, Any]] = None   # POST 请求体, 支持 ${var} 占位符
    params: Optional[dict[str, Any]] = None  # GET 查询参数, 支持 ${var} 占位符
    returns: list[str] = field(default_factory=list)  # 从响应中提取的变量名
    keywords: list[str] = field(default_factory=list)  # 用于自然语言匹配的关键词
    retry: dict[str, Any] = field(default_factory=dict)  # 重试配置 {on_error, max_attempts}
    param_rules: list[dict[str, Any]] = field(default_factory=list)  # 参数提取规则 [{field, enum}, ...]


@dataclass
class SystemProfile:
    """一个系统的完整定义."""
    name: str                           # profile key, 如 "vip_video"
    base_url: str                       # 系统基址 (登录/导航用)
    api_base_url: str = ""              # API 根域名 (和 base_url 可能不同)
    # 登录: 单账号模式 (username/password) 或 多角色模式 (roles dict)
    login_username: str = ""
    login_password: str = ""
    login_roles: dict[str, dict[str, str]] = field(default_factory=dict)
    # DB 配置 (用于 query_tid 等 DB 查询)
    database: dict[str, Any] = field(default_factory=dict)
    # API 模板
    apis: dict[str, ApiTemplate] = field(default_factory=dict)
    # 枚举值映射
    enums: dict[str, dict[str, int]] = field(default_factory=dict)
    # 资源路径
    resources: dict[str, str] = field(default_factory=dict)
    # 导航静态映射
    menu_selectors: dict[str, Any] = field(default_factory=dict)
    # 技能知识 (文件路径或文本)
    skill_path: str = ""


@dataclass
class SessionConfig:
    """一个测试项目的会话配置."""
    name: str                           # session key, 如 "大学增加前审"
    target_system: str                  # 对应的 profile key
    roles: dict[str, dict[str, str]] = field(default_factory=dict)  # role_name -> {username, verify_code}


class ProfileManager:
    """解析多系统、多会话配置, 并为用例选择对应登录/API Profile."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.profiles: dict[str, SystemProfile] = {}
        self.sessions: dict[str, SessionConfig] = {}
        # 旧 target 段作为 default profile
        self.default_profile = _parse_default_target(config.get("target", {}))
        self._load_profiles(config.get("profiles", {}))
        self._load_sessions(config.get("sessions", {}))
        # 兜底账号
        self.fallback: dict[str, dict[str, Any]] = config.get("fallback_accounts", {}) or {}

    def resolve(self, case) -> tuple[SystemProfile, SessionConfig]:
        """根据用例的 session_name/target_system/role 返回 profile + session.

        返回 (profile, session), 其中:
        - profile.base_url 用于浏览器导航
        - session.roles[case.role] 用于登录
        - profile.apis 用于前置环节 API 调用
        - 如果用例不指定 session, session.roles 为空, profile 走 default_profile
        """
        session = self.sessions.get(case.session_name) if case.session_name else None
        if session is not None:
            # session 明确指定目标系统, 用于一个测试项目绑定一套系统与角色账号.
            profile = self.profiles.get(session.target_system)
            if profile is not None:
                return profile, session
            raise ValueError(f"session '{case.session_name}' 引用了不存在的 target_system '{session.target_system}'")

        # 无 session → 用用例的 target_system 直接找 profile
        # 适合用例直接声明系统, 不需要单独 session 包装的场景.
        profile = self.profiles.get(case.target_system)
        if profile is not None:
            # 尝试找到匹配 target_system 的已注册 session (获取其 roles)
            for sess in self.sessions.values():
                if sess.target_system == case.target_system:
                    return profile, sess
            return profile, SessionConfig(name="", target_system=case.target_system)

        # 都未指定 → 走 default (旧 target 段)
        return self.default_profile, SessionConfig(name="", target_system="default")

    def get_credentials(self, session: SessionConfig, role: str) -> tuple[str, str]:
        """获取登录账号 (username, password_or_verify_code).
        优先级: session.roles > fallback_accounts > profile 单账号.
        """
        if session.roles and role:
            # 多角色模式优先使用 session 中为本次测试项目配置的角色账号.
            creds = session.roles.get(role)
            if creds:
                return (
                    creds.get("username", ""),
                    creds.get("verify_code") or creds.get("password", ""),
                )
        # 回退到兜底账号
        fallback_sys = self.fallback.get(session.target_system) if session.target_system else None
        if fallback_sys and role:
            roles = fallback_sys.get("roles", {})
            creds = roles.get(role)
            if creds:
                return (
                    creds.get("username", ""),
                    creds.get("verify_code") or creds.get("password", ""),
                )
        # 回退到 profile 的单账号模式
        profile = self.profiles.get(session.target_system) if session.target_system else None
        if profile:
            return (profile.login_username, profile.login_password)
        return ("", "")

    def _load_profiles(self, profiles_cfg: dict[str, Any]) -> None:
        """加载 profiles 配置段, 构建系统级长期配置."""
        for name, cfg in profiles_cfg.items():
            if not isinstance(cfg, dict):
                continue
            login_cfg = cfg.get("login", {}) or {}
            profile = SystemProfile(
                name=name,
                base_url=cfg.get("base_url", ""),
                api_base_url=cfg.get("api_base_url", ""),
                login_username=login_cfg.get("username", ""),
                login_password=login_cfg.get("password", ""),
                login_roles=login_cfg.get("roles", {}) or {},
                database=cfg.get("database", {}) or {},
                apis=_parse_apis(cfg.get("apis", {}) or {}),
                enums=cfg.get("enums", {}) or {},
                resources=cfg.get("resources", {}) or {},
                menu_selectors=cfg.get("navigation", {}).get("menu_selectors", {}) if isinstance(cfg.get("navigation"), dict) else {},
                skill_path=str(cfg.get("skills", "")),
            )
            self.profiles[name] = profile

    def _load_sessions(self, sessions_cfg: dict[str, Any]) -> None:
        """加载 sessions 配置段, 构建测试项目级会话配置."""
        for name, cfg in sessions_cfg.items():
            if not isinstance(cfg, dict):
                continue
            session = SessionConfig(
                name=name,
                target_system=cfg.get("target_system", ""),
                roles=cfg.get("roles", {}) or {},
            )
            self.sessions[name] = session


def _parse_default_target(target_cfg: dict[str, Any]) -> SystemProfile:
    """把旧 target 段转成 SystemProfile (向后兼容)."""
    login_cfg = target_cfg.get("login", {}) or {}
    return SystemProfile(
        name="default",
        base_url=target_cfg.get("base_url", ""),
        login_username=target_cfg.get("username", login_cfg.get("username", "")),
        login_password=target_cfg.get("password", login_cfg.get("password", "")),
        login_roles=login_cfg.get("roles", {}) or {},
    )


def _parse_apis(apis_cfg: dict[str, Any]) -> dict[str, ApiTemplate]:
    """把配置中的 API 模板转换成 ApiTemplate 字典."""
    out: dict[str, ApiTemplate] = {}
    for name, cfg in apis_cfg.items():
        if not isinstance(cfg, dict):
            continue
        out[name] = ApiTemplate(
            method=str(cfg.get("method", "GET")).upper(),
            url=str(cfg.get("url", "")),
            type=str(cfg.get("type", "http")),
            base_url=str(cfg.get("base_url", "") or ""),
            body=cfg.get("body"),
            params=cfg.get("params"),
            returns=cfg.get("returns", []) or [],
            keywords=cfg.get("keywords", []) or [],
            retry=cfg.get("retry", {}) or {},
            param_rules=cfg.get("param_rules", []) or [],
        )
    return out

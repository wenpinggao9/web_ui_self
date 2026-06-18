"""业务目录加载器 —— 从用例文件路径向上自动发现并加载业务配置.

目录约定:
  业务/<系统名>/
    ├── 业务知识.md          # API/枚举/TID池等 (长期不变)
    └── <项目名>/
        ├── 项目配置.yaml    # 账号、base_url (每次迭代可能不同)
        └── cases/
            └── 测试用例.md

执行 python run.py 业务/vip视频/大学增加前审/cases/ 时:
  1. 向上找到 项目配置.yaml → 取 roles + base_url
  2. 继续向上找到 业务知识.md → 取 apis + enums
"""
from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any


class BusinessLoader:
    def __init__(self) -> None:
        self.business_dir: Path | None = None
        self.project_dir: Path | None = None
        self.knowledge: dict[str, Any] = {}
        self.project_config: dict[str, Any] = {}

    def discover(self, case_file: str | Path) -> bool:
        """从用例文件路径向上扫描, 找到业务目录和项目配置."""
        p = Path(case_file).resolve()

        # 1. 向上找到包含 业务知识.md 的目录 (即业务系统目录, 如 业务/vip视频/)
        system_dir = None
        parent = p.parent
        while True:
            if (parent / "业务知识.md").exists():
                system_dir = parent
                break
            if parent == parent.parent:
                break
            parent = parent.parent

        if not system_dir:
            return False

        self.system_dir = system_dir
        self.business_dir = system_dir  # 别名, 兼容旧引用
        self.knowledge = {}  # 存储业务知识

        # 2. 继续向上找 项目配置.yaml
        proj_dir = p.parent
        while proj_dir != system_dir:
            cfg = proj_dir / "项目配置.yaml"
            if cfg.exists():
                self.project_dir = proj_dir
                self.project_config = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                break
            proj_dir = proj_dir.parent

        # 3. 加载业务知识
        kb_file = system_dir / "业务知识.md"
        if kb_file.exists():
            raw = kb_file.read_text(encoding="utf-8")
            self.knowledge = _parse_kb(raw)
        return bool(self.knowledge or self.project_config)

    def get_knowledge(self) -> dict:
        """返回完整业务知识."""
        return self.knowledge

    def get_base_url(self) -> str:
        return self.project_config.get("base_url", "")

    def get_login_page(self) -> dict[str, Any]:
        return self.knowledge.get("login_page", {})

    def get_roles(self) -> dict[str, dict[str, str]]:
        return self.project_config.get("roles", {})

    def get_apis(self) -> dict[str, Any]:
        return self.knowledge.get("apis", {})

    def get_enums(self) -> dict[str, Any]:
        return self.knowledge.get("enums", {})

    def get_resources(self) -> dict[str, str]:
        return self.knowledge.get("resources", {})

    def build_system_profile(self):
        """从已加载的业务知识构建 SystemProfile (供 API 调用复用)."""
        from .profile import ApiTemplate, SystemProfile

        apis = {}
        for name, cfg in self.get_apis().items():
            if not isinstance(cfg, dict):
                continue
            apis[name] = ApiTemplate(
                method=str(cfg.get("method", "GET")).upper(),
                url=str(cfg.get("url", "")),
                type=str(cfg.get("type", "http")),
                base_url=str(cfg.get("base_url", "") or ""),
                body=cfg.get("body"),
                params=cfg.get("params"),
                returns=cfg.get("returns", []),
                keywords=cfg.get("keywords", []),
                retry=cfg.get("retry", {}),
                param_rules=cfg.get("param_rules", []),
            )
        return SystemProfile(
            name=self.system_dir.name if self.system_dir else "default",
            base_url=self.get_base_url(),
            api_base_url=self.get_knowledge().get("api_base_url", ""),
            database=self.get_knowledge().get("database", {}),
            apis=apis,
            enums=self.get_enums() or {},
            resources=self.get_resources() or {},
        )


def _parse_kb(text: str) -> dict[str, Any]:
    """解析 业务知识.md 中的 YAML frontmatter."""
    t = text.lstrip()
    if not t.startswith("---"):
        return {}
    parts = t.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1].strip()) or {}
    except Exception:
        return {}

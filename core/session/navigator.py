"""步骤⑤ 模块导航 —— 从首页到目标功能页.

三层菜单来源: 静态映射(最高) > 动态扫描(中) > 合并(静态优先).
阶段A: 静态映射为空时跳过 (不阻塞), 由用例操作步骤自行导航.
"""
from __future__ import annotations

from typing import Any, Optional

from rich.console import Console

from .feature_selectors import FEATURE_SELECTORS
from .menu_scanner import scan_menus


class Navigator:
    """负责用例开始后的模块导航, 将 module_path 映射为菜单点击."""

    def __init__(self, console: Optional[Console] = None) -> None:
        self.console = console or Console()
        self._menu_cache: Optional[dict[str, str]] = None

    def ensure_menu_cache(self, page: Any) -> None:
        """懒加载动态菜单扫描结果, 同一批次内避免重复扫描."""
        if self._menu_cache is None:
            self._menu_cache = scan_menus(page)  # 阶段A 返回 {}

    def navigate(self, page: Any, module_path: list[str], default_timeout_ms: int = 10000) -> Any:
        """按 module_path 逐级点菜单. 无映射则跳过."""
        self.ensure_menu_cache(page)
        if not module_path:
            return page

        node = FEATURE_SELECTORS
        for level in module_path:
            sel = self._lookup(node, level)
            if sel is None:
                # 无映射时不失败, 因为很多用例会在操作步骤里自行点击入口.
                self.console.print(f"[yellow]导航: 模块 '{level}' 无静态映射, 跳过 (依赖用例步骤导航)[/yellow]")
                continue
            menu_sel = sel.get("菜单") if isinstance(sel, dict) else sel
            try:
                # 每一级点击后等待 DOM ready, 给下一级菜单/页面内容加载时间.
                page.locator(menu_sel).first.click(timeout=default_timeout_ms)
                page.wait_for_load_state("domcontentloaded", timeout=default_timeout_ms)
            except Exception as e:  # noqa: BLE001
                self.console.print(f"[yellow]导航点击失败 '{level}': {e}[/yellow]")
            node = sel.get("子菜单", {}) if isinstance(sel, dict) else {}
        return page

    @staticmethod
    def _lookup(node: dict, key: str):
        """在当前菜单节点中查找模块名, 支持精确匹配和最长子串匹配."""
        if not isinstance(node, dict):
            return None
        if key in node:
            return node[key]
        # 最长子串匹配
        best = None
        for k in node:
            if k in key or key in k:
                if best is None or len(k) > len(best):
                    best = k
        return node.get(best) if best else None

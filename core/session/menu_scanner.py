"""步骤⑤ 动态菜单扫描 —— JS 遍历侧栏 DOM 提取 模块名+选择器 (留桩, 阶段C 补全).

阶段A 返回空字典: 导航完全依赖静态映射 + 用例操作步骤.
"""
from __future__ import annotations

from typing import Any

_SCAN_JS = r"""
() => {
  const out = [];
  // 预留: 后续可把 name 映射到可复用 selector, 当前阶段只扫描文本.
  document.querySelectorAll(".el-menu-item, .ant-menu-item, [role=menuitem], nav a").forEach(el => {
    const text = (el.innerText || '').trim();
    if (text) out.push({ name: text });
  });
  return out;
}
"""


def scan_menus(page: Any) -> dict[str, str]:
    """扫描侧栏菜单 → {模块名: 选择器}. 阶段A 不构建选择器, 返回空."""
    # 保留 page 参数和 JS 草稿, 方便阶段C 接入动态菜单定位时不改调用方.
    return {}

"""步骤⑤ 静态映射 —— 人工写死的 模块→选择器 字典 (最高可靠性).

结构:
  FEATURE_SELECTORS = {
      "个人中心": {
          "菜单": ".el-menu-item:has-text('个人中心')",
          "子菜单": {"收货地址": ".el-menu-item:has-text('收货地址')"},
      }
  }
为空时导航器会跳过 (由用例操作步骤自行导航). 针对侧栏菜单类应用时在此补充.
"""
from __future__ import annotations

# 项目级静态菜单映射入口.
# 默认留空代表不做自动模块导航, 用例步骤自行完成页面进入.
FEATURE_SELECTORS: dict[str, dict] = {}

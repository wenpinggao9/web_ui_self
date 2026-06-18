---
components:
  - name: 下拉选择 el-select
    category: select
    html: |
      <div class="el-select">
        <div class="el-select__wrapper" role="combobox" aria-haspopup="listbox">
          <input class="el-select__input" />
          <span class="el-select__placeholder">请选择</span>
        </div>
      </div>
      <!-- 展开后选项面板 (teleport 到 body) -->
      <div class="el-select-dropdown">
        <ul><li class="el-select-dropdown__item">选项文本</li></ul>
      </div>
  - name: 级联地区选择
    category: select
    html: |
      <!-- 省/市/区常共用一个表单项, 三个 el-select 按顺序排列; 市/区选项依赖上一级选定后才加载 -->
      <div class="el-form-item">
        <label>所在地区</label>
        <div class="el-select"></div><div class="el-select"></div><div class="el-select"></div>
      </div>
  - name: 树形选择 el-tree
    category: tree
    html: |
      <div class="el-tree">
        <div class="el-tree-node">
          <span class="el-tree-node__expand-icon"></span>
          <span class="el-checkbox"></span>
          <span class="el-tree-node__label">节点</span>
        </div>
      </div>
  - name: 复选框 el-checkbox
    category: checkbox
    html: |
      <label class="el-checkbox" role="checkbox"><span class="el-checkbox__input"><input type="checkbox"></span><span class="el-checkbox__label">文本</span></label>
  - name: 开关 el-switch
    category: switch
  - name: 对话框 el-dialog
    category: dialog
  - name: 商品卡片(Tailwind div)
    category: card
framework_detect:
  - name: ant-design
    check: '.ant-modal, .ant-select-dropdown, [class*="ant-pro-"]'
  - name: element-plus
    check: '.el-dialog, .el-select-dropdown, .el-message-box'
  - name: element-ui
    check: '.el-dialog, .el-select-dropdown, .el-message-box'
  - name: naive-ui
    check: '[class*="n-modal"], [class*="n-select"]'
  - name: arco-design
    check: '[class*="arco-modal"], [class*="arco-select"]'
framework_selectors:
  ant-design:
    container_sel: '.ant-select, .ant-picker, [aria-haspopup]'
    dropdown_sel: '.ant-select-dropdown, .ant-dropdown, [role="listbox"], [role="menu"]'
    option_sel: '.ant-select-item-option, .ant-dropdown-menu-item, [role="option"]'
    dialog_sel: '[role="dialog"], .ant-modal, .ant-modal-confirm'
    form_sel: 'form, .ant-form'
  element-plus:
    container_sel: '.el-select, .el-date-editor, [aria-haspopup]'
    dropdown_sel: '.el-select-dropdown, .el-picker-panel, [role="listbox"], [role="menu"]'
    option_sel: '.el-select-dropdown__item, .el-dropdown-menu__item, [role="option"]'
    dialog_sel: '[role="dialog"], .el-dialog, .el-message-box, .el-drawer'
    form_sel: 'form, .el-form'
  element-ui:
    container_sel: '.el-select, [aria-haspopup]'
    dropdown_sel: '.el-select-dropdown, .el-dropdown-menu, [role="listbox"], [role="menu"]'
    option_sel: '.el-select-dropdown__item, .el-dropdown-menu__item, [role="option"]'
    dialog_sel: '[role="dialog"], .el-dialog, .el-message-box, .el-drawer'
    form_sel: 'form, .el-form'
  naive-ui:
    container_sel: '.n-select, [aria-haspopup]'
    dropdown_sel: '.n-base-select-menu, .n-dropdown-menu, [role="listbox"], [role="menu"]'
    option_sel: '.n-base-select-menu-item, .n-dropdown-menu-item, [role="option"]'
    dialog_sel: '[role="dialog"], .n-modal, .n-dialog'
    form_sel: 'form, .n-form'
  arco-design:
    container_sel: '.arco-select, [aria-haspopup]'
    dropdown_sel: '.arco-select-dropdown, .arco-dropdown, [role="listbox"], [role="menu"]'
    option_sel: '.arco-select-option, .arco-dropdown-option, [role="option"]'
    dialog_sel: '[role="dialog"], .arco-modal, .arco-modal-confirm'
    form_sel: 'form, .arco-form'
---

# 组件操作语义 (供动作规划参考)

- **下拉选择 el-select**: 不是原生 `<select>`, 不能 select_option. 必须拆成两步:
  ① 点击下拉框容器展开; ② 在弹出选项中点击目标选项. 规划时分别产出"点击X下拉框"和"在弹出选项中点击Y"两条动作.
- **级联地区选择(省/市/区)**: 三个下拉按顺序操作, 选完省份才能选城市, 选完城市才能选区县. 顺序: 点省→选省值→点市→选市值→点区→选区值.
- **树形选择**: 展开/收起点 `展开图标`; 勾选点节点上的复选框. "勾选树节点X"要分清是展开还是勾选.
- **复选框/开关**: 直接点击对应标签或开关本体.
- **对话框 el-dialog**: 关闭点右上角关闭按钮(× 图标, 通常 `.el-dialog__headerbtn`). "关闭X弹窗"产出一条点击动作即可.
- **商品卡片**: 列表里的卡片常是无语义 class 的 div(@click 跳详情), 点击商品名文本即可冒泡触发. "点击第一个商品X"产出一条点击动作.

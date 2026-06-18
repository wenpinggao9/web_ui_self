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
entrypoints:
  build_dropdown_option_selector:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: build_dropdown_option_selector
  build_el_select_trigger_selector:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: build_el_select_trigger_selector
  build_checkbox_selector:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: build_checkbox_selector
  build_tree_checkbox_selector:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: build_tree_checkbox_selector
  build_tree_node_selector:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: build_tree_node_selector
  build_date_picker_selector:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: build_date_picker_selector
  choose_best_click_target:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: choose_best_click_target
  choose_best_input_target:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: choose_best_input_target
  choose_best_checkbox_target:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: choose_best_checkbox_target
  find_switch_in_row:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: find_switch_in_row
---

# UI Agent Skill (组件库适配与通用性)

本 Skill 文件用于指导 UI Agent 中的大模型（动作规划器和元素选择器），在 **不绑定某个特定组件库** 的前提下，更通用地理解和操作页面。

适用对象：

- `core/planning/action_planner.py` — 动作规划 (步骤⑥, 注入本文件正文)
- `core/locating/llm_decider.py` + `prompts/element_decide.system.md` — L5 元素决策大模型
- `core/skill_loader.py` — 读取本文件 YAML frontmatter (组件索引、框架探测、选择器模板)

1. 页面可能使用多种 UI 组件库（Element UI/Element Plus、Ant Design、Naive UI、自研组件库等），**你不应假设只存在某一个特定组件库**。
2. 当用例或意图中出现「下拉框」「下拉选项」「弹窗」「表格」「筛选栏」「搜索输入框」等词时：
   - 这些词表示的是**交互语义**（用户期望在界面上看到/操作的组件类型），
   - 而不是某个 UI 库的具体实现或 class 名。
3. 你的决策（动作规划或元素选择）应基于：
   - 自然语言意图（步骤/预期描述）
   - 当前页面 DOM 的语义结构（标签、role、aria 属性、文案文本、上下文层级）
   - 而不是绑定到某个库特定的 class 命名。

## 二、对动作规划器 (Action Planner) 的要求

1. 在生成动作列表 (`type` + `intent` + `value` + `extra`) 时：
    - 使用**语义化描述**来表达组件和位置，例如：
        - ✅ 「点击下拉框展开按钮」
        - ✅ 「在弹窗中的"角色名称"输入框输入 xxx」
        - ✅ 「点击对比结果页面中的"同步滚动"开关」
        - ❌ 「点击 el-select 的下拉箭头」
        - ❌ 「点击 ant-select 下拉」
    - 不要在 `intent` 中嵌入具体 CSS/class/XPath 或特定组件库前缀。

2. 当用例中提到「下拉框/下拉选项」时：
    - 对**步骤意图**要清晰表达：
        - 是点击下拉框本身（触发器），还是点击展开后的某个选项；
        - 该下拉框/选项处于什么上下文中（弹窗内、表格上方的筛选栏、导航栏等）。
    - 示例：
        - 「在"原文文档名称"筛选下拉栏中输入 test」
        - 「点击"原文文档名称"下拉框中的"test_xxx.docx"选项」

3. 当用例中提到「弹窗/对话框/Modal」时：
    - 在 `intent` 中保留弹窗的语义信息：
        - 「在"新建角色"弹窗中的"角色名称"输入框输入 xxx」
        - 「点击"对比结果"弹窗中的"确认"按钮」
    - 不要写成依赖某个具体 UI 库的命名。

4. 当用例中提到「表格/列表」时：
    - 明确是「表格中的某一行/某一列/操作列按钮」，
    - 例如：「点击文件列表中 'test_xxx' 这一行的 '查看' 按钮」。

5. **等待动作 (wait) **:
    - 只有在步骤明确提到"等待/稍等/停顿/等待某文本出现或消失"时才使用 'type="wait"'。
    - 对于"等待页面加载完成""等待表格加载完成"这类：
        - 可以生成 'wait' 动作，'intent' 中语义化描述 (例如「等待表格数据加载完成」)，
        - 执行阶段会根据 DOM/文本/超时策略自行判断，不需要你推断具体组件库行为。

## 三、对元素选择器 (Element Selector) 的要求
  - 用户动作意图 (`intent`)；
  - 动作类型 (`action_type`) 和可选的 `action_value`。

2. 在识别下拉、弹窗、表格等结构时，遵循以下优先级：

### 3.1 通用结构优先

- **下拉 / 下拉选项**：
  - 优先考虑：
    - 原生 `<select>` / `<option>`
    - `role="combobox"`, `role="listbox"`, `role="option"` 的组合
    - 有 `aria-expanded`, `aria-haspopup` 等属性的元素
- **弹窗 / 对话框**：
  - 优先考虑：
    - `<dialog>` 标签
    - `role="dialog"` / `role="alertdialog"` 的容器
    - `aria-modal="true"` 或带有清晰"弹窗/对话框"语义的区域
- **表格**：
  - 优先考虑：
    - 原生 `<table>`
    - `role="grid"` / `role="table"`
    - 表头单元格 `<th>` 作为列头

### 3.2 组件库特征仅作辅助

- 如果某些元素的 class 包含了组件库前缀 (如 `el-table`、`ant-table` 等), 你可以:
  - 把这些 class 当作**加分项**, 帮助确认元素是"表格容器";
  - 但不能在缺乏其他信息的情况下, **仅凭 class 名**就选择该元素。
- 同理, 对于下拉、弹窗等:
  - 可以利用 class 名中包含如 `dropdown`、`select`、`modal`、`dialog` 的模式来辅助判断,
  - 但仍应结合 tag、role、文本和上下文 (是否在弹窗中、zIndex 等) 一起综合评估。

### 3.3 语义 + 上下文匹配

- 当 intent 明确提到「在弹窗中的某输入框」「表格这一行的操作按钮」「筛选下拉栏中的输入框」时:
  - 你必须优先选择:
    - 处于相应上下文中的元素 (弹窗容器内、表格行内、筛选区容器内),
    - 而不是主页面背景中的同名输入框/按钮。
- 当存在多个 placeholder/文本 相同或相似的组件时:
  - 使用 intent + DOM 上下文 (父级结构、是否在弹窗/表格/筛选栏中、zIndex) 来区分,
  - 不要仅凭 placeholder/Text 完全一致就选第一个。

## 四、常见组件库 DOM 特征速查（内置）

> 本节是针对「常见 UI 组件库」预置的一些 DOM 特征，仅作为**加分线索**使用。
> 你仍必须以通用结构 (tag/role/aria/文本/上下文) 为主，不能只凭这些 class/前缀就做决定。

### 4.1 Element UI / Element Plus

- 下拉 / 选项 (select):
    - **禁止**将 `.el-select__input-calculator` (`aria-hidden` 的测宽用 span) 作为 click 目标；应点击 `.el-select__wrapper` 或内部 `input.el-select__input[role=combobox]`。
    - 触发器/输入区域常见:
        - 外层容器: `.el-select`, `.el-select__wrapper`
        - 可输入/可点击区域: `.el-select__input-wrapper`, `[role="combobox"]`
    - 下拉面板/选项区域:
        - 面板容器: `.el-select-dropdown`, `.el-select-dropdown__wrap`
        - 选项项: `.el-select-dropdown__item`, `li[role="option"]`
    - 你可以在看到这些 class 时，将对应元素视为"更可能是下拉触发器或选项"，但仍需结合 role/text/上下文确认。

- 弹窗 / 对话框 (dialog):
    - 典型结构:
        - 外层容器: `.el-dialog`, `.el-dialog__wrapper`
        - 遮罩层: `.v-modal` 或样式上是全屏半透明 div

### 4.2 Ant Design (AntD)

- 下拉 / 选项:
  - 触发器: `.ant-select`, `.ant-select-selector`
  - 下拉面板: `.ant-select-dropdown`
  - 选项项: `.ant-select-item-option`, `.ant-select-item-option-content`

- 弹窗 / 对话框:
  - 模态框容器: `.ant-modal`, `.ant-modal-content`
  - 遮罩层: `.ant-modal-root .ant-modal-mask`
  - 抽屉: `.ant-drawer`, `.ant-drawer-content`

- 表格:
  - 表格容器: `.ant-table`
  - 表头: `.ant-table-thead`, `.ant-table-cell`
  - 行: `.ant-table-tbody .ant-table-row`

在这些结构中, class 名中的 `ant-select` / `ant-modal` / `ant-table` 可以帮助你识别组件类型, 但依然要结合 tag/role/文本/上下文来做最终决定。

### 4.3 Naive UI

- 下拉 / 选项:
  - 触发器: `.n-select`, `.n-base-selection`, `.n-base-select`
  - 下拉面板: `.n-base-select-menu`, `.n-base-select-menu--virtual`
  - 选项项: `.n-base-select-option`, `.n-base-select-option__content`

- 弹窗 / 对话框:
  - 对话框: `.n-dialog`, `.n-dialog__content`
  - 模态: `.n-modal`, `.n-modal-body`
  - 抽屉: `.n-drawer`, `.n-drawer-body`

- 表格 / 数据表:
  - 容器: `.n-data-table`
  - 表头: `.n-data-table-thead`, `.n-data-table-th`
  - 行: `.n-data-table-tbody .n-data-table-tr`

同样，`.n-select` / `.n-dialog` / `.n-data-table` 等前缀仅应作为辅助信号，而不是唯一依据。

### 4.4 技能脚本调用 (YAML Frontmatter entrypoints)

Skill 是热插拔的外置能力: Agent 不随意 `import` 业务脚本, 仅当本文件 YAML frontmatter 声明了可执行入口 (`entrypoints`) 时, 才由 `core/skill_loader.py` 按声明加载并调用.

内置脚本已登记在 frontmatter `entrypoints` 中 (见文件顶部), 实现位于 `core/locating/skill_dom_helpers.py`.

约定 (扩展外置脚本示例):

```yaml
entrypoints:
  my_custom_helper:
    kind: python
    script: "{baseDir}/scripts/my_custom_helper.py"
    args: ["--input", "{input_json}", "--output", "{output_json}"]
```

- `{baseDir}`: 本文件 `prompts/skill.md` 所在目录
- `{input_json}` / `{output_json}`: Agent 传入/回收的 JSON 文件路径 (输出需写入 `{"result": ...}`)

本仓库内置入口使用 `kind: python` + `module` + `callable` (进程内调用, 无需 JSON 中转):

```yaml
entrypoints:
  build_dropdown_option_selector:
    kind: python
    module: core.locating.skill_dom_helpers
    callable: build_dropdown_option_selector
```

L3 规则引擎与 L5 纠偏通过 `core/locating/skill_invoke.py` → `core/skill_loader.invoke_entrypoint()` 调用已登记入口; 若无对应 entrypoints 或调用失败, 则回退为 `skill_dom_helpers` 内置实现.

### 4.5 使用这些特征时的通用原则

1. 如果通用结构 (tag/role/aria/文本/上下文) 已经足以判断组件类型，则**不需要依赖组件库特征**。
2. 当多个候选元素在语义上都可能匹配时，可以用这些 class/前缀来**提升更合理候选的分数**，例如：
    - 在多个可能是表格的容器中，class 含 el-table / ant-table / n-data-table 的容器得分更高。
3. 若需扩展组件库特征, 可在本文件顶部 YAML frontmatter 的 `framework_detect` / `framework_selectors` 中补充, 由 `core/skill_loader.py` 加载。

## 五、组件库无关的行为规范总结

1. **不要写死某个组件库的结构假设**，例如"下拉选项一定是 Element UI 的 `el-select-dropdown__item`"。
2. 当遇到你不认识的组件结构（新的 class/新的 DOM 组合）时：
    - 先基于 tag/role/aria/text/zIndex 等**通用信号**进行推断；
    - 再将带有明显语义的 class（如带有 dropdown/modal/table）作为辅助提升置信度。
3. 始终把用户意图中的词（「下拉框」「下拉选项」「弹窗」「表格」「筛选栏」）理解为**语义标签**，而不是与某个特定库一一对应的实现。
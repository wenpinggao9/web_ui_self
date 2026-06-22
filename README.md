# ui_automation — AI 驱动的自然语言 UI 测试框架

用中文写 Markdown 测试用例，系统自动打开浏览器、自动登录、自动导航、自动把每一步翻译成浏览器操作、自动检查结果、最后给出报告。测试人员不需要写任何代码或选择器，AI 负责理解意图并动态决策。

---

## 架构总览

```
用户提交 .md 用例文件
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤① 解析器 — 把 Markdown 拆成结构化用例                     │
│  输出: 用例编号 + 步骤列表 + 预期列表 + 模块路径 + 前置条件      │
└──────────────────┬───────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤② 前置条件展开 — 把"已有收货地址"变成"先添加一条地址"      │
│  剔除登录类条件, 其余拆为操作步骤插到最前面                     │
└──────────────────┬───────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤③ 用例排序 — 大模型推断用例间依赖边, 稳定拓扑排序          │
└──────────────────┬───────────────────────────────────────────┘
                   ▼ (对每条用例循环)
┌──────────────────────────────────────────────────────────────┐
│  步骤④ 自动登录 — 打开登录页, 填账号密码, 会话复用               │
│  步骤⑤ 模块导航 — 按模块路径逐级点菜单                          │
│  步骤⑥ 动作规划 — LLM 翻译并拆成原子 {类型, 意图, 值}, 不含选择器  │
│  步骤⑧ 语义 DOM 抽取 — 实时页面快照 (弹窗/表单优先)             │
│  步骤⑨ 元素定位五级链 — 缓存→记忆→规则→学习→LLM                  │
│  步骤⑩ 步骤前就绪检查 — 页面就绪? 恢复动作                      │
│  步骤⑪ 动作分发器 — 选择器 → Playwright 执行                   │
│  步骤⑫ 步骤后校验 — LLM 判断"点对了吗" (防假操作)                │
│  步骤⑬ 带后校验重试 — 改值/改选择器/两者都改                     │
│  步骤⑭ 执行编排器 — 串起所有环节                                │
└──────────────────┬───────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤⑮ 结果汇总 + 报告 + 代码生成 + 持久化                      │
└──────────────────────────────────────────────────────────────┘
```

---

## 项目结构

```
ui_automation/
├── run.py                     # CLI 入口
├── config.yaml                # 配置 (LLM/Playwright/运行开关)
├── Dockerfile                 # 容器化部署
├── requirements.txt
│
├── prompts/                   # 【可编辑】每个 LLM 环节的提示词
│   ├── action_plan.system.md   # 动作规划系统提示词
│   ├── action_plan.user.md     # 动作规划用户提示词
│   ├── precondition.system.md  # 前置条件展开
│   ├── element_decide.*.md     # 元素决策
│   ├── readiness.*.md          # 就绪检查
│   ├── post_check.*.md         # 步骤后校验
│   ├── case_sort.*.md          # 用例排序
│   └── skill.md                # 组件库知识 (技能)
│
├── core/
│   ├── parser/                 # 步骤① 用例解析
│   │   ├── schema.py           #   ParsedCase 数据结构
│   │   ├── markdown_parser.py  #   Markdown 解析
│   │   └── xmind_parser.py     #   XMind 解析 (留桩)
│   ├── preprocess/             # 步骤②③ 预处理
│   │   ├── precondition.py     #   前置条件展开
│   │   └── case_sort.py        #   用例排序
│   ├── session/                # 步骤④⑤ 登录与导航
│   │   ├── login.py            #   自动登录 + 会话复用
│   │   ├── navigator.py        #   模块导航
│   │   ├── menu_scanner.py     #   动态菜单扫描 (留桩)
│   │   └── feature_selectors.py#   静态映射字典
│   ├── planning/               # 动作规划
│   │   ├── action_schema.py    #   PlannedAction (无选择器)
│   │   ├── action_planner.py   #   LLM 动作规划 (含拆分)
│   │   └── intent_splitter.py  #   菜单点击剥离
│   ├── dom/                    # 步骤⑧ 语义 DOM 抽取
│   │   ├── semantic_dom.py     #   索引化摘要 + build_locator_info
│   │   └── v3_bridge.py        #   页面 traverse 采集脚本
│   ├── locating/               # 步骤⑨ 五级定位链
│   │   ├── resolver.py         #   编排: L1→L2→L3→L4→L5
│   │   ├── cache.py            #   L1 选择器缓存 (30min TTL)
│   │   ├── memory.py           #   L2 长期记忆 (加减分)
│   │   ├── structure_learner.py#   L4 结构学习 (跨批次持久化)
│   │   ├── skill_resolver.py   #   L3 规则 skill 分发 + 组件类型推断
│   │   ├── skill_dom_helpers.py#   build_* 选择器 (radio/checkbox/fill/…)
│   │   ├── intent_route.py     #   意图 → 组件类型路由
│   │   ├── intent_window.py    #   L5大模型: 从完整 DOM 抽 intent 相关节点
│   │   ├── llm_decider.py      #   L5 大模型元素决策 + use_skill
│   │   ├── node_refiner.py     #   L5 节点纠偏 (skill 祖先爬升)
│   │   ├── self_heal.py        #   自愈三策略
│   │   └── normalize.py        #   URL/意图归一化 + 选择器校验
│   ├── readiness/              # 步骤⑩ 步骤前就绪检查
│   │   └── pre_check.py        #   弹窗优先 + 必填检测 + 恢复动作
│   ├── execution/              # 步骤⑪⑫⑬⑭ 执行层
│   │   ├── dispatcher.py       #   动作分发器 (五级链 + Playwright)
│   │   ├── post_check.py       #   步骤后校验 (防假操作)
│   │   ├── retry.py            #   带后校验重试 (值/选择器/两者/无)
│   │   ├── retry_hint.py       #   后校验 hint → force_selector
│   │   ├── page_session.py     #   Tab 跟随 + 断言 DOM 复用
│   │   └── runner.py           #   执行编排器 PlaywrightRunner
│   ├── llm/                    # 步骤⑱ LLM 基础层
│   │   ├── adapter.py          #   LLM 适配器 (重试 + JSON提取)
│   │   └── prompt_loader.py    #   提示词加载器 (文件→配置覆盖)
│   ├── agent.py                # 步骤⑮ 核心编排器 UITestAgent
│   ├── observability.py        # 步骤⑲ 可观测性收集器
│   ├── resources.py            # 步骤⑳ 资源管理 (上传/本地/资产)
│   ├── codegen.py              # 步骤㉑ Playwright 代码生成 (.spec.ts)
│   ├── skill_loader.py         # 步骤㉒ 组件库知识加载
│   ├── output.py               # 步骤㉓ 文件与输出管理
│   ├── logger.py               #   结构化日志 (复用)
│   └── report.py               #   HTML 报告生成 (复用)
│
├── api/                        # 步骤⑯⑰ 服务化
│   ├── server.py               #   FastAPI REST 服务 (9 接口)
│   ├── task_manager.py         #   任务管理器 (状态机)
│   ├── server_runner.py        #   服务器模式运行器
│   ├── remote_agent_runner.py  #   远程代理运行器 + 注册表
│   └── local_agent.py          #   Flask 本地界面代理 (有头浏览器)
│
├── 业务/                       # 业务用例与知识 (按项目组织)
│   └── vip视频/
│       ├── 业务知识.md
│       └── 大学增加前审/
│           ├── 项目配置.yaml
│           └── cases/
│               ├── 测试用例.md
│               └── 前审1.md …
│
└── 智能加速/                   # 运行时自动生成
    ├── 选择器缓存.json         # L1 持久化 (进程内为主)
    └── 选择器记忆库.json       # L2 长期记忆
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install --with-deps chromium
```

### 2. 配置

编辑 `config.yaml`:

```yaml
llm:
  provider: opus  # ollama | minimax | opus
  opus:
    base_url: https://openproxy.zuoyebang.cc/openproxy/rp/v1
    api_key: "你的 API Key"
    model: claude-opus-4-6

target:
  base_url: http://localhost:4173/#/login
  username: "测试账号"
  password: "测试密码"
  login:
    username_placeholder: "请输入手机号"
    password_placeholder: "请输入密码"

playwright:
  browser: chromium
  headless: false  # true 为无头模式

runner:
  pre_readiness_check: true   # 步骤前就绪检查
  post_step_check: true       # 步骤后校验 (防假操作)
  post_step_max_retries: 5    # 重试上限

# 步骤⑨ L3 大模型 DOM 窗口 (L3规则 仍扫完整 DOM)
locating:
  dom_limit: 80               # 喂给 element_decide LLM 的候选上限
  intent_window: true         # 从完整 DOM 按 intent 抽相关节点; false 则取前 N 条
```

### 3. 运行

```bash
# 单个用例
python run.py 业务/vip视频/大学增加前审/cases/测试用例.md

# 整个用例目录
python run.py 业务/vip视频/大学增加前审/cases/
```

### 4. 查看输出

每次运行在 `输出/UI测试/<时间戳>/` 下生成:

```
<用例编号>/
├── 报告/index.html          # HTML 测试报告
├── 截图/                    # 失败步骤截图
├── 语义DOM/                 # 每步 DOM 快照
├── 执行日志.json            # 结构化执行结果
├── 已解析用例.json          # 解析后的用例结构
├── 已规划动作.json          # LLM 规划的动作列表
├── 使用的模型提示词.md      # 实际使用的提示词
├── 模型原始响应.txt         # LLM 原始返回
└── 可观测性.json            # 可观测性追踪 (含 LLM 调用链)

playwright_<用例编号>.spec.ts  # 生成的 TypeScript 测试脚本
```

---

## 编写测试用例

### Markdown 格式

```markdown
# 一级模块名

## 二级模块名

### 三级模块名

#### 用例ID：电商_地址_添加_001

##### 优先级
P0

##### 前置条件
- 用户已登录
- 已有收货地址

##### 操作步骤
- 鼠标悬浮在页面右上角的用户菜单按钮 "15373137739" 上
- 点击菜单中的 "收货地址" 选项
- 点击 "添加新地址" 按钮
- 在 "收件人" 输入框输入 "张三"
- 在 "联系电话" 输入框输入 "15373137739"
- 点击省份下拉框
- 在弹出选项中点击 "广东省"

##### 预期结果
- 地址保存成功
```

### 格式规则

| 标题层级 | 提取内容 |
|---------|---------|
| `#` | 一级模块名 → 模块路径[0] |
| `##` | 二级模块名 → 模块路径[1] |
| `###` | 三级模块名 → 模块路径[2] |
| `#### 用例ID：XXX` | 用例编号 |
| `##### 前置条件` | 前置条件列表 |
| `##### 操作步骤` | 操作步骤列表 |
| `##### 预期结果` | 预期结果列表 |
| `##### 资源定义` | 上传文件等资源 (```yaml 块) |

列表项支持 `1. xxx` 和 `- xxx` 两种写法。

---

## 自定义提示词

每个 LLM 环节的系统/用户提示词均可修改，优先级:

1. `config.yaml` 的 `llm.prompts.{阶段名}.system/user` (最高)
2. `prompts/{阶段名}.system.md` / `prompts/{阶段名}.user.md` 文件
3. 代码内置默认 (兜底)

支持的环节:

| 环节 | 文件 | 占位符 |
|------|------|--------|
| 动作规划 | `action_plan.*.md` | `{{module_path}}`, `{{steps}}`, `{{expectations}}` |
| 元素决策 | `element_decide.*.md` | `{{action_type}}`, `{{intent}}`, `{{dom}}` |
| 前置展开 | `precondition.*.md` | `{{preconditions}}` |
| 用例排序 | `case_sort.*.md` | `{{cases}}` |
| 就绪检查 | `readiness.*.md` | `{{url}}`, `{{dom}}`, `{{action_type}}`, `{{intent}}` |
| 后校验 | `post_check.*.md` | `{{action_type}}`, `{{intent}}`, `{{dom}}`, `{{ok}}` |
| 重试 | `retry.*.md` | `{{action_type}}`, `{{intent}}` |

---

## REST API

### 启动服务

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
# Swagger: http://localhost:8000/docs
```

### 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/ui-test/run` | 上传用例 + 模式 → 任务编号 |
| GET | `/api/v1/ui-test/status/{id}` | 查状态 + 进度 |
| GET | `/api/v1/ui-test/result/{id}` | 获取结果 |
| GET | `/api/v1/ui-test/report/{id}` | 下载 ZIP 报告 |
| GET | `/api/v1/ui-test/tasks` | 任务列表 |
| POST | `/api/v1/ui-test/task/{id}/complete` | 代理完成回调 |
| POST | `/api/v1/agent/heartbeat` | 代理心跳注册 |
| GET | `/api/v1/agent/list` | 已注册代理列表 |

### 本地界面代理 (有头浏览器调试)

```bash
# 启动本地代理
AGENT_PORT=8100 SERVER_URL=http://127.0.0.1:8000 python -m api.local_agent
# 代理自动每 30 秒向服务器发心跳
```

---

## Docker 部署

```bash
docker build -t ui-automation .
docker run -p 8000:8000 ui-automation
```

---

## 核心设计特点

### 1. 两段式测试用例处理
- **动作规划**: LLM 只产出 `{类型, 意图, 值}`, **不含选择器**
- **步骤拆分**: 复合动作拆成原子操作; 仅当用例明确写"等待"时才生成 `wait`

### 2. 元素和意图纠偏
- 执行前实时抓页面 DOM 摘要
- 页面就绪检查 (弹窗/表单 DOM 优先排序)
- LLM 判断页面是否准备好, 未就绪则给出恢复动作

### 3. 执行防假操作
- 每步执行后 LLM 校验"真的点对了吗"
- 失败时智能重试: 改值 / 改选择器 / 两者都改
- 排除已试过的选择器, 避免死循环

### 4. 提示词完全可编辑
- 每个 LLM 环节的系统/用户提示词均为独立 `.md` 文件
- 支持 `config.yaml` 覆盖
- 每次运行输出实际使用的提示词和 LLM 原始响应

### 5. 五级定位链 (智能加速)

定位顺序:**L1 缓存 → L2 记忆 → L3 规则 → L4 学习 → L5 大模型**. L1-L4 未命中或校验失败时进入下一级; L5 成功后回填 L1+L2+L4, 同页面二次运行显著减少 LLM 调用.

```
L1 缓存 ──→ L2 记忆 ──→ L3 规则 ──→ L4 学习 ──→ L5 大模型
                                                    ├─ L3规则   build_* skill, 扫完整 semantic_items (radio/checkbox/fill/下拉/日期…)
                                                    ├─ L4学习   相似意图 Jaccard 匹配, 跨批次持久化
                                                    ├─ L5大模型 element_decide LLM (意图窗口 / dom_limit 限候选)
                                                    ├─ L5 Skill  use_skill 节点纠偏 (choose_best_input_target 等)
                                                    └─ L5纠偏    node_refiner 祖先爬升
```

| 级别 | 来源 | 说明 |
|------|------|------|
| L1 缓存 | `智能加速/选择器缓存.json` | 同 URL+意图+动作类型, 进程内最快 |
| L2 记忆 | `智能加速/选择器记忆库.json` | 跨运行加减分, 长期复用 |
| L3 规则 | `skill_dom_helpers.build_*` | 无 LLM, 从语义 DOM 推断组件并生成选择器 |
| L4 学习 | `智能加速/页面结构学习.json` | 相似意图 Jaccard 匹配, 跨批次持久化 |
| L5 大模型 | `element_decide` | L4未命中时调用; `intent_window=true` 时从全量 DOM 抽相关节点 |
| L5 Skill | `use_skill` 协议 | LLM 指定 skill 二次精确定位 |
| L5 纠偏 | `node_refiner` | 初匹配 index 上爬升/精炼 |

**DOM 采集**: V3 traverse 抓取完整可交互元素 (弹窗/表单优先排序). **L3规则与后校验/断言用全量 items**; 仅 **L5大模型** 受 `dom_limit` / 意图窗口限制.

**重试 hint**: 后校验若给出裸 CSS (如 `input#searchText`), `retry_hint` 会转为 `force_selector` 跳过后续 LLM 定位.

**断言 DOM**: 同 URL 连续断言复用上一步操作后的 semantic_items; 提交类操作后断言走实时 DOM, 不用固化快照.

框架 **不在 `core/` 硬编码业务字段**; 业务枚举、API、角色写在各项目 `业务知识.md` / `项目配置.yaml`.

---

## 测试统计

| 用例 | 步骤 | 通过 | 说明 |
|------|------|------|------|
| login_success.md | 3/3 | ✅ 全绿 | 登录 + 用户菜单悬停 + 断言 |
| add_address.md | 14/14 | ✅ 全绿 | 含省/市/区级联下拉 |
| shop_scenario.md | 22/33 | ⚠️ 部分 | 购物流程 21-33 全绿; 前置展开需优化 |

---

## LLM Provider 支持

| Provider | 配置方式 | 备注 |
|----------|---------|------|
| Ollama | `provider: ollama` | 本地模型, 无需 API Key |
| MiniMax | `provider: minimax` | 国内代理网关 |
| Opus/Claude | `provider: opus` | OpenAI 兼容接口 |

---

## 许可证

内部项目。

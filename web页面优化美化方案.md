# Web 页面优化美化方案

> **目标系统**：LLMperfomance Web 面板
> **技术栈**：FastAPI + Jinja2 + SQLite + 原生 HTML/CSS/JS（无前端框架）+ ECharts（CDN，当前仅用于生成的 HTML 报告）
> **日期**：2026-09-11
> **定位**：在不改动后端 API 的前提下，通过模板 / CSS / 少量前端 JS 对面板做视觉与交互升级；后端仅需极少量文案改动（如 `server.py` 的 `title`）。

---

## 1. 现状诊断

### 1.1 页面清单与职责

| 模板文件 | 路由 | 职责 | 当前视觉 |
|---|---|---|---|
| `base.html` | 全局 | 布局、导航、Token 条、删除逻辑 JS | 960px 居中白底，CSS 全内联 |
| `index.html` | `/` | Dashboard：最近任务 | 单张表格 |
| `tasks.html` | `/tasks` | 全部任务列表 | 表格 |
| `new_task.html` | `/tasks/new` | 新建任务（按脚本动态渲染表单） | 垂直堆叠表单 |
| `task_detail.html` | `/tasks/{id}` | 详情：状态 / 参数 / 实时进度 / 报告 / 诊断 | 多卡片堆叠 |
| `compare.html` | `/compare` | 多任务指标对比 | 纯表格 |
| `configs.html` | `/configs` | 配置档管理 | 表单 + 表格 |
| `corpora.html` | `/corpora` | 语料上传 / 列表 | 表单 + 表格 |

### 1.2 主要问题清单

1. **视觉朴素**：固定 960px、白底灰字、无设计系统，观感像"后端工具默认页"。
2. **导航简陋**：纯文字链接，无品牌标识、无当前页高亮、无 favicon。
3. **Dashboard 信息密度低**：只有"最近任务"一张表，缺全局指标（总任务、运行中、成功率、平均 TTFB / RPS）。
4. **缺数据可视化**：ECharts 只在生成的 HTML 报告里用，面板本身（Dashboard / Compare / Task Detail）几乎无图表；Compare 页全靠表格，肉眼对比困难。
5. **表单体验一般**：`<p><label>…` 垂直堆叠，无分组、无必填标记、无占位说明层次。
6. **中英混杂**：导航英文（Dashboard/Tasks/New Task/Configs/Corpora），正文中文（语料管理/删除/提交参数），术语不统一（Status/Run/Type 等）。
7. **状态标识单一**：徽标只有颜色，无图标、无动效（运行中无呼吸/旋转提示）。
8. **无响应式**：固定宽度，窄屏 / 平板体验差（内部工具，优先级可后置）。
9. **样式分散**：CSS 全内联在 `base.html`，各页还有大量内联 `style`，难维护、难统一换肤。

---

## 2. 页面标题命名建议（重点）

### 2.1 命名原则

- **准确**：核心动作是「性能压测」（负载 / 吞吐 / 延迟），不是泛泛的"评测"。
- **简洁**：导航 / 标题栏要短，过长会被截断或显得啰嗦。
- **可扩展**：未来可能扩展评测基准、对比报告，名字别写死。
- **技术感与通用性平衡**：对外演示用中文友好，对内保留 LLM 技术标识。

### 2.2 候选方案对比

| 方案 | 标题 | 优点 | 缺点 |
|---|---|---|---|
| **A ⭐** | **LLM 性能压测平台** | 准确、简洁、技术品牌感强 | 需懂 LLM 缩写 |
| B | 大模型性能压测平台 | 全中文、对非技术用户友好 | "大模型"范围比 LLM 略宽 |
| C | AI 大模型性能评测平台 | 覆盖"评测"语义 | "评测"弱化压测强度；"AI + 大模型"略冗余 |
| D | LLM Perf · 性能压测面板 | 保留原品牌 LLM Perf | 中英混排稍长 |
| E | AI 应用 / 大模型性能压测评测系统 | 用户原文 | 过长；"压测/测评"语义重叠；"系统"偏正式 |

### 2.3 推荐方案

**主标题（页面 header / 浏览器 title）**：

> **LLM 性能压测平台**

**副标题（header 下方或空态区标语，可选）**：

> 面向 Dify / OpenAI 兼容接口的性能压测与评测

**浏览器 `<title>` 模板**（各页「页面名 · 站名」形式）：

```html
<title>{% block title %}LLM 性能压测平台{% endblock %}</title>
<!-- 例：Dashboard · LLM 性能压测平台 -->
```

**英文角标（Logo 旁，保留原品牌）**：`LLM Perf Panel`

### 2.4 若坚持全中文 / 偏正式

可选 **「大模型性能压测平台」**，副标题写「LLM Benchmark & Load Testing」。不建议用"压测评测"连写（语义重叠）；"系统"若用于对上级汇报场景也可接受，但日常面板建议"平台"或"面板"。

---

## 3. 整体视觉升级（设计系统）

### 3.1 设计原则

- **轻量优先**：继续用原生 CSS + JS，**不引入** React/Vue/Tailwind 等重型依赖，避免破坏现有 SSE、Token、两段式删除等逻辑。
- **一次定义、处处复用**：用 CSS 变量（Design Token）统一颜色、间距、圆角、阴影。
- **信息优先**：压测面板是"看数据"的工具，视觉要为数据可读性服务，克制装饰。

### 3.2 配色方案（CSS 变量）

建议抽离为 `web/static/css/base.css`，`base.html` 以 `<link>` 引入（并保留一个精简内联兜底，防止 CDN/静态挂载异常时页面裸奔）。

```css
:root {
  /* 品牌主色（沿用现有 Google 蓝，降低饱和更现代） */
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --primary-soft: #eef4ff;

  /* 语义色 */
  --success: #16a34a;
  --warning: #f59e0b;
  --danger:  #dc2626;
  --info:    #7c3aed;

  /* 中性色 */
  --bg:          #f6f7f9;
  --surface:     #ffffff;
  --border:      #e5e7eb;
  --text:        #1f2937;
  --text-muted:  #6b7280;

  /* 状态徽标（与 status-* 对应） */
  --status-pending:   #9ca3af;
  --status-running:   #2563eb;
  --status-completed: #16a34a;
  --status-failed:    #dc2626;
  --status-cancelled: #f59e0b;
  --status-orphaned:  #7c3aed;

  /* 排版与形状 */
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", Roboto, sans-serif;
  --radius: 10px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,.05);
  --shadow:    0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.04);
}
```

### 3.3 字体与排版

- 全局字体栈加 `"Microsoft YaHei"`（Windows 中文），保证中文标题、表格不乱码、更锐利。
- 标题层级：`h1` 页面主标题（22px）、`h2` 卡片标题（16px）、正文 14px、辅助 12px。
- 统一行高 1.5，数字（指标值）用等宽数字 `font-variant-numeric: tabular-nums` 对齐。

### 3.4 布局容器与栅格

- 容器从固定 `960px` 改为 `max-width: 1200px; margin: 0 auto; padding: 0 24px;`，更充分利用宽屏。
- 引入轻量栅格（`.grid` + `.col-*` 或直接 `display:grid; grid-template-columns: repeat(auto-fit, minmax(240px,1fr))`）用于统计卡片与表单两列排版，**无需引入框架**。

### 3.5 全局组件规范

| 组件 | 现状 | 优化 |
|---|---|---|
| 按钮 `.btn` | 蓝底白字圆角 4px | 统一高度 36px、圆角 8px、加 hover/active 过渡、危险/次要不透明变体 |
| 卡片 `.card` | 白底阴影 | 加 `border:1px solid var(--border)`，标题与正文留白更匀称 |
| 表格 | 基础边框 | 斑马纹、表头灰底加粗、单元格垂直居中、hover 高亮行 |
| 徽标 `.status-badge` | 纯色圆角 | 加图标（✓/⏳/✕/⏸）+ 柔和底色（浅底深字），运行中加脉冲动画 |
| 表单 | 堆叠 | 分组卡片 + 两列网格 + 必填 `*` + `label` 加粗 + 输入框统一高度/焦点描边 |

---

## 4. 布局与导航升级

### 4.1 顶栏（Header）

把 `base.html` 里的 `<nav>` 升级为固定顶栏：

```html
<header class="topbar">
  <div class="brand">
    <span class="logo">⚡</span>
    <div>
      <div class="brand-name">LLM 性能压测平台</div>
      <div class="brand-sub">LLM Perf Panel · Dify / OpenAI 兼容接口压测</div>
    </div>
  </div>
  <nav class="nav">
    <a href="/">仪表盘</a>
    <a href="/tasks">任务</a>
    <a href="/tasks/new" class="nav-cta">＋ 新建任务</a>
    <a href="/compare">对比</a>
    <a href="/configs">配置档</a>
    <a href="/corpora">语料</a>
  </nav>
</header>
```

要点：
- 「＋ 新建任务」做成主行动按钮（`.nav-cta`），突出核心操作。
- 新增「对比」入口到导航（当前 compare 页无入口，只能拼 URL）。
- 顶栏白底 + 底部细边框 + 轻微阴影，内容区随滚动。

### 4.2 导航高亮与面包屑

- 后端在每个 `TemplateResponse` 注入 `active`（如 `active="tasks"`），模板据此给当前项加 `.active` 高亮（主色下划线/底色）。
- 详情页、对比页加面包屑：`任务 / Run 20260903xxxx`，方便回退。

### 4.3 favicon 与品牌标识

- 新增 `web/static/favicon.svg`（一个 ⚡ 或测速仪表盘图标），`base.html` 加 `<link rel="icon" href="/static/favicon.svg">`。
- 浏览器书签 / 标签页不再显示默认灰地球。

---

## 5. 逐页优化方案

### 5.1 Dashboard（首页 `/`）

现状：一张「Recent Tasks」表格。优化为「全局指标 + 图表 + 最近任务」三段：

1. **顶部统计卡片（4~6 个）**：总任务数、运行中、成功率、平均 TTFB、平均 RPS、失败任务数——数据来源现成（`db.list_test_runs` 已有字段），后端可新增一个 `/api/stats` 汇总接口（或前端聚合现有 `/api/tasks`）。
2. **图表区**：近 N 次任务的 RPS / TTFB 趋势（折线）、状态分布（环形/饼图）。
3. **最近任务表格**：保留，补状态图标、缩短时间显示（`2026-09-11 10:30`）。
4. **空态**：无任务时给引导卡片（"还没有任务，点击这里创建第一个压测任务" + 大按钮），替代现在的纯文字。

### 5.2 Tasks（任务列表 `/tasks`）

1. 顶部工具栏：筛选（按状态/脚本/协议）、关键字搜索、刷新按钮。
2. 表格列补充：成功率、TTFB p95、RPS（从 summary 提取），而不仅是 ID/Type/Status/时间。
3. **行内勾选 + 「对比选中」按钮**，直接跳 `/compare?ids=…`（打通"列表 → 对比"的路径）。
4. 分页（limit=50 改为分页，前端或后端均可）。
5. 状态徽标统一加图标。

### 5.3 New Task（新建任务 `/tasks/new`）

1. 改为**两步式/分区式**表单：
   - 第 1 步：选脚本（卡片式脚本选择，展示图标 + 协议标签 + 一句话说明，替代裸下拉）。
   - 第 2 步：参数区（按脚本 schema 渲染，两列网格 + 分组 + 必填 `*` + hint 气泡）。
   - 第 3 步：配置档（协议过滤后的下拉，注明"可留空走 .env"）。
2. 「Submit Task」改为主按钮，提交中加 loading 态（禁用 + 转圈），成功跳详情。
3. 顶部展示已推导的 `test_type` 与协议徽标，让用户确认。

### 5.4 Task Detail（任务详情 `/tasks/{id}`）

1. **头部信息卡**：大号状态徽标 + 关键指标速览（TTFB avg/p95、RPS、成功率、耗时），running 时实时刷新。
2. **实时进度**：现有 `#progress-log` 黑底日志保留（技术感），可加「自动滚动到底部」开关与「复制日志」按钮。
3. **HTML 报告**：iframe 加边框/圆角，加"新窗口打开"按钮（全屏查看）。
4. **诊断区**：规则引擎 findings 表格加严重度颜色（critical 红 / warning 黄 / info 蓝），LLM 叙述用卡片美化。

### 5.5 Compare（对比 `/compare`）

1. **图表先行**：用 ECharts 柱状图并列对比「RPS / TTFB p95 / Latency p95 / Success Rate」，替代（或叠加在）现有大表格之上。
2. 选中态 UI：选择任务改为可搜索的 checkbox 列表（任务多时好找）。
3. 表格保留但做**最佳值高亮**（每行最优单元格加底色/加粗），一眼看出谁快谁稳。
4. 失败分类做堆叠条形图。

### 5.6 Configs（配置档 `/configs`）

1. 配置档改为**卡片网格**（Name + 协议徽标 + Base URL + Model + 删除按钮），替代表格。
2. 新增表单分两列（Name / Protocol / Base URL / Model / API Key），API Key 输入框加"显示/隐藏"切换。
3. 加"测试连接"按钮（可选，后端加一个 ping 接口）。

### 5.7 Corpora（语料 `/corpora`）

1. 上传区改成**拖拽上传**（拖入 `.txt` 高亮），保留点击选择。
2. 语料列表加图标区分「内置/上传」，上传项显示行数、大小、可删除；只读项置灰。
3. 上传成功 toast 提示（替代纯文字状态）。

---

## 6. 数据可视化方案（ECharts）

统一引入 `echarts`（本地 `web/static/echarts.min.js` 优先，避免内网无外网时 CDN 失效；有外网可回退 CDN）。封装一个 `window.renderChart(el, option)` 助手。

| 页面 | 图表 | 说明 |
|---|---|---|
| Dashboard | 状态分布环形图 | 一眼看 pending/running/completed/failed 占比 |
| Dashboard | RPS & TTFB 趋势折线 | 最近任务性能走向 |
| Compare | 指标并列柱状图 | RPS / TTFB p95 / Latency p95 / Success Rate 分组柱 |
| Compare | 失败分类堆叠条 | 各任务失败原因分布 |
| Task Detail | TTFB 分布直方图 | 若 summary 含分位数/直方数据 |

> 说明：`scripts/llmperf_common/html_report.py` 已用 ECharts 生成单任务 HTML 报告，面板侧可复用同一份 ECharts 与数据口径，保证"报告"与"面板"视觉一致。

---

## 7. 响应式与可访问性

- **响应式**：容器 `max-width` + 栅格 `auto-fit` 已天然适配；顶栏在窄屏折叠为汉堡菜单（可选）。内部工具为主，响应式放 P2。
- **对比度**：正文与背景对比度 ≥ 4.5:1；状态徽标采用"浅底深字"避免纯色底 + 白字在小字号下难读。
- **键盘可达**：按钮/链接保留原生语义元素（`<button>`/`<a>`），不滥用 `div onclick`。
- **减动效**：`prefers-reduced-motion` 下关闭脉冲/呼吸动画。

---

## 8. 中英文文案统一

| 位置 | 现文案 | 建议 |
|---|---|---|
| 导航 Dashboard | Dashboard | 仪表盘 |
| 导航 Tasks | Tasks | 任务 |
| 导航 New Task | New Task | 新建任务 |
| 导航 Configs | Configs | 配置档 |
| 导航 Corpora | Corpora | 语料 |
| 状态 pending/running/completed/failed/cancelled/orphaned | 英文 | 待执行 / 运行中 / 已完成 / 失败 / 已取消 / 已孤立（徽标可"中文+英文小字"并排） |
| 表头 Type / Script / Status / Started | 英文 | 类型 / 脚本 / 状态 / 开始时间 |
| 按钮 Submit / Save / Cancel / Delete | 英文 | 提交 / 保存 / 取消 / 删除 |

> 建议状态徽标统一为「中文名」，鼠标悬浮 title 显示英文原值（对接 API 无需改动）；脚本名、协议名、指标名（TTFB/RPS/Latency）保留英文（技术通用术语）。

---

## 9. 实施路线（分阶段）

### P0 —— 投入小、见效快（1 天内）

1. 改标题：`server.py` 的 `FastAPI(title=...)` 与 `base.html` 的 `<title>` → 「LLM 性能压测平台」。
2. 抽离 `base.css` 到 `web/static/css/base.css`，应用 §3 配色 / 字体 / 卡片 / 按钮 / 徽标 / 表格。
3. 顶栏升级（§4.1）：品牌 + 中文导航 + 「＋ 新建任务」CTA + 导航高亮。
4. favicon（§4.3）。

### P1 —— 核心体验（2~3 天）

5. Dashboard 统计卡片 + 空态引导（§5.1）。
6. Compare 图表 + 导航补「对比」入口（§5.5、§6）。
7. 表单两列网格 + 分区 + 必填标记（§5.3）。
8. 状态徽标加图标 + 中文（§8）。
9. Tasks 工具栏筛选 + 勾选对比（§5.2）。

### P2 —— 进阶（可选）

10. ECharts 本地化 + Dashboard/Detail 图表（§6）。
11. 响应式 / 汉堡菜单 / 暗色主题切换。
12. Configs「测试连接」、Corpora 拖拽上传、toast 通知。

---

## 10. 技术实现要点与约束

1. **尽量纯模板/CSS/JS 改动**：现有 SSE、Token 注入、两段式删除、协议过滤、语料 409 强制删除等前端逻辑**必须保留**，只改外观与新增展示。
2. **静态资源**：新建 `web/static/css/base.css`、`web/static/js/app.js`、`web/static/favicon.svg`；`server.py` 已具备条件挂载 `/static`（`static_dir.exists()`），目录建好即自动生效，**无需改挂载逻辑**。
3. **ECharts 引入**：优先把 `echarts.min.js` 放 `web/static/js/`，模板 `<script src="/static/js/echarts.min.js">`；无外网环境也能出图。
4. **新增 `/api/stats`（可选）**：若 Dashboard 需要全局聚合指标，可在 `server.py` 加一个只读接口，前端 fetch；不改动既有接口签名。
5. **回滚安全**：所有改动集中在 `templates/` 与新增 `static/`，`db.py`/`executor.py`/`script_registry.py` 等核心逻辑零改动，可随时 `git revert`。
6. **兼容**：继续支持 Python 3.10+，不新增前端构建步骤（无 npm/webpack）。

---

## 11. 风险与注意事项

| 风险 | 说明 | 缓解 |
|---|---|---|
| 破坏现有交互 | SSE 重连、两段式删除、token 拦截依赖 DOM 结构 | 重构时保留原 `id`/`class`/`data-*` 选择器；改完逐页回归测试 |
| CDN 失效 | 若用外网 ECharts CDN，内网环境图表空白 | ECharts 本地化到 `/static/js/` |
| 文案改动牵连 | 状态/表头改中文后，若有前端按英文匹配会失效 | 核对无按文案匹配的逻辑；`status-*` class 名不变 |
| 后端 title 改动 | `FastAPI(title=...)` 影响 `/docs` 标题 | 同时更新，保持一致 |

---

## 附：改动清单速览

| 文件 | 动作 |
|---|---|
| `web/server.py` | `FastAPI(title="LLM 性能压测平台")`；路由注入 `active`；（可选）新增 `/api/stats` |
| `web/templates/base.html` | 标题、顶栏、`<link>` 引入 base.css、favicon、导航高亮 |
| `web/templates/index.html` | 统计卡片 + 空态 + 图表 |
| `web/templates/tasks.html` | 工具栏 + 勾选对比 + 补充指标列 |
| `web/templates/new_task.html` | 分区表单 + 网格 + 必填 |
| `web/templates/task_detail.html` | 头部指标卡 + 诊断美化 + 报告新窗 |
| `web/templates/compare.html` | ECharts 柱状图 + 最优高亮 |
| `web/templates/configs.html` | 卡片网格 + API Key 显隐 |
| `web/templates/corpora.html` | 拖拽上传 + toast |
| `web/static/css/base.css`（新增） | 设计系统样式 |
| `web/static/js/app.js`（新增） | 图表助手 / toast / 通用交互 |
| `web/static/js/echarts.min.js`（新增） | ECharts 本地库 |
| `web/static/favicon.svg`（新增） | 站点图标 |


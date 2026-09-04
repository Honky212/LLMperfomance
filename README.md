# LLMperfomance —— LLM 对话 API 性能测试工具集

针对 LLM 流式（SSE）对话接口的性能测试项目，主要面向 Dify 风格的
`POST /v1/chat-messages` 接口，覆盖三种测试形态：

1. **单线程多轮对话 + TTFB（首包时间）测量**
2. **多线程并发对话 + 完整问答记录（CSV）**
3. **Locust 多用户多轮对话压测**

RPS（Requests Per Second）‌：每秒请求数，统计服务端每秒成功处理的独立HTTP/接口请求数量，是最基础的吞吐观测单元，适合直接用于限流管控和容量规划。
‌QPS（Queries Per Second）‌：每秒查询数，最早用于衡量数据库每秒执行SELECT查询的能力，现在也延伸用于单接口服务的每秒请求处理能力统计。
‌TPS（Transactions Per Second）‌：每秒事务数，统计每秒完成的完整业务事务数量，一个事务可能包含多个请求，需满足业务完整性要求，比如一次完整的下单操作。

    基础通用换算公式‌

    通用基础公式：RPS = 并发数 / 平均响应时间，该公式在无网络瓶颈、系统资源充足的场景下完全成立。
    单接口场景：当一个业务事务仅对应1个HTTP请求时，‌RPS = QPS = TPS‌，三者数值完全等价。
    多请求事务场景：若1个业务事务包含N个独立请求，则换算关系为 TPS = RPS / N，比如一次页面加载发起3个请求，1000 RPS对应的TPS仅为333。

    ‌结合业务流量的延伸换算‌

    峰值RPS换算：基于日PV计算峰值流量，公式为 峰值RPS = (日总PV × 80%) / (全天秒数 × 20%)，符合“80%流量集中在20%峰值时段”的通用流量规律。
    服务节点数量换算：所需节点数 = 峰值RPS / 单节点最大承载RPS，可直接用于集群容量规划。


> 本仓库于 2026-08 做过一次去重清理，并于 2026-09 重建为单提交干净历史：
> 早期草稿、CERAI 归档数据与含密钥的旧提交已移除，不再保留在 git 历史中。

## 目录结构

```
LLMperfomance/
├── README.md
├── requirements.txt
├── corpus/                  # 测试语料（输入问题集，去重后统一存放）
│   ├── short10words.txt ~ long3000words.txt   # 10~3000 字分档长文本语料
│   ├── test1.txt            # OpenAI 兼容 Locust 脚本默认语料（逐行问题集）
│   ├── test2.txt            # OpenAI 兼容多线程脚本默认语料（逐行问题集）
│   └── 50QA-1.txt / 50QA-2.txt                # QA 问题集（单线程/多线程脚本默认语料）
├── scripts/                 # 核心测试脚本（见下方说明）
│   ├── llmperf_common/      # 公共模块（config / sse / stats，仅标准库，零第三方依赖）
│   ├── tests/               # 离线单测（unittest：test_sse.py / test_stats.py）
│   └── *.py                 # 6 个测试脚本（Dify 风格 + OpenAI 兼容孪生）
└── results/                 # 测试输出（各脚本运行产出）
    ├── summary_*.json       # 多线程脚本的机器可读汇总（schema v1，见"测试输出说明"）
    └── locust/              # Locust 脚本的逐用户 CSV 与 --html/--csv 报告
```

## 环境准备

```powershell
pip install -r requirements.txt

# 复制配置模板并填写实际值（.env 已被 git 忽略，不会提交）
Copy-Item .env.example .env
# 用编辑器打开 .env，填写 API_KEY / OPENAI_API_KEY 等
```

配置统一从项目根目录 `.env` 文件读取（也兼容直接设置系统环境变量，
且系统环境变量优先于 `.env` 中的值）：

| 变量 | 说明 | 使用方 |
|---|---|---|
| `LLM_API_URL` | Dify 应用基础地址（脚本自动拼接 `/v1/chat-messages`） | scripts/ 原有 3 个脚本 |
| `API_KEY` | Dify 应用 API Key | scripts/ 原有 3 个脚本 |
| `OPENAI_BASE_URL` | OpenAI 兼容接口基础 URL（SDK 自动拼接 `/chat/completions`） | openai_compat_dialog_ttfb.py、openai_compat_multi_thread_record.py、openai_compat_locust_multi_dialog.py |
| `OPENAI_API_KEY` | OpenAI 兼容接口 Key：登录后于「令牌」页面创建 | 所有 OpenAI 兼容接口脚本 |
| `MODEL_NAME` | 模型 ID：渠道中配置的模型名 | 所有 OpenAI 兼容接口脚本 |
| `OPENAI_MAX_TOKENS` | 最大输出 token 数（0 = 不限制） | openai_compat_dialog_ttfb.py、openai_compat_locust_multi_dialog.py |
| `VERIFY_SSL` | 是否校验 TLS 证书；设置为 `false` 时仅在本地自签名环境下临时关闭校验 | 所有脚本 |

## Web 面板（web/，可选）

基于 FastAPI + SQLite 的单进程管理面板（Jinja2 页面 + REST API + SSE 实时进度），
为 6+1 个脚本提供图形化任务提交、语料上传、结果查看与诊断：

```powershell
pip install -r requirements-web.txt   # fastapi / uvicorn / jinja2 等（面板依赖）
python web/server.py                  # 默认 http://127.0.0.1:8686
```

- 配置档（Configs）保存于 `web/data/llmperf.db`（SQLite）：**API Key 为本地明文存储**，
  仅建议本机/内网使用；`web/data/` 已加入 `.gitignore`，禁止提交；
- 脚本注册表（`web/script_registry.py`）是单一事实来源：驱动前端参数表单渲染，
  并集中完成参数 → 命令行/环境变量翻译，6+1 个脚本均在列；
- 执行器为单队列串行：同一时间仅允许一个压测任务，存在 running/pending 任务时
  新提交返回 409（避免并发压测互相污染指标）。

**安全提示**：默认仅绑定 `127.0.0.1` 且写接口无鉴权（开发模式）。若需对外/局域网暴露
（`WEB_HOST` 非 127.0.0.1），**必须设置 `WEB_TOKEN`**——否则所有 POST/DELETE 写操作
（提交压测任务、删除任务/配置/语料）均无鉴权开放，`/results` 产物目录也只读开放，
可能被利用消耗 API Key 额度。

## 脚本说明

### 1. `scripts/single_dialog_ttfb.py` —— 单线程多轮对话 + TTFB
- 逐行读取 `corpus/50QA-1.txt` 中的问题（可在代码中更换），在同一会话（conversation）中连续提问
- 正确测量 TTFB：请求发出前开始计时；只有在收到真实回答内容时才记录首包时间，避免状态事件/空事件被误记为 TTFB
- 对 Dify 的 `conversation_id` 兼容读取 `conversation_started` / `message` 两种事件，防止上下文断开
- 结果写入 `results/dialogue_log0.csv`（问题 / 回答 / 总耗时 / TTFB / 会话 ID / ITL / 生成速率 / 内容块数）

```powershell
python scripts/single_dialog_ttfb.py
```

### 2. `scripts/openai_compat_dialog_ttfb.py` —— OpenAI 兼容协议多轮对话 + TTFB
- 测试形态与 `single_dialog_ttfb.py` 相同，但采用 OpenAI 兼容协议（`POST /v1/chat/completions`，SSE 流式）
- 可直接打 DeepSeek 官方 API、vLLM、Ollama、one-api 令牌代理等 OpenAI 兼容服务
- 多轮上下文通过 messages 消息历史维持（OpenAI 协议无 conversation_id 概念）
- 真实 TTFB 仅在 `delta.content` 首次出现时统计，避免首个 SSE 状态块被误计入性能指标
- 配置从 `.env` 读取：`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL_NAME` / `OPENAI_MAX_TOKENS` / `VERIFY_SSL`
- 结果写入 `results/openai_dialogue_log.csv`（问题 / 回答 / 总耗时 / TTFB）

```powershell
python scripts/openai_compat_dialog_ttfb.py
```

### 3. `scripts/multi_thread_record.py` —— Dify 风格多线程压测 + Locust 风格汇总
- `openai_compat_multi_thread_record.py` 的 Dify 风格孪生版：纯 `requests` 实现（不依赖 Locust），
  `Session` 连接复用、用户队列 + 线程池调度，以 `conversation_id` 维持多轮上下文
- 配置从 `.env` 读取：`LLM_API_URL` / `API_KEY` / `VERIFY_SSL`；用户数/线程数/轮数等全部命令行可配
- 每用户一个 CSV 记录问答明细（含 TTFB / 整轮耗时 / 响应长度 / ITL / 生成速率 / 内容块数），
  并在空响应/短响应时做异常保护，
  结束时打印 Locust 风格汇总表（按请求类型 NewChat / ContinueChat / Aggregated：# reqs / # fails /
  Median / Avg / Min / p90 / p95 / p99 / Max / RPS + ITL / 生成速率指标行），
  并追加写入根目录 `压测汇总.md`（可用 `--report` 指定其他路径）、输出机器可读
  `results/summary_dify_*.json`

```powershell
python scripts/multi_thread_record.py                          # 默认 5 用户×10 轮
python scripts/multi_thread_record.py --users 20 --threads 10 --rounds 10 --sleep 0
```

### 4. `scripts/openai_compat_multi_thread_record.py` —— OpenAI 兼容协议多线程压测
- `multi_thread_record.py` 的 OpenAI 兼容孪生版：以 `messages` 历史替代 `conversation_id` 维持多轮上下文
- 配置从 `.env` 读取：`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL_NAME` / `VERIFY_SSL`；用户数/线程数/轮数等全部命令行可配
- 每用户一个 CSV 记录问答明细（含 TTFB / 整轮耗时 / 响应长度 / ITL / 生成速率 / 内容块数），
  并在回答过短时回滚上下文避免伪命中；统计口径与 Dify 版一致（NewChat / ContinueChat / Aggregated 分组）
- 结束时打印汇总统计（成功率、TTFB 与耗时的 avg/p50/p95/max、整体吞吐、失败分布），
  并追加写入根目录 `压测汇总.md`（可用 `--report` 指定其他路径）、输出机器可读
  `results/summary_openai_*.json`

```powershell
python scripts/openai_compat_multi_thread_record.py                          # 默认 5 用户×10 轮
python scripts/openai_compat_multi_thread_record.py --users 20 --threads 10 --rounds 10 --sleep 0
```

### 5. `scripts/locust_multi_dialog.py` —— Locust 多用户多轮对话压测
- Locust 压测版：每用户独立 `conversation_id`，任务权重区分"多轮对话"与"会话初始化"
- 逐用户记录问答与响应时间到 `results/locust/chat_responses_<uuid>.csv`
- 带响应完整性校验（回答长度阈值）与错误事件处理，`VERIFY_SSL` 统一控制 TLS 校验行为
- 语料默认 `corpus/50QA-1.txt`，可用环境变量 `LOCUST_CORPUS` 覆盖

```powershell
# Web 界面模式（默认 8089 端口，在浏览器里设置并发数）
locust -f scripts/locust_multi_dialog.py --host http://115.25.86.121
# 无界面模式示例：50 用户、每秒启动 5 个、运行 2 分钟
locust -f scripts/locust_multi_dialog.py --host http://115.25.86.121 --headless -u 50 -r 5 -t 2m
```

### 6. `scripts/openai_compat_locust_multi_dialog.py` —— Locust OpenAI 兼容协议压测
- `locust_multi_dialog.py` 的 OpenAI 兼容孪生版：请求 `/chat/completions`（SSE 流式），
  以 `messages` 历史替代 `conversation_id` 维持多轮上下文
- host 默认取自 `.env` 的 `OPENAI_BASE_URL`（命令行 `--host` 优先），Key/模型读取
  `OPENAI_API_KEY` / `MODEL_NAME` / `VERIFY_SSL`；配置缺失时快速失败
- 任务权重与 Dify 版一致：「多轮对话」(权重3，每任务连续 3 轮) 与「新建会话」(权重1)
- 逐用户记录问答与响应时间（含 TTFB）到 `results/locust/openai_chat_responses_<uuid>.csv`
- 语料默认 `corpus/test1.txt`，可用环境变量 `LOCUST_CORPUS` 覆盖
- `stream=True` 时 Locust 内置统计只计到响应头到达（≈首个 SSE 数据块），因此脚本拆成两个统计行：
  `ChatCompletions-TTFB`（响应头到达耗时）与 `ChatCompletions-Total`（含流式消费的完整往返）。
  注意：首真实内容口径的 TTFB 以逐用户 CSV 的 `ttfb_ms` 字段为准，两者口径不同，引用时注意区分
- 实测该模型单轮流式生成约 15~25 秒（回答 ~1200–2100 字符），单用户请求频率偏低属正常，
  评估容量请关注并发执行情况与总吞吐

```powershell
# Web 界面模式（host 自动取自 .env）
locust -f scripts/openai_compat_locust_multi_dialog.py
# 无界面模式示例：2 用户、每秒启动 2 个、运行 2 分钟
locust -f scripts/openai_compat_locust_multi_dialog.py --headless -u 2 -r 2 -t 2m
# 耐力压测（找模型变慢/崩溃的临界点）：10 秒拉起 50 用户、零思考时间持续发、跑 30 分钟
$env:LOCUST_WAIT_TIME = "0"
locust -f scripts/openai_compat_locust_multi_dialog.py --headless -u 50 -r 5 -t 30m --html results/locust/report.html --csv results/locust/endurance
```

- 压测调优环境变量：`LOCUST_WAIT_TIME`（思考时间秒数，`0` = 收到回复立即发下一条，缺省 1~3 秒）、
  `LOCUST_MAX_TURNS`（保留最近 N 轮历史，缺省 10，防止长跑上下文膨胀，`0` = 不限制）、
  `LOCUST_TIMEOUT`（相邻响应包之间超时秒数，缺省 300，超时计失败以发现挂起/卡死）
- 找崩溃临界点的判断方法：观察周期性统计中 `ChatCompletions-TTFB` / `ChatCompletions-Total`
  的中位数和高分位是否随时间持续抬升（逐渐变慢），以及 `# fails` 是否开始增长（崩溃/超时）

> 注：早期 CERAI 测试曾附带一个日志转 CSV 的小工具（`testCERAI/数据/logTOtxtTOcsv.py`），
> 已随去重清理与历史重建移出仓库。

## 语料说明（corpus/）

| 文件 | 内容 |
|---|---|
| `short10words.txt` ~ `long3000words.txt` | 10 / 100 / 300 / 500 / 1000 / 2000 / 3000 字分档长文本，用于"输入长度 × 并发数"矩阵测试 |
| `test1.txt` | 逐行短问题集（OpenAI 兼容 Locust 脚本默认语料） |
| `test2.txt` | 逐行短问题集（`openai_compat_multi_thread_record.py` 默认语料；长文本输入测试请用上方 `long*words.txt` 分档语料） |
| `50QA-1.txt` / `50QA-2.txt` | 问答式问题集（单线程/多线程 Dify 脚本默认语料；各 50 条） |

## 测试输出说明（results/）

- 多线程脚本输出：`chat_responses_*.csv` / `openai_chat_responses_*.csv`（每用户一个文件），
  逐轮明细列：`timestamp, user_id, (conversation_id,) question, answer, ttfb_ms,
  response_time_ms, response_length` + **G1 指标列**：`itl_avg_ms, itl_p95_ms,
  gen_chars_per_sec, chunk_count`（内容块 < 2 时 ITL 列为空）
- 单线程脚本输出：`dialogue_log0.csv` / `openai_dialogue_log.csv`（同上含 G1 指标列）
- `results/summary_dify_*.json` / `results/summary_openai_*.json`：多线程脚本每次运行产出的
  **机器可读汇总**（schema v1：totals / fail_categories / by_request_type 分组指标，
  无样本指标为 `null`），可用 `python -m json.tool` 校验；`压测汇总.md` 对应记录会附
  **失败分布**行（仅在有失败时）与该 JSON 的相对路径链接
- `results/locust/`：Locust 脚本逐用户问答明细（`*_chat_responses_<uuid>.csv`，含 G1 指标列）及 `--html/--csv` 报告
- 早期 CERAI 平台「字数 × 并发数」测试数据（`testCERAI/` 目录）已在 2026-09 历史重建时移除

## 指标口径说明（ITL / 生成速率）

- **TTFB**：首个真实回答内容到达耗时（排除 SSE 状态事件/空事件）；Locust 报表的 `-TTFB` 行是
  "响应头到达"口径（Locust `stream=True` 机制决定），两者并存、引用时注意区分
- **ITL（Inter-chunk Latency）**：相邻两个非空内容块的到达时刻之差（ms）；首个内容块不产生样本；
  逐轮明细中 `itl_avg_ms / itl_p95_ms` 为该轮样本的均值与 p95，内容块 < 2 时为空
- **生成速率（gen_chars_per_sec）**：`len(answer) / (末内容块时刻 - 首内容块时刻)`，即纯生成阶段
  （不含 TTFB）的字符产出速率；内容块不足或时刻异常时为 null
- **失败轮次样本口径**：失败轮**不入全局聚合统计**（控制台 / 压测汇总.md / JSON 仅统计成功轮次，
  与 TTFB / 整轮耗时口径一致）；失败分布按 `fail_category` 计数。**逐轮 CSV 的留档行为因脚本族而异**：
  基线脚本（`single_dialog_ttfb.py` / `openai_compat_dialog_ttfb.py`）失败轮也保留一行
  （answer 为空或"请求失败"，便于人工复盘）；多线程与 Locust 脚本失败轮**不写入逐轮 CSV**
  （CSV 无 fail 标记列，中途流断已收的测量值仅用于失败计数）
- **口径声明**：ITL 与生成速率为客户端测量，包含网络抖动与服务端批处理间隙，
  仅用于同一环境的纵向对比，不做跨网络绝对值比较

## 公共模块（scripts/llmperf_common/）

6 个测试脚本的配置加载、SSE 解析、失败分类与统计/报告输出均收敛到公共模块（仅标准库）：

| 模块 | 职责 |
|---|---|
| `config.py` | `.env` 加载、VERIFY_SSL、Key 自检/脱敏、配置聚合 |
| `sse.py` | SSE 解析（Dify / OpenAI 两协议）+ TTFB / ITL / 生成速率采集（StreamTimer） |
| `stats.py` | 统一 StatsCollector（分组统计）、失败分类（classify_exception）、MD / JSON 报告 |

离线单测位于 `scripts/tests/`（仅标准库 unittest），运行：

```powershell
python -m unittest discover -s scripts/tests -t scripts -v
```

## 安全说明

- 所有 API Key 已移出代码，统一通过项目根目录 `.env` 文件或系统环境变量注入
  （`API_KEY` / `OPENAI_API_KEY`）
- `.env` 已加入 `.gitignore`，不会被提交；配置模板为 `.env.example`
- 历史代码中硬编码过的密钥仍留在 git 历史（提交 `e04b092`）中，
  如这些密钥仍然有效，建议到对应平台**作废并重新生成**

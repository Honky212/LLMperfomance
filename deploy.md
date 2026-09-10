# LLMperfomance 安装部署指南（deploy.md）

> 适用对象：在**另一台电脑 / 服务器**上部署本项目（压测脚本 + Web 管理面板）。
> 本文以 **Linux（Ubuntu/Debian 系）** 为主，Windows 部署要点见文末附录。
> 项目源码含完整说明：`README.md`（结构/脚本/指标口径）、`脚本介绍.md`（使用路线）。

---

## 1. 可以在 Linux 上部署吗？——可以，且是推荐环境

结论：**完全支持 Linux 部署**。依据：

1. **代码层面已做跨平台处理**（不是"仅 Windows 可用"的项目）：
   - `web/executor.py` 的任务取消在非 Windows 平台走 `os.killpg(pid, SIGTERM)`（POSIX 分支），Windows 的 `taskkill`/`CREATE_NEW_PROCESS_GROUP` 都有 `os.name == "nt"` 守卫；
   - 所有路径均使用 `os.path.join` / `pathlib.Path`，无硬编码盘符或反斜杠；
   - 输出文件统一 UTF-8（含 `utf-8-sig` CSV），无 Windows 专属编码依赖。
2. **依赖全部跨平台**：`requests` / `locust` / `httpx` / `openai` / `fastapi` / `uvicorn` 等均有 Linux wheel。
3. **官方已提供容器化方案**：`web/Dockerfile` 基于 `python:3.11-slim`，即官方默认 Linux 运行。

推荐部署环境：

| 项 | 推荐值 |
|---|---|
| 操作系统 | Ubuntu 22.04 / 24.04（Debian 12 亦可；CentOS/RHEL 把 `apt` 换成 `dnf`） |
| Python | **3.11 或 3.12**（必须 ≥ 3.10，代码使用 `X \| None` 类型注解；开发环境为 3.11） |
| 内存/磁盘 | ≥ 2GB 内存；磁盘 ≥ 2GB（压测结果 CSV/HTML 会持续累积） |
| 网络 | 能访问被测接口（`LLM_API_URL` 的 Dify 服务，或 `OPENAI_BASE_URL` 的 OpenAI 兼容服务） |
| 端口 | 面板默认 **8686**（可改）；Locust 自带的 Web UI 是 **8089**（仅用 `--headless` 可不开） |

---

## 2. 获取项目代码

> ⚠️ **重要提醒（针对本仓库当前状态）**：本地工作区有大量**尚未提交**的改动
> （`web/` 整个目录、`scripts/llmperf_common/`、`scripts/tests/`、`scripts/analyze_summary.py` 等均为 untracked）。
> 如果目标机器打算用 `git clone` 部署，请**先在本机提交并推送**这些改动，否则克隆下来的仓库会缺 Web 面板等关键部分；
> 如果直接用文件拷贝（tar/rsync）则不受影响（会把未提交内容一并带上）。

### 方式 A：git clone（推荐，需先推送全部改动）

```bash
git clone <你的仓库地址> /opt/llmperf
cd /opt/llmperf
```

### 方式 B：从当前这台电脑直接打包拷贝

在**当前（Windows）机器**的项目根目录打包（排除运行产物与敏感文件）：

```powershell
# PowerShell，在 D:\learn\LLMperfomance 下执行
tar czf llmperf-deploy.tar.gz `
  --exclude=.git --exclude=.env --exclude=web/data `
  --exclude=results --exclude=log --exclude='__pycache__' `
  --exclude=.venv --exclude=.pytest_cache --exclude=corpus `
  .
```

> 说明：`corpus/` 里的语料只是示例文本，可自行在目标机器补建；若想原样保留可去掉 `--exclude=corpus`。
> `.env` 含密钥，**不随包传输**，目标机器上重新填写（见 §4）。

传到目标机器并解压：

```bash
scp llmperf-deploy.tar.gz user@linux-host:/tmp/
ssh user@linux-host
sudo mkdir -p /opt/llmperf && sudo chown $USER:$USER /opt/llmperf
tar xzf /tmp/llmperf-deploy.tar.gz -C /opt/llmperf
```

> 行尾提示：若从 Windows 拷贝，文本文件可能带 CRLF，Python 运行无碍；
> 想统一成 LF 可执行 `find /opt/llmperf -type f \( -name '*.py' -o -name '*.md' \) -exec sed -i 's/\r$//' {} +`

---

## 3. 安装 Python 环境与依赖

```bash
# 1) 安装 Python 3.11（若系统已带 3.10~3.12 可跳过）
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip

# 2) 创建虚拟环境（务必使用虚拟环境，勿装到系统 Python）
cd /opt/llmperf
python3.11 -m venv .venv
source .venv/bin/activate

# 3) 升级 pip 并安装依赖（两条都装，web 依赖里已含脚本所需 requests/locust/openai）
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-web.txt

# 4) 验证关键依赖
python -c "import requests, locust, openai, fastapi, uvicorn; print('deps OK')"
```

> 若 `pip install locust` 报编译错误（缺少 gevent 等 wheel），先安装系统编译依赖：
> `sudo apt install -y build-essential python3.11-dev libffi-dev`

---

## 4. 配置 `.env`（密钥与接口地址）

```bash
cp .env.example .env
vim .env   # 用编辑器填写实际值
chmod 600 .env   # 含密钥，收紧权限
```

| 变量 | 用途 | 必填 |
|---|---|---|
| `LLM_API_URL` | Dify 应用基础地址（脚本自动拼 `/v1/chat-messages`） | Dify 脚本用 |
| `API_KEY` | Dify 应用 Key（形如 `app-xxx`） | Dify 脚本用 |
| `OPENAI_BASE_URL` | OpenAI 兼容接口基础 URL（SDK 自动拼 `/chat/completions`） | OpenAI 兼容脚本用 |
| `OPENAI_API_KEY` | OpenAI 兼容 Key（形如 `sk-xxx`） | OpenAI 兼容脚本用 |
| `MODEL_NAME` | 模型 ID（渠道中配置的模型名） | OpenAI 兼容脚本用 |
| `OPENAI_MAX_TOKENS` | 最大输出 token 数（`0` = 不限制） | 可选 |
| `VERIFY_SSL` | TLS 校验开关，默认 `true`；本地自签名环境临时设 `false` | 可选 |
| `LOCUST_WAIT_TIME` / `LOCUST_MAX_TURNS` / `LOCUST_TIMEOUT` | Locust 压测调优（思考时间/历史轮数/包间超时） | 可选 |
| `WEB_HOST` | 面板监听地址，默认 `127.0.0.1`（对外暴露改 `0.0.0.0`） | 可选 |
| `WEB_PORT` | 面板端口，默认 `8686` | 可选 |
| `WEB_TOKEN` | **对外暴露时必须设置**（写接口 Bearer 鉴权） | 见 §8 |

要点：

- **系统环境变量优先于 `.env` 文件**（代码行为，勿困惑）；
- 占位值（`app-xxxx` / `sk-xxxx`）或重复前缀（`app-app-` / `sk-sk-`）会导致脚本快速失败并提示，属正常防护；
- 面板提交任务时可选用"配置档"覆盖 `.env`（配置档存于 SQLite，Key 明文），二者取其一即可，不冲突。

---

## 5. 部署验证（建议按顺序执行）

### 5.1 跑离线单测（不依赖任何外部服务）

```bash
cd /opt/llmperf && source .venv/bin/activate
python -m unittest discover -s scripts/tests -t scripts -v
```

全部通过说明公共模块（SSE 解析 / 统计 / 失败分类）安装正确。

### 5.2 命令行冒烟：Fake Script（不调用真实 LLM、不消耗额度）

```bash
FAKE_ROUNDS=3 python web/fake_script.py
# 期望输出 3 行 LLMPERF_PROGRESS round_done 并在当前目录生成 summary_fake_*.json
```

### 5.3 启动 Web 面板（前台方式，先确认能起来）

```bash
cd /opt/llmperf && source .venv/bin/activate
python web/server.py
# 看到 "Starting on http://127.0.0.1:8686" 即成功
```

浏览器访问 `http://<服务器IP>:8686`（本机则 `http://127.0.0.1:8686`）。

### 5.4 Web 面板端到端冒烟（推荐，验证 执行器+SQLite+SSE 全链路）

1. 打开首页 → **新建任务**；
2. 脚本选择 **`Fake Script (M3 smoke test)`**（注册表内置，无需配置档、无需 API Key）；
3. 轮数填 3，提交；
4. 进入任务详情页，应能看到实时进度（SSE）滚动，最终状态 `completed`，并生成 summary JSON 与 HTML 报告。

> 若这步通过，说明整条"Web 提交 → 子进程执行 → 进度上报 → 结果落库"链路在 Linux 上正常，
> 之后换成真实脚本（Baseline/Concurrent/Endurance）即可。

---

## 6. 正式部署 Web 面板（systemd 常驻服务）

### 6.1 建运行用户（可选但推荐，勿用 root 跑）

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/llmperf llmperf
sudo chown -R llmperf:llmperf /opt/llmperf
# 运行期要写：results/ log/ web/data/
sudo -u llmperf mkdir -p /opt/llmperf/results /opt/llmperf/log /opt/llmperf/web/data
```

### 6.2 编写 systemd 单元

```bash
sudo tee /etc/systemd/system/llmperf.service > /dev/null <<'EOF'
[Unit]
Description=LLM Perf Panel (FastAPI + single-queue executor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=llmperf
Group=llmperf
WorkingDirectory=/opt/llmperf
# 使用 venv 解释器：面板与它拉起的压测子进程（python/locust）共用同一环境
ExecStart=/opt/llmperf/.venv/bin/python /opt/llmperf/web/server.py
Restart=on-failure
RestartSec=3
# 应用会自行加载项目根 .env（load_dotenv），无需 EnvironmentFile
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now llmperf
sudo systemctl status llmperf
```

### 6.3 常用运维命令

```bash
sudo systemctl restart llmperf        # 重启（重启后残留 running/pending 任务会被自动标记 orphaned）
sudo journalctl -u llmperf -f         # 实时看服务日志
tail -f /opt/llmperf/log/server.log   # 应用日志（含任务执行明细）
```

> **架构约束提醒**：面板是**单进程 + 单队列**设计——同一时间只允许一个压测任务（有任务时新提交返回 409）。
> 不要用多 worker 方式部署（uvicorn `--workers N`、gunicorn 多进程等都不适用），保持单进程常驻即可。

---

## 7.（可选）Nginx 反向代理

若要多机访问/加域名 HTTPS，可在前面加 Nginx（面板仍只绑 `127.0.0.1`）：

```nginx
server {
    listen 80;
    server_name perf.example.com;

    location / {
        proxy_pass http://127.0.0.1:8686;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;         # 关键：SSE 实时进度不能被缓冲
        proxy_read_timeout 3600s;    # 压测可能持续很久
        client_max_body_size 10m;    # 语料上传（面板限制 5MB）
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## 8. 安全配置（务必阅读）

- 默认只绑 `127.0.0.1` 且**无鉴权**（开发模式）；
- **只要 `WEB_HOST` 不是 127.0.0.1（局域网/公网暴露），必须设置 `WEB_TOKEN`**：
  - 在 `.env` 加 `WEB_TOKEN=<强随机串>`，重启面板；
  - 此后所有 POST/PUT/DELETE 写接口要求 `Authorization: Bearer <token>`（页面登录后自动携带；SSE 用 `?token=`）；
  - 不设置则任何能访问端口的人都能提交压测任务（消耗你的 API Key 额度）、删除任务/配置/语料。
- API Key 在面板 SQLite（`web/data/llmperf.db`）中**明文存储**，仅建议本机/内网使用；请确保 `web/data/` 权限收紧、不入库、不随备份外传；
- 建议防火墙只放行需要的端口：`sudo ufw allow 8686/tcp`（若 Nginx 在外层则只放 80/443）。

---

## 9. Linux 与 Windows 的差异注意点

| 事项 | 说明 |
|---|---|
| 任务取消机制 | Linux 走 `killpg + SIGTERM`（代码已内置 POSIX 分支），无需额外处理 |
| 目录权限 | `results/`、`log/`、`web/data/` 必须对运行用户可写（否则任务失败/面板 500） |
| 路径大小写 | Linux 区分大小写，命令/参数里的相对路径与文件名必须与仓库一致 |
| 中文输出 | 服务器 locale 建议 UTF-8：`export LANG=C.UTF-8`（或 `zh_CN.UTF-8`）；面板拉起的子进程已强制 `PYTHONIOENCODING=utf-8` |
| Locust 命令行 | 与 README 一致：`locust -f scripts/xxx.py --headless -u 20 -r 2 -t 2m`（venv 内执行） |
| 自签名/内网 HTTP | 若被测服务是 http 或自签名证书，设 `VERIFY_SSL=false`（脚本会打印明显警告） |
| 时区 | 仅影响日志/展示时间戳，不影响压测指标 |
| 与 Windows 版差异 | 无功能差异；唯一环境差异是 Windows 上取消任务用 `taskkill /F /T`，Linux 自动切换为 SIGTERM |

---

## 10.（可选）Docker 部署

仓库自带 `web/Dockerfile`（python:3.11-slim，已含面板+脚本全部依赖）：

```bash
cd /opt/llmperf
docker build -t llmperf-panel -f web/Dockerfile .

mkdir -p results web/data   # 持久化目录

# 方式一：环境变量注入（镜像内没有 .env，用 --env-file 传入本机 .env）
docker run -d --name llmperf-panel --restart unless-stopped \
  -p 8686:8686 \
  --env-file .env \
  -e WEB_HOST=0.0.0.0 \
  -v "$(pwd)/results:/app/results" \
  -v "$(pwd)/web/data:/app/web/data" \
  llmperf-panel

# 方式二：带鉴权
#  -e WEB_TOKEN=mysecret \
```

要点：

- 镜像内 `WEB_HOST` 默认已是 `0.0.0.0`，容器必须用 `--env-file .env`（或逐个 `-e`）传密钥，镜像不携带 `.env`；
- 必须挂载 `web/data`（SQLite 库+上传语料）与 `results`（产物），否则容器重建即丢数据；
- 容器内同样单进程单队列，勿加 `--scale`。

---

## 11. 部署后首次使用速览

1. **配置档**：面板「配置」页新增配置档（选 Dify / OpenAI 兼容协议，填 base_url + Key + 模型）——相当于把 `.env` 挪到界面管理，二选一即可；
2. **语料**：「语料」页可上传（5MB 上限），或直接用仓库内置 `corpus/*.txt`；
3. **提交任务**：新建任务 → 按"基线(Baseline) → 并发(Concurrent) → 耐力(Endurance)"顺序逐步加压；
4. **看结果**：任务详情页实时进度 + summary JSON + 自动生成的 HTML 报告；`results/` 下逐用户 CSV 可留档复盘；
5. **命令行直跑**（不经面板）同样可用：

```bash
# 单用户基线（OpenAI 兼容）
python scripts/openai_compat_baseline.py
# 并发 20 用户
python scripts/openai_compat_concurrent.py --users 20 --threads 10 --rounds 10 --sleep 0
# Locust 耐力（30 分钟）
LOCUST_WAIT_TIME=0 locust -f scripts/openai_compat_endurance_locust.py --headless -u 50 -r 5 -t 30m --html results/locust/report.html --csv results/locust/endurance
```

---

## 12. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 启动报 `API_KEY 未配置` / `OPENAI_API_KEY 未配置` / `MODEL_NAME 未配置` | `.env` 未复制、仍是占位值（`app-xxxx`/`sk-xxxx`），或系统环境变量覆盖了 `.env` 值 |
| `ModuleNotFoundError: No module named 'fastapi'/'requests'` | 未激活 venv 或依赖没装全（检查 §3 两条 requirements） |
| `SyntaxError` / `invalid syntax` 指向 `\|` 类型注解 | Python 版本 < 3.10，需升级到 3.10+ |
| `ModuleNotFoundError: llmperf_common` | 未从项目根目录运行 `python scripts/xxx.py`（脚本入口目录需在 sys.path） |
| 面板任务一直 `pending/running` 卡住 | 重启面板服务即可——启动时会把残留任务自动标记为 `orphaned`（设计如此，防 409 卡死） |
| 提交任务返回 409 | 单队列串行是**设计约束**：等当前任务结束/取消后再提交 |
| 页面进度不刷新/卡住 | 走了 Nginx 等反代但未关 `proxy_buffering`（见 §7）；或直连测试排除代理因素 |
| 端口被占用 | `.env` 改 `WEB_PORT` 后重启 |
| pip 装 locust/gevent 编译失败 | `sudo apt install -y build-essential python3.11-dev libffi-dev` 后重装 |
| `database is locked` | 正常不会出现（WAL 已启用）；请勿同时启动两个 `server.py` 实例 |
| CLI 直接跑脚本时中文报 `UnicodeEncodeError` | `export LANG=C.UTF-8`（面板子进程不受影响，已强制 utf-8） |
| 面板无鉴权警告刷屏 | 绑了非 127.0.0.1 且没设 `WEB_TOKEN`——按 §8 设置后消除 |

---

## 13. 附录：Windows 部署要点（目标机是 Windows 时）

```powershell
# 1) 安装 Python 3.11（勾选 Add to PATH），项目目录解压/克隆
# 2) 虚拟环境 + 依赖
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-web.txt
# 3) 配置
Copy-Item .env.example .env   # 编辑填写
# 4) 验证
python -m unittest discover -s scripts\tests -t scripts -v
# 5) 启动面板
python web\server.py          # http://127.0.0.1:8686
# 6)（可选）开机自启：任务计划程序创建"启动程序"任务指向 .venv\Scripts\python.exe + web\server.py
```

Windows 额外注意：取消任务走 `taskkill /F /T`（代码自动识别）；CSV 用 `utf-8-sig` 编码，Excel 打开不乱码；`VERIFY_SSL=false` 仅用于自签名本地环境。

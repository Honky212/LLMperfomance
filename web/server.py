# -*- coding: utf-8 -*-
"""
web.server —— FastAPI 应用（V2.2 §4.6）

单进程：Jinja2 页面渲染 + REST API + SSE 实时进度 + 执行器调度。
启动：python web/server.py  （默认 http://127.0.0.1:8686）
安全：默认仅绑 127.0.0.1 且无鉴权（开发模式）；对外暴露（WEB_HOST 非
127.0.0.1）必须设置 WEB_TOKEN，否则所有 POST/DELETE 写接口无鉴权开放。
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Query, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---- 日志配置：写入 log/ 目录 ----
_LOG_DIR = Path(__file__).resolve().parent.parent / "log"
_LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "server.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("llmperf")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db
import script_registry
from executor import Executor

# ---- .env 加载（WEB_* 变量支持）----
# 项目根 .env 中的 WEB_TOKEN / WEB_HOST / WEB_PORT 等面板配置自动生效，
# 无需每次手动 $env:WEB_TOKEN=xxx。系统环境变量仍优先于 .env。
try:
    from dotenv import load_dotenv
    _project_root = Path(__file__).resolve().parent.parent
    load_dotenv(_project_root / ".env")
except ImportError:
    pass  # 未装 python-dotenv 时静默跳过，仍可手动设环境变量

# ---- 初始化 ----

app = FastAPI(title="LLM 性能压测平台", version="2.2")
WEB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_DIR.parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# 静态文件（echarts.min.js 等）
static_dir = WEB_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# 结果产物目录只读服务（Web 优化 V2.0 §5.5 / P0-1）：
# 任务产出的 summary JSON / 逐轮 CSV / report_*.html / locust CSV+HTML 统一经 /results 访问，
# task_detail iframe 直连，无需逐文件 API。
_results_dir = PROJECT_ROOT / "results"
_results_dir.mkdir(exist_ok=True)
app.mount("/results", StaticFiles(directory=str(_results_dir)), name="results")

# 语料上传存储目录（Web 优化 V2.0 §3.1 / §6）
CORPORA_DIR = PROJECT_ROOT / "web" / "data" / "corpora"
CORPORA_MAX_BYTES = 5 * 1024 * 1024  # 上传大小上限 5MB（P1-5）
_BUILTIN_CACHE: dict[str, tuple[float, int, int]] = {}  # path -> (mtime, size, lines)（P1-5 行数缓存）

executor = Executor()


@app.on_event("startup")
async def startup():
    db.init_db()
    orphaned = db.mark_stale_tasks_orphaned()
    if orphaned:
        print(f"[startup] Marked {orphaned} stale task(s) (running/pending) as orphaned")
    # 语料上传存储目录（Web 优化 V2.0 §6）
    (PROJECT_ROOT / "web" / "data" / "corpora").mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_event_loop()
    executor.set_event_loop(loop)
    # 后台清理过期缓冲
    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            executor.cleanup_expired()
    asyncio.create_task(cleanup_loop())


# ---- 全局异常处理：ASGI 层未捕获异常写入日志 ----

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.method} {request.url.path}: "
                 f"{exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


# ---- 页面路由 ----

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    runs = db.list_test_runs(limit=10)
    return templates.TemplateResponse("index.html", {"request": request, "runs": runs})


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    runs = db.list_test_runs(limit=50)
    return templates.TemplateResponse("tasks.html", {"request": request, "runs": runs})


@app.get("/tasks/new", response_class=HTMLResponse)
async def new_task_page(request: Request):
    # V2.0 §7.1：模板仅需 id/name/protocol（不含 api_key 等字段）
    profiles = db.list_profiles()
    slim = [{"id": p["id"], "name": p["name"], "protocol": p["protocol"]} for p in profiles]
    return templates.TemplateResponse("new_task.html", {"request": request, "profiles": slim})


@app.get("/corpora", response_class=HTMLResponse)
async def corpora_page(request: Request):
    """语料管理页（Web 优化 V2.0 §7.2）。"""
    return templates.TemplateResponse("corpora.html", {"request": request})


@app.get("/tasks/{run_id}", response_class=HTMLResponse)
async def task_detail_page(request: Request, run_id: int):
    run = db.get_test_run(run_id)
    if not run:
        raise HTTPException(404, "Task not found")
    analyses = db.get_analyses_for_test(run_id)
    # 规则引擎结果存于 findings_json（report_md 仅 LLM 引擎有）：
    # 解析为列表供模板渲染明细，避免只显示 "rule (ok)" 无内容
    for a in analyses:
        a["_findings"] = None
        if a.get("findings_json"):
            try:
                a["_findings"] = json.loads(a["findings_json"])
            except json.JSONDecodeError:
                a["_findings"] = None
    return templates.TemplateResponse("task_detail.html", {
        "request": request, "run": run, "analyses": analyses,
    })


@app.get("/configs", response_class=HTMLResponse)
async def configs_page(request: Request):
    profiles = db.list_profiles()
    return templates.TemplateResponse("configs.html", {"request": request, "profiles": profiles})


# ---- REST API ----

@app.post("/api/tasks")
async def create_task(body: dict):
    """提交任务：{script, test_type?, params?, profile_id?}（Web 优化 V2.0 §5.1）

    校验与推导：
    - script 必须在注册表内（7 项含 fake，P0-3）；
    - 脚本协议与 profile 协议一致（P1-4），否则 422；
    - test_type 一律以注册表推导值为准落库，客户端冲突值忽略并告警（P2-9）；
    - params 与注册表默认值合并并做类型/范围/格式校验（P2-7），非法 422；
    - 语料字段解析为绝对路径后入库（P1-5 / §5.1-4）。
    """
    script = body.get("script")
    schema = script_registry.get_script(script)
    if not schema:
        raise HTTPException(400, f"未知脚本: {script}（不在脚本注册表内）")

    # 单队列执行器并发防护（executor 无内部排队，MAX_CONCURRENT_TASKS=1 仅注释宣称）：
    # 已有 pending/running 任务时拒绝新提交（409），避免两个压测（尤其两个 Locust）
    # 并发抢跑、互相污染指标。前端 new_task.html 会把 detail 展示给用户。
    active = db.list_active_tasks()
    if active:
        a = active[0]
        label = a.get("run_no") or f"#{a['id']}"
        raise HTTPException(
            409,
            f"已有任务 {label}（{a['script']}）处于 {a['status']} 状态：单队列执行器"
            f"同一时间只允许一个任务，请等待其结束/取消后再提交（若长期卡在 "
            f"pending/running，重启面板会自动将其标记为 orphaned）",
        )

    profile_id = body.get("profile_id")

    # P1-4：脚本协议 ↔ profile 协议一致性（any=不校验，如 fake）
    protocol = script_registry.script_protocol(script)
    if protocol != "any" and profile_id:
        prof = db.get_profile(profile_id)
        if prof and prof["protocol"] != protocol:
            raise HTTPException(
                422,
                f"脚本协议({protocol}) 与配置档协议({prof['protocol']})不匹配，"
                f"请选择协议一致的配置档（配置档 #{profile_id} {prof['name']}）",
            )

    # params 默认合并 + 校验
    params = script_registry.default_params(script)
    raw = body.get("params") or {}
    keys = script_registry.field_keys(script)
    params.update({k: v for k, v in raw.items() if k in keys})
    ok, errors = script_registry.validate_params(script, params)
    if not ok:
        raise HTTPException(422, {"errors": errors})

    # 语料字段 → 绝对路径（内置 corpus/xxx.txt 或上传 uploaded:{id}）
    try:
        script_registry.resolve_corpora(script, params)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # P2-9：test_type 以注册表推导为准
    test_type = script_registry.test_type_of(script)
    req_tt = body.get("test_type")
    if req_tt and req_tt != test_type:
        logger.warning(f"Task script={script}: ignoring client test_type={req_tt}, "
                       f"derived={test_type}")

    run_id = db.create_test_run(profile_id, test_type, script, params)

    # 异步调度执行
    asyncio.create_task(_execute_task(run_id, script, params, profile_id))

    return {"id": run_id, "status": "pending"}


def _build_profile_env(profile_id) -> dict:
    """根据 profile_id 构建子进程环境变量注入（V2.2 §4.6.1 映射表）。

    返回空 dict 表示无 profile 或 profile 不存在（子进程回退读 .env）。
    """
    if not profile_id:
        return {}
    profile = db.get_profile(profile_id)
    if not profile:
        logger.warning(f"Profile #{profile_id} not found, falling back to .env")
        return {}

    env = {}
    protocol = profile["protocol"]

    if protocol == "dify":
        env["LLM_API_URL"] = profile["base_url"]
        # api_key 为本地明文存储（仅内网/本机场景使用；web/data/ 已 gitignore）。
        # 如后续接入 Fernet 加密，解密逻辑放在此处注入前。
        if profile.get("api_key"):
            env["API_KEY"] = profile["api_key"]
        env["VERIFY_SSL"] = "true" if profile.get("verify_ssl", 1) else "false"
    elif protocol == "openai_compat":
        env["OPENAI_BASE_URL"] = profile["base_url"]
        if profile.get("api_key"):
            env["OPENAI_API_KEY"] = profile["api_key"]
        if profile.get("model_name"):
            env["MODEL_NAME"] = profile["model_name"]
        if profile.get("max_tokens"):
            env["OPENAI_MAX_TOKENS"] = str(profile["max_tokens"])
        env["VERIFY_SSL"] = "true" if profile.get("verify_ssl", 1) else "false"

    logger.info(f"Profile #{profile_id} ({profile['name']}) env injected: "
                f"protocol={protocol}, base_url={profile['base_url']}, "
                f"model={profile.get('model_name', '—')}")
    return env


def _allocate_run_no(started: datetime) -> str:
    """按启动时刻分配展示用日期 ID（run_no）：YYYYMMDDHHMM。

    产物目录命名 run_{run_no}。同一分钟内启动多个任务时追加 -2/-3 后缀防目录冲突；
    旧记录（run_no IS NULL）不受影响，仍显示数字 ID、目录保持 run_{id}。
    """
    base = started.strftime("%Y%m%d%H%M")
    existing = db.list_run_nos()
    cand = base
    n = 2
    while cand in existing or (PROJECT_ROOT / "results" / f"run_{cand}").exists():
        cand = f"{base}-{n}"
        n += 1
    return cand


async def _execute_task(run_id: int, script: str, params: dict, profile_id=None):
    """后台执行任务并更新数据库状态（Web 优化 V2.0 §5.2）。

    健壮性（修复 #5）：
    - 整个函数体（含 run_no 分配 / 目录创建 / 命令翻译 / 状态落库）都在 try 内，
      任何启动期异常都统一落 failed，不遗留永久 pending 卡死 409 并发闸门；
    - 协程启动先复核 DB 状态：记录已被删除或已不在 pending（如被取消）时直接返回，
      不会对已删除任务拉起压测子进程。
    """
    from datetime import datetime as dt

    # 启动前复核：pending 任务可能在调度协程执行前被用户删除/取消（单线程事件循环内
    # 本协程从开始到 update(running) 之间无 await，删除只能发生在此之前，此处兜底即可）
    pre = db.get_test_run(run_id)
    if not pre or pre["status"] != "pending":
        logger.info(f"Task #{run_id} skipped: 记录不存在或状态={pre and pre['status']}（非 pending）")
        return

    try:
        logger.info(f"Task #{run_id} starting: script={script}, params={params}, profile_id={profile_id}")

        # 启动时刻 = run_no（YYYYMMDDHHMM，展示 ID）+ 产物目录（run_{run_no}）的唯一基准
        started_dt = dt.now()
        run_no = _allocate_run_no(started_dt)
        result_dir = str(PROJECT_ROOT / "results" / f"run_{run_no}")
        os.makedirs(result_dir, exist_ok=True)

        # V2.0 §5.2/§5.3：注册表集中翻译参数 → (launcher, args, env_extra)。
        # FAKE_ROUNDS 由 fake 条目 env 字段翻译进 env_extra，此处不再特判（旧 L203-204 逻辑删除）。
        launcher, args, env_extra = script_registry.build_command(script, params, result_dir)
        env_inject = {
            "LLMPERF_OUTPUT_DIR": result_dir,
            "LLMPERF_SUMMARY": "1",
            "LLMPERF_HTML_REPORT": "1",
            **env_extra,
        }
        # 注入 profile 配置（V2.2 §4.6.1）
        env_inject.update(_build_profile_env(profile_id))

        db.update_test_run(run_id, status="running",
                           started_at=started_dt.isoformat(timespec="seconds"),
                           run_no=run_no, result_dir=result_dir)

        async def on_status(tid, status, extra):
            logger.info(f"Task #{tid} status change: {status} extra={extra}")
            updates = {"status": status}
            # started_at/run_no 已在启动前置更新中落库（run_no 与结果目录同源），此处不再覆盖
            if status in ("completed", "failed", "cancelled"):
                updates["finished_at"] = dt.now().isoformat(timespec="seconds")
                if extra.get("error"):
                    updates["error"] = extra["error"]
            db.update_test_run(tid, **updates)

        status, extra = await executor.run_task(
            run_id, script, env_inject, args=args, launcher=launcher,
            on_status_change=on_status,
        )
        logger.info(f"Task #{run_id} finished: status={status}")
        # 尝试读取 summary JSON（仅 completed；cancelled 的 locust 强杀后无汇总产物）
        if status == "completed":
            summaries = list(Path(result_dir).glob("summary_*.json"))
            if summaries:
                summary_path = str(summaries[-1])
                with open(summary_path, "r", encoding="utf-8") as f:
                    summary_data = f.read()
                html_reports = list(Path(result_dir).glob("*.html"))
                if html_reports:
                    # V2.0 P0-1：落库项目根相对路径（供 /results 路由 + iframe 直连）
                    html_path = os.path.relpath(html_reports[-1], PROJECT_ROOT).replace("\\", "/")
                else:
                    html_path = None
                db.update_test_run(run_id, summary_json=summary_data, html_report=html_path)
    except Exception as e:
        logger.error(f"Task #{run_id} exception: {e}\n{traceback.format_exc()}")
        # 头部（run_no 分配/目录创建/命令翻译）失败也会走到这里：统一落 failed，
        # 不遗留永久 pending（修复 #5：否则 list_active_tasks 的 409 闸门被永久卡死）
        db.update_test_run(run_id, status="failed", error=str(e),
                           finished_at=dt.now().isoformat(timespec="seconds"))


@app.get("/api/tasks")
async def api_list_tasks():
    return db.list_test_runs(limit=50)


@app.get("/api/tasks/{run_id}")
async def api_get_task(run_id: int):
    run = db.get_test_run(run_id)
    if not run:
        raise HTTPException(404)
    return run


@app.delete("/api/tasks/{run_id}")
async def api_delete_task(run_id: int):
    """删除任务记录（含诊断 analyses 级联 + 磁盘产物 results/run_{id}）。

    Dashboard / Tasks 列表"删除"按钮调用（Web 面板功能，不触碰压测脚本）。

    安全约束：
    - running 拒删：子进程仍在占位，删库会与执行器终态回写打架；
    - pending 可删（修复 #5）：_execute_task 启动时会复核 DB 状态，行已删则直接返回，
      不会拉起子进程（单线程事件循环内"复核→update(running)"之间无 await，删除无法插入）；
    - 仅删除解析后位于 {PROJECT_ROOT}/results 内的目录（带路径分隔符边界，防误删
      resultsX 等兄弟目录），防误删项目其他文件；相对路径先锚定 PROJECT_ROOT（低 #28）；
    - analyses 依赖外键 ON DELETE CASCADE（PRAGMA foreign_keys=ON）随行删除。
    """
    run = db.get_test_run(run_id)
    if not run:
        raise HTTPException(404, "Task not found")
    if run["status"] == "running":
        raise HTTPException(
            400,
            f"任务 #{run_id} 正在执行（status=running），不能删除；可先取消（Cancel）后再删除",
        )

    def _remove_result_dir_sync(result_dir: str) -> list[str]:
        """同步删除 results/ 内的结果目录（修复 #7：在 to_thread 中执行，避免阻塞事件循环）。"""
        removed: list[str] = []
        if not result_dir:
            return removed
        # 相对路径（历史记录可能存相对值）先锚定项目根，再 resolve
        if not os.path.isabs(result_dir):
            result_dir = str(PROJECT_ROOT / result_dir)
        results_root = (PROJECT_ROOT / "results").resolve()
        try:
            p = Path(result_dir).resolve()
        except OSError:
            return removed
        # 边界校验：必须是 results/ 内的子目录（带分隔符，防 resultsX 兄弟目录误删）
        root_prefix = str(results_root) + os.sep
        if p != results_root and str(p).startswith(root_prefix):
            try:
                shutil.rmtree(p)
                removed.append(str(p))
            except FileNotFoundError:
                pass  # 目录本就不存在，视为已清理
            except OSError as e:
                logger.warning(f"删除任务 #{run_id} 结果目录失败（记录仍将删除）: {p}: {e}")
        return removed

    removed_dirs = await asyncio.to_thread(_remove_result_dir_sync, run.get("result_dir"))

    db.delete_test_run(run_id)
    logger.info(f"Task #{run_id} deleted (status={run['status']}, removed_dirs={removed_dirs})")
    return {"deleted": True, "id": run_id, "removed_dirs": removed_dirs}


@app.post("/api/tasks/{run_id}/cancel")
async def cancel_task(run_id: int):
    """取消任务（修复 #5 / #6 / #7）。

    - running：向执行器发取消信号（Windows taskkill / POSIX killpg，在 to_thread 中执行，
      不阻塞事件循环）；终态由 executor.run_task 收尾时经 on_status 回写 cancelled，
      端点不抢先写库，避免"进程恰好自然完成却被误标 cancelled"的竞争（#6）；
    - pending：调度协程尚未启动，直接置 cancelled（_execute_task 启动复核非 pending 即返回）；
    - 已终态（completed/failed/cancelled/orphaned）：幂等返回 cancelled=False。
    """
    run = db.get_test_run(run_id)
    if not run:
        raise HTTPException(404, "Task not found")
    if run["status"] == "pending":
        db.update_test_run(run_id, status="cancelled",
                           finished_at=datetime.now().isoformat(timespec="seconds"))
        logger.info(f"Task #{run_id} cancelled while pending")
        return {"cancelled": True}
    if run["status"] != "running":
        return {"cancelled": False}  # 已终态，幂等
    ok = await asyncio.to_thread(executor.cancel_task, run_id)
    # 注意：不在此处写 cancelled——run_task 收尾会按 was_cancelled 标记回写终态（修复 #6）
    return {"cancelled": ok}


# ---- SSE 端点 ----

@app.get("/api/tasks/{run_id}/events")
async def task_events(request: Request, run_id: int, token: str = Query(default="")):
    """SSE 实时进度流（V2.2 §4.6 / 附录 A）。

    浏览器 EventSource 不能设自定义请求头，用 ?token= 传令牌。
    """
    # WEB_TOKEN 校验（可选）
    web_token = os.environ.get("WEB_TOKEN", "")
    if web_token and token != web_token:
        raise HTTPException(403, "Invalid token")

    # 修复 #9：读取 SSE Last-Event-ID 请求头（EventSource 自动重连时携带），
    # 传入 subscribe 做增量回放——慢消费者被环形缓冲挤掉的事件断线后可补回
    last_id = None
    raw_last = request.headers.get("last-event-id", "").strip()
    if raw_last.isdigit():
        last_id = int(raw_last)
    buf = executor.get_buffer(run_id)

    async def event_stream():
        nonlocal buf, last_id
        q = None  # 确保 finally 中 q 始终有定义
        if buf:
            backlog, q = buf.subscribe(last_event_id=last_id)
            # 回放 backlog
            if backlog:
                data = json.dumps(backlog, ensure_ascii=False)
                yield f"event: backlog\ndata: {data}\n\n"
                # backlog 已含终态事件（任务在订阅前已结束）：补发 event: status 并关流，
                # 避免实时循环永远等不到下一条、连接一直挂到 TTL 过期
                terminal = next((e for e in backlog if e.get("type") == "status"), None)
                if terminal is not None:
                    yield (f"event: status\ndata: "
                           f"{json.dumps({'status': terminal.get('status')})}\n\n")
                    return
            # 终态兜底（修复 #9）：增量回放（last_event_id ≥ 终态事件 seq）后 backlog 为空，
            # 但任务实际已结束——同样补发 status 收敛，避免连接挂到 30s 心跳循环
            run_now = db.get_test_run(run_id)
            if run_now and run_now["status"] in ("completed", "failed", "cancelled", "orphaned"):
                yield (f"event: status\ndata: "
                       f"{json.dumps({'status': run_now['status']})}\n\n")
                return
            yield f"event: caught_up\ndata: \n\n"
        else:
            # 任务尚未开始或 buffer 已过期
            run = db.get_test_run(run_id)
            if run and run["status"] in ("completed", "failed", "cancelled", "orphaned"):
                status_data = json.dumps({"status": run["status"]})
                yield f"event: status\ndata: {status_data}\n\n"
                return
            # 等待 buffer 创建
            for _ in range(30):
                await asyncio.sleep(0.5)
                buf = executor.get_buffer(run_id)
                if buf:
                    backlog, q = buf.subscribe(last_event_id=last_id)
                    if backlog:
                        data = json.dumps(backlog, ensure_ascii=False)
                        yield f"event: backlog\ndata: {data}\n\n"
                        terminal = next((e for e in backlog if e.get("type") == "status"), None)
                        if terminal is not None:
                            yield (f"event: status\ndata: "
                                   f"{json.dumps({'status': terminal.get('status')})}\n\n")
                            return
                    # 同上终态兜底
                    run_now = db.get_test_run(run_id)
                    if run_now and run_now["status"] in ("completed", "failed", "cancelled", "orphaned"):
                        yield (f"event: status\ndata: "
                               f"{json.dumps({'status': run_now['status']})}\n\n")
                        return
                    yield f"event: caught_up\ndata: \n\n"
                    break
            else:
                yield f"event: status\ndata: {json.dumps({'status': 'timeout'})}\n\n"
                return

        # 实时推送
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    sse_event = event.get("sse_event", "progress")
                    seq = event.get("seq", 0)
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"id: {seq}\nevent: {sse_event}\ndata: {data}\n\n"
                    # 终态事件后关闭流
                    if event.get("type") == "status":
                        break
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield ": heartbeat\n\n"
        finally:
            if buf and q is not None:
                buf.unsubscribe(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- 配置 API ----

@app.get("/api/configs")
async def api_list_configs():
    profiles = db.list_profiles()
    # 脱敏：不返回明文 key（响应层屏蔽，落库为明文——见 config_profiles.api_key 注释）
    for p in profiles:
        if p.get("api_key"):
            p["api_key"] = "***hidden***"
    return profiles


@app.post("/api/configs")
async def api_create_config(body: dict):
    pid = db.create_profile(
        name=body["name"],
        protocol=body["protocol"],
        base_url=body["base_url"],
        api_key=body.get("api_key", ""),
        model_name=body.get("model_name", ""),
        max_tokens=body.get("max_tokens", 0),
        verify_ssl=body.get("verify_ssl", 1),
    )
    return {"id": pid}


@app.delete("/api/configs/{profile_id}")
async def api_delete_config(profile_id: int):
    """删除配置档（如填错的模型 profile）。

    test_runs.profile_id 外键为 ON DELETE SET NULL：历史任务保留但 profile 置空，
    不影响已完成任务的 summary 等产物。
    """
    ok = db.delete_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"deleted": True, "id": profile_id}


# ---- 脚本注册表 API（Web 优化 V2.0 §4.3 / §7.1）----

@app.get("/api/scripts")
async def api_list_scripts():
    """注册表脚本列表（7 项：fake + 6 真实），前端据此动态渲染参数表单。"""
    return script_registry.list_scripts()


# ---- 语料管理 API（Web 优化 V2.0 §6）----

def _count_lines(text: str) -> int:
    """统计非空行数（与脚本 read_questions 口径一致）。"""
    return sum(1 for line in text.splitlines() if line.strip())


def _scan_builtin_corpora() -> list[dict]:
    """扫描 corpus/*.txt（内置语料，不入库），带 mtime 行数缓存（P1-5）。"""
    out = []
    corpus_root = PROJECT_ROOT / "corpus"
    if not corpus_root.is_dir():
        return out
    for p in sorted(corpus_root.glob("*.txt")):
        try:
            mtime = p.stat().st_mtime
            cached = _BUILTIN_CACHE.get(str(p))
            if cached and cached[0] == mtime:
                _, size, lines = cached
            else:
                size = p.stat().st_size
                lines = _count_lines(p.read_text(encoding="utf-8", errors="replace"))
                _BUILTIN_CACHE[str(p)] = (mtime, size, lines)
        except OSError:
            continue
        rel = os.path.relpath(p, PROJECT_ROOT).replace("\\", "/")
        out.append({
            "id": None, "name": p.name, "size": size, "lines": lines,
            "source": "builtin", "select_value": rel,
        })
    return out


@app.get("/api/corpora")
async def api_list_corpora():
    """内置语料（目录扫描）+ 上传语料（corpora 表）分组返回。"""
    uploaded = [
        {"id": r["id"], "name": r["name"], "size": r["size"], "lines": r["line_count"],
         "source": "uploaded", "select_value": f"uploaded:{r['id']}"}
        for r in db.list_corpora()
    ]
    return {"builtin": _scan_builtin_corpora(), "uploaded": uploaded}


@app.post("/api/corpora")
async def api_upload_corpus(file: UploadFile = File(...)):
    """上传 .txt 语料（P1-5：扩展名 / UTF-8 / 大小上限 / 同名 409）。"""
    name = (file.filename or "corpus.txt").strip()
    if not name.lower().endswith(".txt"):
        raise HTTPException(400, "仅支持 .txt 语料文件")
    data = await file.read()
    if len(data) > CORPORA_MAX_BYTES:
        raise HTTPException(413, f"语料超过大小上限（{CORPORA_MAX_BYTES // (1024 * 1024)}MB）")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "语料必须为 UTF-8 编码")
    if db.get_corpus_by_name(name):
        raise HTTPException(409, f"语料已存在（同名文件：{name}），可改名后重新上传或先删除旧文件")
    stored_name = uuid.uuid4().hex + ".txt"
    CORPORA_DIR.mkdir(parents=True, exist_ok=True)
    (CORPORA_DIR / stored_name).write_bytes(data)
    lines = _count_lines(text)
    row_id = db.create_corpus(name, stored_name, len(data), lines)
    return {"id": row_id, "name": name, "size": len(data), "lines": lines,
            "source": "uploaded", "select_value": f"uploaded:{row_id}"}


@app.delete("/api/corpora/{corpus_id}")
async def api_delete_corpus(corpus_id: int, force: bool = Query(default=False)):
    """删除上传语料（P1-5）：被任务引用时 409 并列出任务，除非 force=true 强制删。"""
    row = db.get_corpus(corpus_id)
    if not row:
        raise HTTPException(404, "语料不存在")
    refs = db.runs_referencing_corpus(row["stored_name"])
    if refs and not force:
        return JSONResponse(status_code=409, content={
            "error": f"语料被 {len(refs)} 个任务引用，删除将无法复核其历史结果；如确认请带 force=true 重试",
            "runs": refs,
        })
    # 仅删除 corpora 目录内的文件（stored_name 为服务端生成的 uuid；resolve 防穿越双保险）
    target = (CORPORA_DIR / row["stored_name"]).resolve()
    if str(target).startswith(str(CORPORA_DIR.resolve())):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.warning(f"删除语料文件失败: {target}")
    db.delete_corpus(corpus_id)
    return {"deleted": True, "id": corpus_id}


# ---- 对比页 ----

@app.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request, ids: list[str] = Query(default=[])):
    """多任务指标并排对比（V2.2 §4.6）。

    修复 #8：ids 声明为 list[str]，兼容 checkbox 表单的重复参数（?ids=1&ids=2）
    与逗号分隔（?ids=1,2）两种形态——FastAPI str 类型遇重复参数只保留末值，
    会导致勾选多个任务时只能解析出 1 个、对比页 UI 失效。
    """
    all_runs = db.list_test_runs(limit=100)
    raw_ids = ",".join(ids).strip()
    if not raw_ids:
        return templates.TemplateResponse("compare.html", {
            "request": request, "runs": [], "all_runs": all_runs, "error": None,
        })

    try:
        id_list = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]
    except ValueError:
        return templates.TemplateResponse("compare.html", {
            "request": request, "runs": [], "all_runs": all_runs,
            "error": "Invalid ids parameter",
        })

    runs = []
    for rid in id_list:
        run = db.get_test_run(rid)
        if not run:
            continue
        run = dict(run)
        # 解析 summary JSON 供模板使用
        doc = {}
        if run.get("summary_json"):
            try:
                doc = json.loads(run["summary_json"])
            except json.JSONDecodeError:
                pass
        run["_doc"] = doc
        run["_totals"] = doc.get("totals", {})
        run["_agg_metrics"] = doc.get("by_request_type", {}).get("Aggregated", {}).get("metrics", {})
        run["_by_type"] = doc.get("by_request_type", {})
        run["_fail_cats"] = doc.get("fail_categories", {})
        runs.append(run)

    return templates.TemplateResponse("compare.html", {
        "request": request, "runs": runs, "all_runs": all_runs, "error": None,
    })


# ---- LLM 诊断触发 ----

@app.post("/api/tasks/{run_id}/analysis")
async def trigger_analysis(run_id: int, engine: str = Query(default="rule")):
    """触发诊断（V2.2 §3.4 / §4.5）。

    engine=rule（默认）仅跑规则引擎；engine=llm 叠加 LLM 叙述层。
    返回 analysis_run_id。

    修复 #7：LLM 叙述层内部是同步 httpx.post(timeout=60)，必须放线程池执行，
    否则最长 60s 冻结整个单进程事件循环（所有页面/SSE/心跳全停）。
    """
    run = db.get_test_run(run_id)
    if not run or not run.get("summary_json"):
        raise HTTPException(400, "Task has no summary data")

    try:
        doc = json.loads(run["summary_json"])
    except json.JSONDecodeError:
        raise HTTPException(400, "Task summary JSON 已损坏，无法诊断")
    analysis_run_id = str(uuid.uuid4())

    # 规则引擎（必跑，纯计算、微秒级，保持同步）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from analyze_summary import run_rule_engine, load_thresholds
    thresholds = load_thresholds()
    findings = run_rule_engine(doc, thresholds)
    findings_json = json.dumps(findings, ensure_ascii=False)

    db.create_analysis(
        test_run_id=run_id,
        analysis_run_id=analysis_run_id,
        engine="rule",
        findings_json=findings_json,
        report_md=None,
        status="ok",
    )

    # LLM 叙述层（可选）：同步 httpx 调用放线程池，不阻塞事件循环（修复 #7）
    if engine == "llm":
        from analyze_summary import run_llm_analysis
        llm_report = await asyncio.to_thread(run_llm_analysis, doc, findings)
        llm_status = "ok" if not llm_report.startswith("[LLM") else "failed"
        db.create_analysis(
            test_run_id=run_id,
            analysis_run_id=analysis_run_id,
            engine="llm",
            findings_json=None,
            report_md=llm_report,
            status=llm_status,
        )

    return {"analysis_run_id": analysis_run_id, "engine": engine}


@app.get("/api/tasks/{run_id}/analysis")
async def get_analyses(run_id: int):
    """获取某任务的所有诊断结果。"""
    return db.get_analyses_for_test(run_id)


# ---- WEB_TOKEN 安全中间件 ----

WEB_TOKEN = os.environ.get("WEB_TOKEN", "")

# Jinja2 全局变量：所有模板自动获得 require_token（必须在 WEB_TOKEN 定义之后）
templates.env.globals["require_token"] = bool(WEB_TOKEN)

@app.middleware("http")
async def token_middleware(request: Request, call_next):
    """写操作需 Bearer token（SSE 端点用 ?token= 已在路由内校验）。

    V2.2 §4.8：GET 只读页面免 token，POST/PUT/DELETE 写操作必须带 Authorization: Bearer。
    WEB_TOKEN 未设置时跳过校验（开发模式）。
    """
    if not WEB_TOKEN:
        return await call_next(request)

    # SSE 端点已在路由内用 ?token= 校验，此处跳过
    if request.url.path.endswith("/events"):
        return await call_next(request)

    # GET/HEAD/OPTIONS 只读免校验
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)

    # 写操作校验 Bearer
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != WEB_TOKEN:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    return await call_next(request)


# ---- 启动入口 ----

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8686"))
    if WEB_TOKEN:
        print(f"[server] WEB_TOKEN is set, write APIs require Bearer auth")
    else:
        print(f"[server] WEB_TOKEN not set (dev mode, no auth)")
        # 安全提示：绑定到非 loopback 且无 token 时，写操作（提交压测/删任务/删语料）
        # 完全无鉴权，且 /results 产物目录只读开放——对外暴露必须设置 WEB_TOKEN。
        if host not in ("127.0.0.1", "localhost", "::1"):
            print("=" * 70)
            print("[server] 警告: 正在监听非本机地址且未设置 WEB_TOKEN —— 写接口无鉴权开放！")
            print("[server] 若需对外/局域网暴露，请在 .env 或系统环境变量设置 WEB_TOKEN")
            print("[server] （所有 POST/DELETE 写操作将要求 Authorization: Bearer <token>）")
            print("=" * 70)
    print(f"[server] Starting on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")

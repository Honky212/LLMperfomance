# -*- coding: utf-8 -*-
"""
Locust 多用户多轮对话压测脚本（Dify 风格 API 版）

请求 POST {LLM_API_URL}/v1/chat-messages（SSE 流式），通过 conversation_id 维持多轮会话。
- 地址/Key 从项目根目录 .env 读取（LLM_API_URL / API_KEY），命令行 --host 优先于 LLM_API_URL
- 语料默认读取 corpus/50QA-1.txt，可用环境变量 LOCUST_CORPUS 覆盖
- 逐用户记录问答明细到 results/locust/chat_responses_<uuid>.csv
  （含 TTFB / 整轮耗时 / 响应长度 / ITL / 生成速率 / 内容块数，G1 指标列）

用法:
  # Web 界面模式（默认 8089 端口，host 默认取 .env 的 LLM_API_URL）
  locust -f scripts/locust_multi_dialog.py
  # 无界面模式示例：20 用户、每秒启动 2 个、运行 2 分钟
  locust -f scripts/locust_multi_dialog.py --headless -u 20 -r 2 -t 2m
  # 显式指定 host（覆盖 .env）
  locust -f scripts/locust_multi_dialog.py --host http://115.25.86.121
  # 指定语料（等价于设置环境变量）
  $env:LOCUST_CORPUS = "corpus\\short10words.txt"
  locust -f scripts/locust_multi_dialog.py --headless -u 10 -r 2 -t 5m

耐力压测（找模型变慢/崩溃的临界点）:
  $env:LOCUST_WAIT_TIME = "0"
  locust -f scripts/locust_multi_dialog.py --headless -u 50 -r 5 -t 30m --html results/locust/report.html --csv results/locust/endurance

注意：
- 必须传 stream=True，否则 requests 会在返回前把整个 SSE 响应体读完，
  Locust 内置统计要等 with 块退出才上报（clients.py:391），导致生成期间图表完全不动
- stream=True 时 Locust 内置统计只计到响应头到达（≈首个 SSE 数据块到达），因此本脚本
  拆分为两组统计行：NewChat-TTFB / ContinueChat-TTFB（响应头到达耗时）与
  NewChat-Total / ContinueChat-Total（含流式消费的完整往返）。
  口径注意：Locust 报表中 -TTFB 行是"响应头到达"耗时，并非首真实回答内容时间；
  首真实回答内容口径的 TTFB 记录在逐用户 CSV 的 ttfb_ms 字段中
- 判断方法：观察统计中 -TTFB / -Total 的中位数和高分位是否随时间持续抬升（变慢），
  以及 # fails 是否开始增长（崩溃/超时）
"""
import os
import uuid
import csv
import time
from datetime import datetime

from locust import HttpUser, task, between, constant
import urllib3

from llmperf_common import config
from llmperf_common import stats as perf_stats
from llmperf_common import sse
from llmperf_common import progress
from llmperf_common.html_report import auto_post_run

# 配置加载与自检由公共模块统一；host 可来自命令行 --host，故此处仅加载与解析、不校验 LLM_API_URL
config.load_env()
VERIFY_SSL = config.parse_verify_ssl()
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 默认 host 取自 .env 的 LLM_API_URL（命令行 --host 优先）
_DEFAULT_HOST = os.environ.get("LLM_API_URL", "").strip()

# 语料文件（默认 50QA-1.txt，可用环境变量 LOCUST_CORPUS 覆盖）
CORPUS_FILE = os.environ.get("LOCUST_CORPUS", "") or os.path.join(config.PROJECT_ROOT, "corpus", "50QA-1.txt")

TURNS_PER_TASK = 3   # 每个多轮任务连续发送的对话轮数（首轮新建会话 + 后续 TURNS_PER_TASK-1 轮追问）
ANSWER_MIN_LEN = 10  # 响应完整性校验阈值（字符）


# ---- 压测调优参数（环境变量，压测耐力/找崩溃场景常用） ----
def _float_env(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        print(f"警告: {name} 非法，使用默认值 {default}")
        return default


def _build_wait_time():
    """LOCUST_WAIT_TIME: 用户思考时间(秒)；0 = 收到回复立即发下一条；缺省 1~3 秒随机"""
    raw = os.environ.get("LOCUST_WAIT_TIME", "").strip()
    if raw == "":
        return between(1, 3)
    try:
        return constant(max(0.0, float(raw)))
    except ValueError:
        print(f"警告: LOCUST_WAIT_TIME={raw} 非法，回退为默认 1~3 秒思考时间")
        return between(1, 3)


WAIT_TIME = _build_wait_time()
# LOCUST_TIMEOUT: 相邻响应数据包之间的超时秒数，超时计失败，用于发现模型挂起/卡死
REQUEST_TIMEOUT = _float_env("LOCUST_TIMEOUT", 300)

# 配置自检（快速失败，避免跑起来才发现全是 401）；host 可来自命令行，故不校验 LLM_API_URL
_api_key_check = os.environ.get("API_KEY", "").strip()
config.check_dify_key(_api_key_check)

# 加载时打印配置预览（Key 脱敏）
_k_masked = config.mask_key(_api_key_check)
print(f"[locust_multi_dialog] host={_DEFAULT_HOST or '需命令行 --host 指定'} | key={_k_masked} | "
      f"corpus={CORPUS_FILE} | turns_per_task={TURNS_PER_TASK} | timeout={REQUEST_TIMEOUT}s")

# V2.2 §3.2.2：模块级 StatsCollector 单例，Locust 多线程/协程用户共用（自带 Lock）
_COLLECTOR = perf_stats.StatsCollector()
_WALL_START = time.time()


class ChatUser(HttpUser):
    host = _DEFAULT_HOST or None  # 为 None 时命令行必须提供 --host
    wait_time = WAIT_TIME         # 思考时间，可用 LOCUST_WAIT_TIME=0 设为持续压测

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_id = str(uuid.uuid4())  # 生成唯一用户ID
        self.api_key = os.environ.get("API_KEY", "").strip()
        self.request_timeout = REQUEST_TIMEOUT
        self.conversation_id = ""
        self.test_prompt = self.read_test_prompt()
        self.q_index = 0     # 语料轮换游标

        # V2.2 修订 #1：逐用户 CSV 走 resolve_output_dir("locust")，裸跑仍为 results/locust/
        _locust_dir = config.resolve_output_dir("locust")
        os.makedirs(_locust_dir, exist_ok=True)
        self.csv_file = os.path.join(_locust_dir, f"chat_responses_{self.user_id}.csv")
        with open(self.csv_file, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["timestamp", "user_id", "conversation_id", "question",
                                    "answer", "ttfb_ms", "response_time_ms", "response_length",
                                    "itl_avg_ms", "itl_p95_ms", "gen_chars_per_sec", "chunk_count"])

    def read_test_prompt(self):
        """读取测试内容文件"""
        try:
            with open(CORPUS_FILE, "r", encoding="utf-8-sig") as f:
                return [line.strip() for line in f if line.strip()]
        except Exception as e:
            self.environment.events.request.fire(
                request_type="FILE", name="ReadPrompt", response_time=0, exception=e)
            return []

    def next_question(self):
        """轮换获取语料问题，避免所有用户同一时刻发送同一问题"""
        q = self.test_prompt[self.q_index % len(self.test_prompt)]
        self.q_index += 1
        return q

    @task(weight=3)
    def multi_round_chat(self):
        """多轮对话任务：在同一会话中连续发送 TURNS_PER_TASK 轮"""
        if not self.test_prompt:
            return

        # 第一轮：新建会话（无 conversation_id），问题取自轮换游标
        q = self.next_question()
        result, _ = self.chat(q, "", "NewChat")
        if not result:
            return  # 首轮失败拿不到 conversation_id，无法继续追问
        self.conversation_id = result["conversation_id"]
        self.save_to_csv(q, result)

        # 后续轮次：基于同一会话继续追问
        for _ in range(TURNS_PER_TASK - 1):
            q = self.next_question()
            result, _ = self.chat(q, self.conversation_id, "ContinueChat")
            if not result:
                break  # 单轮失败则放弃本次任务剩余轮次
            self.conversation_id = result["conversation_id"] or self.conversation_id
            self.save_to_csv(q, result)

    @task(weight=1)
    def init_conversation(self):
        """开启新会话任务：仅模拟"用户新开一个会话"的请求负载。
        此处产生的 conversation_id 不做保存——multi_round_chat 任务每次都会自行
        新建会话，保存它不会被任何后续逻辑消费（原有赋值为死代码，已移除）"""
        result, _ = self.chat("你是谁", "", "NewChat")
        if result:
            self.save_to_csv("你是谁", result)

    def chat(self, query, conversation_id, name):
        """发送一轮对话，返回 (result, elapsed_ms)；失败时 result 为 None"""
        payload = {
            "inputs": {},
            "query": query,
            "user": f"loadtest_{self.user_id}",
            "response_mode": "streaming"
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id

        start_time = time.time()

        # 注意：stream=True 时，Locust 内置统计的耗时在收到响应头时即定格，
        # 实际等于 TTFB，因此命名为 <name>-TTFB；
        # 完整往返耗时（含流式正文消费）由下方 <name>-Total 单独上报
        try:
            with self.client.post(
                    "/v1/chat-messages",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Accept": "text/event-stream"
                    },
                    stream=True,
                    catch_response=True,
                    name=f"{name}-TTFB",
                    timeout=self.request_timeout
            ) as response:
                result, sr = self.handle_stream_response(response, start_time, name)
        except Exception as e:
            # 请求建立阶段异常（response 尚未拿到，handle_stream_response 不会执行）：
            # 归类并上报失败（修订 A；勿重复包装流内异常——流内异常由解析器/既有 except 分支处理）
            sr = sse.failed_result(perf_stats.classify_exception(e), str(e)[:200],
                                   start_time=start_time)
            # V2.2 §3.2.2：连接阶段失败也进 StatsCollector
            _COLLECTOR.record(name, False, ttfb_ms=0.0, latency_ms=sr.elapsed_ms,
                              length=0, fail_category=sr.fail_category)
            progress.emit({"type": "round_done", "user_id": "locust", "round": 0,
                           "ok": False, "req_type": name, "ttfb_ms": 0.0,
                           "elapsed_ms": round(sr.elapsed_ms, 2)})
            result = None

        # 完整往返时间单独上报为一个统计项
        self.environment.events.request.fire(
            request_type="POST",
            name=f"{name}-Total",
            response_time=sr.elapsed_ms,
            response_length=len(result["answer"]) if result else 0,
            exception=None if result else Exception(sr.fail_reason or "unknown error"),
            context={},
        )
        return result, sr.elapsed_ms

    def handle_stream_response(self, response, start_time, name):
        """处理 Dify SSE 流式响应，返回 (result, sr)；失败时 result 为 None（P3 起由 sse 统一解析）

        name: 请求类型统计名（NewChat / ContinueChat，由 chat() 传入）。
        """
        if response.status_code != 200:
            # Locust safe-mode：连接失败返回 status_code=0 且带 .error 的 LocustResponse（不抛异常），
            # 需按其 error 归类为 connection_error/timeout；真实 HTTP 非 200 才归 http_error（修订 A 实证）
            err = getattr(response, "error", None)
            if err is not None:
                reason = f"Status {response.status_code}: {err}"
                response.failure(str(err)[:200])
                sr = sse.failed_result(perf_stats.classify_exception(err), reason,
                                       start_time=start_time)
            else:
                reason = f"Status {response.status_code}: {response.text[:200]}"
                response.failure(reason)
                sr = sse.failed_result("http_error", reason, start_time=start_time)
            # 修复 #11：HTTP 非 200 分支同样记 StatsCollector + 进度事件
            # （此前漏计，summary JSON / HTML 报告的失败分布看不到 http 层失败）
            _COLLECTOR.record(name, False, ttfb_ms=0.0, latency_ms=sr.elapsed_ms,
                              length=0, fail_category=sr.fail_category)
            progress.emit({"type": "round_done", "user_id": "locust", "round": 0,
                           "ok": False, "req_type": name, "ttfb_ms": 0.0,
                           "elapsed_ms": round(sr.elapsed_ms, 2)})
            return None, sr

        sr = sse.consume_dify_sse(response.iter_lines(), start_time=start_time,
                                  min_answer_len=ANSWER_MIN_LEN)
        if sr.fail_category is not None:
            response.failure(sr.fail_reason or sr.fail_category)
            # V2.2 §3.2.2：失败轮次也进 StatsCollector（ITL/gen 仅 ok=True 并入样本）
            _COLLECTOR.record(name, False, ttfb_ms=sr.ttfb_ms or 0.0,
                              latency_ms=sr.elapsed_ms, length=0,
                              fail_category=sr.fail_category)
            progress.emit({"type": "round_done", "user_id": "locust", "round": 0,
                           "ok": False, "req_type": name,
                           "ttfb_ms": round(sr.ttfb_ms or 0.0, 2),
                           "elapsed_ms": round(sr.elapsed_ms, 2)})
            return None, sr
        response.success()
        result = {"answer": sr.answer, "conversation_id": sr.conversation_id,
                  "ttfb_ms": sr.ttfb_ms, "elapsed_ms": sr.elapsed_ms,
                  "itl_ms": sr.itl_ms, "gen_chars_per_sec": sr.gen_chars_per_sec,
                  "chunk_count": sr.chunk_count}
        # V2.2 §3.2.2：成功轮次进 StatsCollector
        _COLLECTOR.record(name, True, ttfb_ms=sr.ttfb_ms or 0.0,
                          latency_ms=sr.elapsed_ms, length=len(sr.answer),
                          itl_ms=sr.itl_ms, gen_chars_per_sec=sr.gen_chars_per_sec)
        progress.emit({"type": "round_done", "user_id": "locust", "round": 0,
                       "ok": True, "req_type": name,
                       "ttfb_ms": round(sr.ttfb_ms or 0.0, 2),
                       "elapsed_ms": round(sr.elapsed_ms, 2)})
        return result, sr

    def save_to_csv(self, question, result):
        """保存响应到CSV文件（P4：含 ITL / 生成速率 / 内容块数 G1 指标列）"""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.csv_file, "a", newline="", encoding="utf-8-sig") as f:
                clean_question = question.replace("\n", " ").replace('"', "'")
                clean_answer = result["answer"].replace("\n", " ").replace('"', "'")
                ttfb = round(result["ttfb_ms"], 1) if result["ttfb_ms"] is not None else ""
                itl_avg, itl_p95 = sse.itl_summary(result.get("itl_ms") or [])
                gen = result.get("gen_chars_per_sec")
                csv.writer(f).writerow([timestamp, self.user_id, result["conversation_id"],
                                        clean_question, clean_answer, ttfb,
                                        round(result["elapsed_ms"], 1), len(result["answer"]),
                                        round(itl_avg, 1) if itl_avg is not None else "",
                                        round(itl_p95, 1) if itl_p95 is not None else "",
                                        round(gen, 1) if gen is not None else "",
                                        result.get("chunk_count", 0)])
        except Exception as e:
            self.environment.events.request.fire(
                request_type="CSV", name="SaveResponse", response_time=0, exception=e)

    def on_start(self):
        """用户启动时执行的初始化"""
        self.client.verify = VERIFY_SSL


# ---- V2.2 §3.2.2：test_stop 钩子写 summary JSON + progress 事件 ----

def _on_test_start(environment, **kwargs):
    """Locust test_start 时发出 progress.start（endurance 类型 expected_rounds=null）"""
    progress.emit({
        "type": "start",
        "script": "locust_multi_dialog.py",
        "protocol": "dify",
        "test_type": "endurance",
        "params": {"corpus": os.path.basename(CORPUS_FILE),
                   "turns_per_task": TURNS_PER_TASK},
        "expected_rounds": None,
    })


def _on_test_stop(environment, **kwargs):
    """Locust test_stop 时写出 summary JSON（受 LLMPERF_SUMMARY 控制）并发 progress.done"""
    wall_time = time.time() - _WALL_START
    output_dir = config.resolve_output_dir()
    summary_path = None
    if os.environ.get("LLMPERF_SUMMARY", "1").strip() != "0":
        host = environment.host or _DEFAULT_HOST or ""
        summary_path = perf_stats.save_report_json(
            output_dir,
            script="locust_multi_dialog.py",
            protocol="dify",
            endpoint=host.rstrip("/") + "/v1/chat-messages",
            model="",
            params={"corpus": os.path.basename(CORPUS_FILE),
                    "turns_per_task": TURNS_PER_TASK,
                    "locust_distributed": False},
            wall_time=wall_time,
            stats=_COLLECTOR,
        )
    # V2.2 §3.3 自动入口
    auto_post_run(summary_path)

    progress.emit({"type": "done", "summary_json": summary_path or ""})


# Locust 事件注册（模块加载时即绑定；headless / web 模式均生效）
from locust import events as _locust_events
_locust_events.test_start.add_listener(_on_test_start)
_locust_events.test_stop.add_listener(_on_test_stop)

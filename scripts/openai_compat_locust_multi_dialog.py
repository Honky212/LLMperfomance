# -*- coding: utf-8 -*-
"""
Locust 多用户多轮对话压测脚本（OpenAI 兼容 API 版）

本脚本是 locust_multi_dialog.py（Dify 版）的 OpenAI 兼容孪生版：
- Dify 版请求 /v1/chat-messages 并以 conversation_id 维持会话；
  本版本请求 /chat/completions（SSE 流式），以 messages 历史维持多轮上下文
- 地址/Key/模型从项目根目录 .env 读取（OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME），
  Locust 命令行 --host 参数优先于 OPENAI_BASE_URL
- 语料默认读取 corpus/test1.txt，可用环境变量 LOCUST_CORPUS 覆盖

用法:
  # Web 界面模式（默认 8089 端口，host 默认取 .env 的 OPENAI_BASE_URL）
  locust -f scripts/openai_compat_locust_multi_dialog.py
  # 无界面模式示例：20 用户、每秒启动 2 个、运行 2 分钟
  locust -f scripts/openai_compat_locust_multi_dialog.py --headless -u 20 -r 2 -t 2m
  # 显式指定 host（覆盖 .env）
  locust -f scripts/openai_compat_locust_multi_dialog.py --host http://172.18.10.3:3000/v1

耐力压测（找模型变慢/崩溃的临界点）:
  # 10 秒内拉起 50 用户，收到回复立即发下一条（零思考时间），跑 30 分钟并输出 HTML/CSV 报告
  $env:LOCUST_WAIT_TIME = "0"
  locust -f scripts/openai_compat_locust_multi_dialog.py --headless -u 50 -r 5 -t 30m --html results/locust/report.html --csv results/locust/endurance
  # 判断方法：观察周期性统计中 ChatCompletions-TTFB / ChatCompletions-Total 的中位数和
  # 高分位是否随时间持续抬升（变慢），以及 # fails 是否开始增长（崩溃/超时）

注意：
- stream=True 时 Locust 内置统计只计到响应头到达（≈首个 SSE 数据块到达），因此本脚本
  拆分为两个统计行：ChatCompletions-TTFB（响应头到达耗时）与 ChatCompletions-Total
  （含流式消费的完整往返）。口径注意：-TTFB 行并非首真实内容时间，首真实内容口径的
  TTFB 记录在逐用户 CSV 的 ttfb_ms 字段中
- 实测该模型单轮流式生成约 15~25 秒（回答 ~1200-2100 字符），单用户单位时间请求数
  偏低属正常现象，评估容量时请关注并发执行情况和总吞吐
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

# 配置加载与自检由公共模块统一；host 可来自命令行 --host，故此处仅加载与解析、不校验 OPENAI_BASE_URL
config.load_env()
VERIFY_SSL = config.parse_verify_ssl()
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 默认 host 取自 .env 的 OPENAI_BASE_URL（命令行 --host 优先；误填完整接口地址自动截取）
_DEFAULT_HOST = config.normalize_base_url(os.environ.get("OPENAI_BASE_URL", "").strip())

# 语料文件（默认 test1.txt，可用环境变量 LOCUST_CORPUS 覆盖）
CORPUS_FILE = os.environ.get("LOCUST_CORPUS", "") or os.path.join(config.PROJECT_ROOT, "corpus", "test1.txt")

TURNS_PER_TASK = 3   # 每个多轮任务连续发送的对话轮数
ANSWER_MIN_LEN = 10  # 响应完整性校验阈值（字符，与 Dify 版一致）


# ---- 压测调优参数（环境变量，压测耐力/找崩溃场景常用） ----
def _int_env(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        print(f"警告: {name} 非法，使用默认值 {default}")
        return default


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
# LOCUST_MAX_TURNS: 保留最近 N 轮历史(一轮=一问一答)，防止长跑上下文膨胀撑爆模型窗口；0 = 不限制
MAX_TURNS = _int_env("LOCUST_MAX_TURNS", 10)
# LOCUST_TIMEOUT: 相邻响应数据包之间的超时秒数，超时计失败，用于发现模型挂起/卡死
REQUEST_TIMEOUT = _float_env("LOCUST_TIMEOUT", 300)

# 配置自检（快速失败，避免跑起来才发现全是 401/404）；host 可来自命令行，故仅校验 Key 与模型
_api_key_check = os.environ.get("OPENAI_API_KEY", "").strip()
config.check_openai_key(_api_key_check)
config.check_model(os.environ.get("MODEL_NAME", "").strip())

# 加载时打印配置预览（Key 脱敏）
_k_masked = config.mask_key(_api_key_check)
print(f"[openai_compat_locust] host={_DEFAULT_HOST or '需命令行 --host 指定'} | "
      f"model={os.environ.get('MODEL_NAME', '')} | key={_k_masked} | corpus={CORPUS_FILE} | "
      f"max_turns={MAX_TURNS} | timeout={REQUEST_TIMEOUT}s")

# V2.2 §3.2.2：模块级 StatsCollector 单例
_COLLECTOR = perf_stats.StatsCollector()
_WALL_START = time.time()


class OpenAICompatChatUser(HttpUser):
    host = _DEFAULT_HOST or None  # 为 None 时命令行必须提供 --host
    wait_time = WAIT_TIME         # 思考时间，可用 LOCUST_WAIT_TIME=0 设为持续压测

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_id = str(uuid.uuid4())  # 生成唯一用户ID
        self.api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("MODEL_NAME", "").strip()
        try:
            self.max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "0") or 0)
        except ValueError:
            self.max_tokens = 0
        self.messages = []   # 多轮对话历史（替代 Dify 的 conversation_id）
        self.q_index = 0     # 语料轮换索引
        self.max_turns = MAX_TURNS
        self.request_timeout = REQUEST_TIMEOUT
        self.test_prompt = self.read_test_prompt()

        # V2.2 修订 #1：逐用户 CSV 走 resolve_output_dir("locust")
        _locust_dir = config.resolve_output_dir("locust")
        os.makedirs(_locust_dir, exist_ok=True)
        self.csv_file = os.path.join(_locust_dir, f"openai_chat_responses_{self.user_id}.csv")
        with open(self.csv_file, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["timestamp", "user_id", "question", "answer",
                                    "ttfb_ms", "response_time_ms", "response_length",
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
        """多轮对话任务：在同一上下文中连续发送 TURNS_PER_TASK 轮"""
        if not self.test_prompt:
            return
        for _ in range(TURNS_PER_TASK):
            if not self.chat_once(self.next_question()):
                break  # 单轮失败则放弃本次任务剩余轮次

    @task(weight=1)
    def init_conversation(self):
        """开启新会话：清空历史并从语料取题提问（OpenAI 协议无服务端会话，
        对应 Dify 版的"新建会话"）。

        修复 #12：原实现调 chat_once("") 被其 `if not question: return None`
        短路——该 weight=1 任务从不发请求、无统计、无 CSV，纯空转；现补真实请求。
        """
        self.messages = []
        if not self.test_prompt:
            return
        self.chat_once(self.next_question())

    def chat_once(self, question):
        """发送一轮对话，成功返回 {"answer", "ttfb_ms", "elapsed_ms"}，失败返回 None"""
        if not question:
            return None
        start_time = time.time()
        payload = {
            "model": self.model,
            "messages": self.messages + [{"role": "user", "content": question}],
            "stream": True,
        }
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens

        # 注意：stream=True 时，Locust 内置统计的耗时在收到响应头时即定格，
        # 实际等于 TTFB，因此命名为 ChatCompletions-TTFB；
        # 完整往返耗时（含流式正文消费）由下方 ChatCompletions-Total 单独上报
        try:
            with self.client.post(
                    "/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Accept": "text/event-stream"
                    },
                    stream=True,
                    catch_response=True,
                    name="ChatCompletions-TTFB",
                    timeout=self.request_timeout
            ) as response:
                result, sr = self.handle_stream_response(response, start_time)
        except Exception as e:
            # 请求建立阶段异常（response 尚未拿到，handle_stream_response 不会执行）：
            # 归类并上报失败（修订 A；勿重复包装流内异常——流内异常由解析器/既有 except 分支处理）
            sr = sse.failed_result(perf_stats.classify_exception(e), str(e)[:200],
                                   start_time=start_time)
            _COLLECTOR.record("ChatCompletions", False, ttfb_ms=0.0,
                              latency_ms=sr.elapsed_ms, length=0,
                              fail_category=sr.fail_category)
            progress.emit({"type": "round_done", "user_id": "locust", "round": 0,
                           "ok": False, "req_type": "ChatCompletions", "ttfb_ms": 0.0,
                           "elapsed_ms": round(sr.elapsed_ms, 2)})
            result = None

        # 完整往返时间单独上报为一个统计项
        self.environment.events.request.fire(
            request_type="POST",
            name="ChatCompletions-Total",
            response_time=sr.elapsed_ms,
            response_length=len(result["answer"]) if result else 0,
            exception=None if result else Exception(sr.fail_reason or "unknown error"),
            context={},
        )

        if result:
            # 本轮对话入历史，维持多轮上下文
            self.messages.append({"role": "user", "content": question})
            self.messages.append({"role": "assistant", "content": result["answer"]})
            # 历史裁剪：防止长时间耐力压测时上下文无限膨胀（0 = 不限制）
            if self.max_turns > 0 and len(self.messages) > self.max_turns * 2:
                self.messages = self.messages[-self.max_turns * 2:]
            self.save_to_csv(question, result)
        return result

    def handle_stream_response(self, response, start_time):
        """处理 OpenAI SSE 流式响应，返回 (result, sr)；失败时 result 为 None（P3 起由 sse 统一解析）"""
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
            _COLLECTOR.record("ChatCompletions", False, ttfb_ms=0.0,
                              latency_ms=sr.elapsed_ms, length=0,
                              fail_category=sr.fail_category)
            progress.emit({"type": "round_done", "user_id": "locust", "round": 0,
                           "ok": False, "req_type": "ChatCompletions", "ttfb_ms": 0.0,
                           "elapsed_ms": round(sr.elapsed_ms, 2)})
            return None, sr

        sr = sse.consume_openai_sse(response.iter_lines(), start_time=start_time,
                                    min_answer_len=ANSWER_MIN_LEN)
        if sr.fail_category is not None:
            response.failure(sr.fail_reason or sr.fail_category)
            _COLLECTOR.record("ChatCompletions", False, ttfb_ms=sr.ttfb_ms or 0.0,
                              latency_ms=sr.elapsed_ms, length=0,
                              fail_category=sr.fail_category)
            progress.emit({"type": "round_done", "user_id": "locust", "round": 0,
                           "ok": False, "req_type": "ChatCompletions",
                           "ttfb_ms": round(sr.ttfb_ms or 0.0, 2),
                           "elapsed_ms": round(sr.elapsed_ms, 2)})
            return None, sr
        response.success()
        result = {"answer": sr.answer, "ttfb_ms": sr.ttfb_ms, "elapsed_ms": sr.elapsed_ms,
                  "itl_ms": sr.itl_ms, "gen_chars_per_sec": sr.gen_chars_per_sec,
                  "chunk_count": sr.chunk_count}
        _COLLECTOR.record("ChatCompletions", True, ttfb_ms=sr.ttfb_ms or 0.0,
                          latency_ms=sr.elapsed_ms, length=len(sr.answer),
                          itl_ms=sr.itl_ms, gen_chars_per_sec=sr.gen_chars_per_sec)
        progress.emit({"type": "round_done", "user_id": "locust", "round": 0,
                       "ok": True, "req_type": "ChatCompletions",
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
                csv.writer(f).writerow([timestamp, self.user_id, clean_question, clean_answer,
                                        ttfb, round(result["elapsed_ms"], 1), len(result["answer"]),
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
    progress.emit({
        "type": "start",
        "script": "openai_compat_locust_multi_dialog.py",
        "protocol": "openai_compat",
        "test_type": "endurance",
        "params": {"corpus": os.path.basename(CORPUS_FILE),
                   "turns_per_task": TURNS_PER_TASK,
                   "model": os.environ.get("MODEL_NAME", "")},
        "expected_rounds": None,
    })


def _on_test_stop(environment, **kwargs):
    wall_time = time.time() - _WALL_START
    output_dir = config.resolve_output_dir()
    summary_path = None
    if os.environ.get("LLMPERF_SUMMARY", "1").strip() != "0":
        host = environment.host or _DEFAULT_HOST or ""
        summary_path = perf_stats.save_report_json(
            output_dir,
            script="openai_compat_locust_multi_dialog.py",
            protocol="openai_compat",
            endpoint=config.normalize_base_url(host).rstrip("/") + "/chat/completions",
            model=os.environ.get("MODEL_NAME", ""),
            params={"corpus": os.path.basename(CORPUS_FILE),
                    "turns_per_task": TURNS_PER_TASK,
                    "max_turns": MAX_TURNS,
                    "locust_distributed": False},
            wall_time=wall_time,
            stats=_COLLECTOR,
        )
    # V2.2 §3.3 自动入口
    auto_post_run(summary_path)

    progress.emit({"type": "done", "summary_json": summary_path or ""})


from locust import events as _locust_events
_locust_events.test_start.add_listener(_on_test_start)
_locust_events.test_stop.add_listener(_on_test_stop)
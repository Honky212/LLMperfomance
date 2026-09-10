# -*- coding: utf-8 -*-
"""
多线程多轮对话压测脚本（Dify 风格 API 版）

请求 POST {LLM_API_URL}/v1/chat-messages，通过 conversation_id 维持多轮会话；
本脚本是 openai_compat_concurrent.py 的 Dify 风格孪生版：
- 线程池并发模拟多个用户，每个用户按语料顺序进行多轮对话，
  每用户结果写入 results/chat_responses_{user_id}.csv
  （TTFB / 整轮耗时 / 响应长度 / ITL / 生成速率 / 内容块数，G1 指标列）
- 结束打印 Locust 风格汇总表（按请求类型 NewChat / ContinueChat / Aggregated：
  # reqs / # fails / Median / Avg / Min / p90 / p95 / p99 / Max / RPS），
  并追加写入项目根目录 压测汇总.md、输出机器可读 results/summary_dify_*.json（G3）
- 配置从项目根目录 .env 读取（LLM_API_URL / API_KEY），已存在的系统环境变量优先于 .env

用法:
  python scripts/dify_concurrent.py
  python scripts/dify_concurrent.py --users 20 --threads 10 --rounds 10
  python scripts/dify_concurrent.py --corpus corpus/50QA-2.txt --sleep 0
"""
import os
import csv
import time
import queue
import argparse
import threading
from datetime import datetime

import requests
import urllib3

from llmperf_common import config
from llmperf_common import stats as perf_stats
from llmperf_common import sse
from llmperf_common import progress
from llmperf_common.html_report import auto_post_run

# 配置加载与自检（LLM_API_URL / API_KEY）由公共模块统一，模块加载时快速失败
config.load_env()
VERIFY_SSL = config.parse_verify_ssl()
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_CFG = config.require_dify_config()
API_URL = _CFG["url"]
DEFAULT_API_KEY = _CFG["key"]

# 请求类型名称（与 llmperf_common.stats 统一，按请求类型分组统计，与 Locust 一致）
REQ_NEW = perf_stats.REQ_NEW            # 新建会话
REQ_CONTINUE = perf_stats.REQ_CONTINUE  # 多轮追问

ANSWER_MIN_LEN = 10  # 响应完整性校验阈值（字符，与 Locust 版一致）


class ChatUser:
    """模拟单个用户：按语料顺序进行多轮对话（以 conversation_id 维持会话）"""

    def __init__(self, user_id, api_key, questions, think_time, timeout, stats,
                 output_dir=None):
        # V2.2 修订 #1：默认输出目录走 resolve_output_dir()，裸跑仍为 results/
        if output_dir is None:
            output_dir = config.resolve_output_dir()
        self.user_id = user_id
        self.api_key = api_key
        self.questions = questions
        self.think_time = think_time
        self.timeout = timeout
        self.stats = stats
        self.conversation_id = ""
        self.session = requests.Session()
        self.session.verify = VERIFY_SSL
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "text/event-stream"
        })

        # 确保输出目录存在
        os.makedirs(output_dir, exist_ok=True)
        self.csv_file = os.path.join(output_dir, f"chat_responses_{self.user_id}.csv")

        # 创建CSV文件并写入表头（P4：追加 G1 指标列）
        with open(self.csv_file, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["timestamp", "user_id", "conversation_id", "question",
                                    "answer", "ttfb_ms", "response_time_ms", "response_length",
                                    "itl_avg_ms", "itl_p95_ms", "gen_chars_per_sec", "chunk_count"])

    def save_to_csv(self, question, result, sr):
        """将响应保存到CSV文件（P4：含 ITL / 生成速率 / 内容块数 G1 指标列）"""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.csv_file, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                clean_question = question.replace("\n", " ").replace('"', "'")
                clean_answer = result["answer"].replace("\n", " ").replace('"', "'")
                itl_avg, itl_p95 = sse.itl_summary(sr.itl_ms)
                writer.writerow([
                    timestamp,
                    self.user_id,
                    result["conversation_id"],
                    clean_question,
                    clean_answer,
                    round(sr.ttfb_ms, 1) if sr.ttfb_ms is not None else "",
                    round(sr.elapsed_ms, 1),
                    len(result["answer"]),
                    round(itl_avg, 1) if itl_avg is not None else "",
                    round(itl_p95, 1) if itl_p95 is not None else "",
                    round(sr.gen_chars_per_sec, 1) if sr.gen_chars_per_sec is not None else "",
                    sr.chunk_count,
                ])
        except Exception as e:
            print(f"[{self.user_id}] 保存CSV失败: {str(e)}")

    def start_new_chat(self, query):
        """发起新会话，返回 (result, sr)；失败时 result 为 None，sr 含失败分类"""
        payload = {
            "inputs": {},
            "query": query,
            "user": f"loadtest_{self.user_id}",
            "response_mode": "streaming"
        }
        return self._post(payload)

    def continue_chat(self, query, conversation_id):
        """继续已有会话，返回 (result, sr)；失败时 result 为 None，sr 含失败分类"""
        payload = {
            "inputs": {},
            "query": query,
            "user": f"loadtest_{self.user_id}",
            "conversation_id": conversation_id,
            "response_mode": "streaming"
        }
        return self._post(payload)

    def _post(self, payload):
        """发送 POST 请求并处理流式响应；返回 (result, sr)；失败时 result 为 None"""
        start_time = time.time()
        try:
            response = self.session.post(f"{API_URL}/v1/chat-messages", json=payload,
                                         stream=True, timeout=self.timeout)
            if response.status_code != 200:
                print(f"[{self.user_id}] 请求失败: {response.status_code} - {response.text[:200]}")
                return None, sse.failed_result("http_error",
                                               f"Status {response.status_code}: {response.text[:200]}",
                                               start_time=start_time)
            return self.handle_stream_response(response, start_time)
        except Exception as e:
            print(f"[{self.user_id}] 请求异常: {str(e)}")
            return None, sse.failed_result(perf_stats.classify_exception(e), str(e)[:200],
                                           start_time=start_time)

    def handle_stream_response(self, response, start_time):
        """处理流式响应，返回 (result, sr)；失败时 result 为 None（P3 起由 sse 统一解析）"""
        sr = sse.consume_dify_sse(response.iter_lines(), start_time=start_time,
                                  min_answer_len=ANSWER_MIN_LEN)
        if sr.fail_category is not None:
            print(f"[{self.user_id}] 请求失败[{sr.fail_category}]: {sr.fail_reason or ''}")
            return None, sr
        return {"answer": sr.answer, "conversation_id": sr.conversation_id}, sr

    def multi_round_chat(self):
        """执行多轮对话：第一轮新建会话，其余轮次基于同一会话追问"""
        questions = self.questions
        if not questions:
            return

        # 第一轮：新建会话
        result, sr = self.start_new_chat(questions[0])
        ok = result is not None
        self.stats.record(REQ_NEW, ok, ttfb_ms=sr.ttfb_ms or 0.0, latency_ms=sr.elapsed_ms,
                          length=len(result["answer"]) if result else 0,
                          itl_ms=sr.itl_ms, gen_chars_per_sec=sr.gen_chars_per_sec,
                          fail_category=sr.fail_category)
        progress.emit({"type": "round_done", "user_id": self.user_id, "round": 1,
                       "ok": ok, "req_type": REQ_NEW,
                       "ttfb_ms": round(sr.ttfb_ms or 0.0, 2),
                       "elapsed_ms": round(sr.elapsed_ms, 2)})
        if not ok:
            return  # 首轮失败拿不到 conversation_id，无法继续多轮追问
        self.conversation_id = result["conversation_id"]
        self.save_to_csv(questions[0], result, sr)

        # 后续轮次：基于同一会话继续追问
        for i, q in enumerate(questions[1:]):
            result, sr = self.continue_chat(q, self.conversation_id)
            ok = result is not None
            self.stats.record(REQ_CONTINUE, ok, ttfb_ms=sr.ttfb_ms or 0.0, latency_ms=sr.elapsed_ms,
                              length=len(result["answer"]) if result else 0,
                              itl_ms=sr.itl_ms, gen_chars_per_sec=sr.gen_chars_per_sec,
                              fail_category=sr.fail_category)
            progress.emit({"type": "round_done", "user_id": self.user_id, "round": i + 2,
                           "ok": ok, "req_type": REQ_CONTINUE,
                           "ttfb_ms": round(sr.ttfb_ms or 0.0, 2),
                           "elapsed_ms": round(sr.elapsed_ms, 2)})
            if ok:
                self.conversation_id = result["conversation_id"] or self.conversation_id
                self.save_to_csv(q, result, sr)
            # 模拟用户思考时间（最后一轮不再等待）
            if self.think_time > 0 and i < len(questions) - 2:
                time.sleep(self.think_time)

    def run(self):
        """执行用户测试流程"""
        try:
            self.multi_round_chat()
        finally:
            self.session.close()  # 释放连接池，避免长跑时连接泄漏


def read_questions(file_path):
    """读取语料文件，每行一个问题"""
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"读取语料文件失败: {str(e)}")
        return []


def worker(user_queue, api_key, questions, rounds, think_time, timeout, stats):
    """线程工作函数：从队列取用户并执行其多轮对话"""
    while True:
        try:
            user_id = user_queue.get_nowait()
        except queue.Empty:
            break
        print(f"启动用户: {user_id}")
        user = ChatUser(user_id, api_key, questions[:rounds], think_time, timeout, stats)
        user.run()
        print(f"完成用户: {user_id}")



def parse_args():
    parser = argparse.ArgumentParser(description="Dify 风格 API 多线程多轮对话压测")
    parser.add_argument("--users", type=int, default=5, help="模拟用户总数(默认5)")
    parser.add_argument("--threads", type=int, default=5, help="并发线程数(默认5)")
    parser.add_argument("--rounds", type=int, default=10, help="每个用户对话轮数，即取语料前N条(默认10)")
    parser.add_argument("--corpus", default=os.path.join(config.PROJECT_ROOT, "corpus", "test1.txt"),
                        help="语料文件路径，每行一个问题(默认 corpus/test1.txt)")
    parser.add_argument("--sleep", type=float, default=1.0, help="轮次间思考时间秒，0表示持续压测(默认1.0)")
    parser.add_argument("--timeout", type=float, default=120.0, help="单请求超时秒(默认120)")
    parser.add_argument("--report", default=os.path.join(config.PROJECT_ROOT, "压测汇总.md"),
                        help="汇总报告输出的 Markdown 文件(默认 项目根目录/压测汇总.md)")
    return parser.parse_args()


def main():
    args = parse_args()

    # 配置自检已在模块加载时由 config.require_dify_config() 完成（快速失败）
    api_key = DEFAULT_API_KEY

    questions = read_questions(args.corpus)
    if not questions:
        raise SystemExit("错误: 语料为空，无法压测")
    rounds = min(args.rounds, len(questions))

    # 启动时打印当前生效配置（Key 脱敏），便于排查配置来源问题
    _k_masked = config.mask_key(api_key)
    print(f"当前配置: api_url={API_URL} | key={_k_masked}")
    print(f"压测参数: 用户数={args.users} 线程数={args.threads} 每用户轮数={rounds} "
          f"思考间隔={args.sleep}s 语料={args.corpus}")

    stats = perf_stats.StatsCollector()
    user_queue = queue.Queue()
    for i in range(args.users):
        user_queue.put(f"user_{i}")

    # V2.2 §4.4：progress.start（concurrent 类型）
    progress.emit({
        "type": "start",
        "script": os.path.basename(__file__),
        "protocol": "dify",
        "test_type": "concurrent",
        "params": {"users": args.users, "threads": args.threads,
                   "rounds": rounds, "corpus": os.path.basename(args.corpus)},
        "expected_rounds": args.users * rounds,
    })

    wall_start = time.time()
    threads = []
    for _ in range(max(1, min(args.threads, args.users))):
        t = threading.Thread(target=worker,
                             args=(user_queue, api_key, questions, rounds, args.sleep, args.timeout, stats))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    wall_time = time.time() - wall_start

    print("所有用户测试完成")
    stats.print_summary(wall_time, detail="results/chat_responses_*.csv")

    # G3：机器可读 JSON + 追加式 Markdown 汇总（人机两份数据互为索引）
    params = {
        "users": args.users, "threads": args.threads, "rounds": rounds,
        "sleep_s": args.sleep, "timeout_s": args.timeout, "corpus": args.corpus,
    }
    # V2.2 修订 #1：summary JSON / MD 输出目录走 resolve_output_dir()
    output_dir = config.resolve_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    json_path = None
    if os.environ.get("LLMPERF_SUMMARY", "1").strip() != "0":
        json_path = perf_stats.save_report_json(
            output_dir,
            script=os.path.basename(__file__), protocol="dify", endpoint=API_URL,
            model="", params=params, wall_time=wall_time, stats=stats)
    perf_stats.save_report_md(
        args.report, script=os.path.basename(__file__), protocol="dify",
        endpoint=API_URL, model="", params=params, wall_time=wall_time,
        stats=stats, json_path=json_path or "")
    print(f"压测汇总已追加到: {args.report}")

    # V2.2 §3.3 自动入口
    auto_post_run(json_path)

    progress.emit({"type": "done", "summary_json": json_path or ""})


if __name__ == "__main__":
    main()


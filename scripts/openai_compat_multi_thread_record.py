# -*- coding: utf-8 -*-
"""
多线程多轮对话压测脚本（OpenAI 兼容 API 版）

本脚本是 multi_thread_record.py（Dify 版）的 OpenAI 兼容孪生版：
- Dify 版通过 conversation_id 维持多轮会话；本版本按 OpenAI 标准以 messages 历史维持上下文
- API 地址/Key/模型名从项目根目录 .env 读取（OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME），
  已存在的系统环境变量优先于 .env
- 线程池并发模拟多个用户，每个用户按语料顺序进行多轮对话，
  每用户结果写入 results/openai_chat_responses_{user_id}.csv
  （TTFB / 整轮耗时 / 响应长度 / ITL / 生成速率 / 内容块数，G1 指标列），
  结束打印与 Dify 版一致的 Locust 风格分组汇总表（NewChat / ContinueChat / Aggregated），
  并追加写入项目根目录 压测汇总.md、输出机器可读 results/summary_openai_*.json（G3）

用法:
  python scripts/openai_compat_multi_thread_record.py
  python scripts/openai_compat_multi_thread_record.py --users 20 --threads 10 --rounds 10
  python scripts/openai_compat_multi_thread_record.py --corpus corpus/test2.txt --sleep 0

说明:
  --rounds 表示“每个用户最多执行几轮”，但会被语料长度限制：
  实际执行轮数 = min(args.rounds, len(questions))。
  因此当语料总数少于 --rounds 时，脚本会自动按实际可用问题数执行，
  这意味着该参数不是强制要求“必须跑满 N 轮”，也可以不传，
  直接使用默认值 10 或按语料长度自动收敛。
"""
import os
import csv
import time
import queue
import argparse
import threading
from datetime import datetime

import httpx
from openai import OpenAI

from llmperf_common import config
from llmperf_common import stats as perf_stats
from llmperf_common import sse
from llmperf_common import progress
from llmperf_common.html_report import auto_post_run

ANSWER_MIN_LEN = 10  # 响应完整性校验阈值（字符，与其他脚本保持一致）

# 配置加载与自检（OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME）由公共模块统一
config.load_env()
VERIFY_SSL = config.parse_verify_ssl()


def get_config():
    """配置聚合（含快速失败自检）；url 已做 /chat/completions 后缀归一"""
    _c = config.require_openai_config()
    return {"api.url": _c["url"], "api.key": _c["key"], "model": _c["model"]}


class ChatUser:
    """模拟单个用户：按语料顺序进行多轮对话（以 messages 历史维持上下文）"""

    def __init__(self, user_id, client, model, questions, think_time, stats,
                 output_dir=None):
        self.user_id = user_id
        self.client = client
        self.model = model
        self.questions = questions
        self.think_time = think_time
        self.stats = stats
        self.messages = []  # 多轮对话历史

        # V2.2 修订 #1：默认输出目录走 resolve_output_dir()
        if output_dir is None:
            output_dir = config.resolve_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        self.csv_file = os.path.join(output_dir, f"openai_chat_responses_{self.user_id}.csv")
        with open(self.csv_file, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["timestamp", "user_id", "question", "answer",
                                    "ttfb_ms", "response_time_ms", "response_length",
                                    "itl_avg_ms", "itl_p95_ms", "gen_chars_per_sec", "chunk_count"])

    def save_to_csv(self, question, answer, sr):
        """保存响应到CSV（P4：含 ITL / 生成速率 / 内容块数 G1 指标列）"""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.csv_file, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                clean_question = question.replace("\n", " ").replace('"', "'")
                clean_answer = answer.replace("\n", " ").replace('"', "'")
                itl_avg, itl_p95 = sse.itl_summary(sr.itl_ms)
                writer.writerow([timestamp, self.user_id, clean_question, clean_answer,
                                 round(sr.ttfb_ms or 0.0, 1), round(sr.elapsed_ms, 1), len(answer),
                                 round(itl_avg, 1) if itl_avg is not None else "",
                                 round(itl_p95, 1) if itl_p95 is not None else "",
                                 round(sr.gen_chars_per_sec, 1) if sr.gen_chars_per_sec is not None else "",
                                 sr.chunk_count])
        except Exception as e:
            print(f"[{self.user_id}] 保存CSV失败: {str(e)}")

    def chat_once(self, question):
        """发送一轮对话，返回 (answer, sr)；失败时 answer 为 None（P3 起指标由 StreamResult 承载）"""
        self.messages.append({"role": "user", "content": question})
        start_time = time.time()
        timer = sse.StreamTimer(start_time=start_time)  # 必须在 create() 之前构造（异常分支也要用）
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                stream=True,
            )
            # P3：chunk 循环收敛到 sse.StreamTimer + extract_openai_delta（TTFB / ITL / 生成速率）
            parts = []
            for chunk in stream:
                delta = sse.extract_openai_delta(chunk)
                if delta:
                    timer.on_content()
                    parts.append(delta)
            answer = "".join(parts)
            # 验证响应完整性（低于阈值计为失败，与其他脚本保持一致，
            # 同时避免空响应时 ttfb=None 进入统计导致汇总打印崩溃）
            if len(answer) < ANSWER_MIN_LEN:
                self.messages.pop()  # 回滚本轮 user 消息，避免污染后续上下文
                print(f"[{self.user_id}] 响应不完整（长度 {len(answer)} < {ANSWER_MIN_LEN}）")
                return None, timer.result(answer, fail_category="incomplete_answer",
                                          fail_reason=f"长度 {len(answer)} < {ANSWER_MIN_LEN}")
            # 助手回复入历史，维持下一轮上下文
            self.messages.append({"role": "assistant", "content": answer})
            return answer, timer.result(answer)
        except Exception as e:
            # 失败时回滚本轮 user 消息，避免污染后续上下文；异常按类名归类（G3/5.1）
            self.messages.pop()
            print(f"[{self.user_id}] 请求失败: {str(e)[:200]}")
            cat = perf_stats.classify_exception(e)
            return None, timer.result("", fail_category=cat, fail_reason=str(e)[:200])

    def run(self):
        for i, q in enumerate(self.questions):
            answer, sr = self.chat_once(q)
            # 分组口径（G3/5.2）：首轮（无历史）→ NewChat，携带历史轮 → ContinueChat
            group = perf_stats.REQ_NEW if i == 0 else perf_stats.REQ_CONTINUE
            ok = answer is not None
            self.stats.record(group, ok, ttfb_ms=sr.ttfb_ms or 0.0,
                              latency_ms=sr.elapsed_ms,
                              length=len(answer) if answer else 0,
                              itl_ms=sr.itl_ms, gen_chars_per_sec=sr.gen_chars_per_sec,
                              fail_category=sr.fail_category)
            progress.emit({"type": "round_done", "user_id": self.user_id, "round": i + 1,
                           "ok": ok, "req_type": group,
                           "ttfb_ms": round(sr.ttfb_ms or 0.0, 2),
                           "elapsed_ms": round(sr.elapsed_ms, 2)})
            if ok:
                self.save_to_csv(q, answer, sr)
            # 模拟用户思考时间（最后一轮不再等待）
            if self.think_time > 0 and i < len(self.questions) - 1:
                time.sleep(self.think_time)

def read_questions(file_path):
    """读取语料文件，每行一个问题"""
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"读取语料文件失败: {str(e)}")
        return []


def worker(user_queue, client, model, questions, rounds, think_time, stats):
    """线程工作函数：从队列取用户并执行其多轮对话"""
    while True:
        try:
            user_id = user_queue.get_nowait()
        except queue.Empty:
            break
        print(f"启动用户: {user_id}")
        user = ChatUser(user_id, client, model, questions[:rounds], think_time, stats)
        user.run()
        print(f"完成用户: {user_id}")


def parse_args():
    parser = argparse.ArgumentParser(description="OpenAI 兼容 API 多线程多轮对话压测")
    parser.add_argument("--users", type=int, default=5, help="模拟用户总数(默认5)")
    parser.add_argument("--threads", type=int, default=5, help="并发线程数(默认5)")
    parser.add_argument("--rounds", type=int, default=10, help="每个用户对话轮数，即取语料前N条(默认10)")
    parser.add_argument("--corpus", default=os.path.join(config.PROJECT_ROOT, "corpus", "test1.txt"),
                        help="语料文件路径，每行一个问题(默认 corpus/test1.txt)")
    parser.add_argument("--sleep", type=float, default=1.0, help="轮次间思考时间秒，0表示持续压测(默认1.0)")
    parser.add_argument("--timeout", type=float, default=600.0, help="单请求超时秒(默认600.0)")
    parser.add_argument("--report", default=os.path.join(config.PROJECT_ROOT, "压测汇总.md"),
                        help="汇总报告输出的 Markdown 文件(默认 项目根目录/压测汇总.md)")
    return parser.parse_args()


def main():
    args = parse_args()
    # 配置自检（OPENAI_API_KEY / MODEL_NAME 等）已由 get_config → require_openai_config 完成
    cfg = get_config()

    questions = read_questions(args.corpus)
    if not questions:
        raise SystemExit("错误: 语料为空，无法压测")
    rounds = min(args.rounds, len(questions))

    # 启动时打印当前生效配置（Key 脱敏），便于排查配置来源问题
    _k_masked = config.mask_key(cfg["api.key"])
    print(f"当前配置: base_url={cfg['api.url']} | model={cfg['model']} | key={_k_masked}")
    print(f"压测参数: 用户数={args.users} 线程数={args.threads} 每用户轮数={rounds} "
          f"思考间隔={args.sleep}s 语料={args.corpus}")

    client = OpenAI(
        base_url=cfg["api.url"],
        api_key=cfg["api.key"] or "EMPTY",  # 部分本地服务不校验密钥，但 SDK 要求非空
        timeout=args.timeout,
        http_client=httpx.Client(verify=VERIFY_SSL),
    )

    stats = perf_stats.StatsCollector()
    user_queue = queue.Queue()
    for i in range(args.users):
        user_queue.put(f"user_{i}")

    # V2.2 §4.4：progress.start（concurrent 类型）
    progress.emit({
        "type": "start",
        "script": os.path.basename(__file__),
        "protocol": "openai_compat",
        "test_type": "concurrent",
        "params": {"users": args.users, "threads": args.threads,
                   "rounds": rounds, "corpus": os.path.basename(args.corpus),
                   "model": cfg["model"]},
        "expected_rounds": args.users * rounds,
    })

    wall_start = time.time()
    threads = []
    for _ in range(max(1, min(args.threads, args.users))):
        t = threading.Thread(target=worker,
                             args=(user_queue, client, cfg["model"], questions, rounds, args.sleep, stats))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    wall_time = time.time() - wall_start

    print("所有用户测试完成")
    stats.print_summary(wall_time, detail="results/openai_chat_responses_*.csv")

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
            script=os.path.basename(__file__), protocol="openai_compat", endpoint=cfg["api.url"],
            model=cfg["model"], params=params, wall_time=wall_time, stats=stats)
    perf_stats.save_report_md(
        args.report, script=os.path.basename(__file__), protocol="openai_compat",
        endpoint=cfg["api.url"], model=cfg["model"], params=params, wall_time=wall_time,
        stats=stats, json_path=json_path or "")
    print(f"压测汇总已追加到: {args.report}")

    # V2.2 §3.3 自动入口
    auto_post_run(json_path)

    progress.emit({"type": "done", "summary_json": json_path or ""})


if __name__ == "__main__":
    main()
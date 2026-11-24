# -*- coding: utf-8 -*-
"""自定义智能体接口（Cookie 鉴权 / 非流式 JSON）多用户并发压测

适用接口（docs/自定义智能体接口.txt）：
    POST {url}
    Content-Type: application/json
    Cookie: JSESSIONID=...
    {"message": "..."}
响应：
    {"tableData": [], "paperData": [], "text": "..."}

与项目内 Dify / OpenAI 兼容脚本的差异：
- 鉴权是浏览器登录态 Cookie（会过期），不是 API Key；
- 响应非流式，无法测真实 TTFB，TTFB 记为整轮耗时（报告中标明口径）；
- 服务端通常按 Cookie/会话保存上下文，多个模拟用户共享同一个 Cookie 时
  上下文会互相污染；真实多用户压测请为每个用户准备独立登录会话。

用法：
    # 1 用户冒烟（等价基线）
    python scripts/custom_agent_concurrent.py --users 1 --threads 1 --rounds 3 \
        --cookie "JSESSIONID=..." --corpus corpus/test1.txt

    # 多 Cookie 真实多用户（cookie 文件每行一个，支持 "别名<TAB>Cookie"）
    python scripts/custom_agent_concurrent.py --users 10 --threads 10 --rounds 5 \
        --cookie-file cookies.txt --sleep 0

Cookie 也可来自环境变量：CUSTOM_AGENT_COOKIE / CUSTOM_AGENT_COOKIE_FILE；
接口地址必须通过 --url 或环境变量 CUSTOM_AGENT_URL 提供（仓库不内置真实地址）。
"""
import argparse
import csv
import os
import queue
import re
import threading
import time
from datetime import datetime

import requests

from llmperf_common import config
from llmperf_common import progress
from llmperf_common import stats as perf_stats
from llmperf_common.html_report import auto_post_run

config.load_env()

# 接口地址不在仓库内硬编码：从 .env / 系统环境变量 CUSTOM_AGENT_URL 读取，
# 命令行 --url 优先；两者都未提供时 main() 给出明确报错。
DEFAULT_URL = os.environ.get("CUSTOM_AGENT_URL", "").strip()
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _safe_name(name: str) -> str:
    """文件名安全化（用户别名可能含中文/空格）。"""
    return re.sub(r"[^\w.-]+", "_", str(name)).strip("_") or "user"


def mask_cookie(cookie: str) -> str:
    """日志脱敏：短值全隐藏，长值沿用 config.mask_key（前 6 + 后 4）。"""
    parts = []
    for pair in cookie.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
            parts.append(f"{k}={config.mask_key(v) if len(v) >= 10 else '****'}")
        else:
            parts.append(pair)
    return "; ".join(parts)


def normalize_cookie(raw: str) -> str:
    """去掉误粘贴的 'Cookie:' 前缀与换行，保留完整键值串。"""
    s = (raw or "").strip()
    if s.lower().startswith("cookie:"):
        s = s.split(":", 1)[1].strip()
    return s.replace("\r", " ").replace("\n", " ").strip()


def load_cookie_file(path: str) -> list:
    """读取 Cookie 文件，返回 [(别名, Cookie)]。

    每行一个 Cookie；支持 "别名<TAB>JSESSIONID=..." 或 "别名 JSESSIONID=..."；
    # 开头为注释。整行只写 Cookie 时自动命名 user_<行号>。
    """
    items = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for idx, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                tag, raw = line.split("\t", 1)
            elif " " in line and "=" not in line.split(None, 1)[0]:
                # "别名 JSESSIONID=..."（首段不是 key=value 才视为别名，
                # 避免把 "SESSION=x; JSESSIONID=y" 这类多 Cookie 行拆坏）
                tag, raw = line.split(None, 1)
            else:
                tag, raw = f"user_{idx}", line

            cookie = normalize_cookie(raw)
            if not cookie or "=" not in cookie:
                raise SystemExit(f"错误: Cookie 文件第 {idx} 行不是合法 Cookie: {line[:60]}")
            items.append((tag.strip().rstrip(":") or f"user_{idx}", cookie))
    if not items:
        raise SystemExit(f"错误: Cookie 文件为空: {path}")
    return items


def _fail(elapsed_ms: float, category: str, reason: str, status=None) -> dict:
    return {
        "ok": False, "answer": "", "ttfb_ms": elapsed_ms, "elapsed_ms": elapsed_ms,
        "http_status": status, "length": 0, "tables": 0, "papers": 0,
        "fail_category": category, "fail_reason": reason[:300],
    }


def chat_once(session: requests.Session, url: str, question: str, *,
              message_field: str, answer_field: str, timeout: float,
              verify: bool, min_answer_len: int) -> dict:
    """发送一轮非流式请求，返回统一结果（不抛异常）。

    非流式接口拿不到首 token 时刻，按项目统计口径把 ttfb_ms 记为整轮耗时；
    报告中同时带 streaming=False 标注，避免被误读为真实 TTFB。
    """
    start = time.perf_counter()
    try:
        resp = session.post(
            url,
            json={message_field: question},
            timeout=(min(10.0, timeout), timeout),
            verify=verify,
        )
    except requests.exceptions.RequestException as e:
        elapsed = (time.perf_counter() - start) * 1000
        return _fail(elapsed, perf_stats.classify_exception(e),
                     f"{type(e).__name__}: {str(e)[:200]}")

    elapsed = (time.perf_counter() - start) * 1000
    status = resp.status_code

    if status in (401, 403):
        # 未读取响应体时主动关连接，避免 keep-alive 复用脏连接（部分网关 401 不读 body）
        resp.close()
        return _fail(elapsed, "http_error",
                     f"HTTP {status}（Cookie 可能已失效或未登录，请重新登录获取 JSESSIONID）", status)

    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        resp.close()
        return _fail(elapsed, "http_error", f"HTTP {status}: {str(e)[:160]}", status)

    try:
        data = resp.json()
    except ValueError:
        snippet = (resp.text or "")[:160].replace("\n", " ")
        is_login = "login" in (resp.url or "").lower() or "<html" in snippet.lower()
        hint = "（响应像登录页/HTML，Cookie 可能已失效）" if is_login else ""
        return _fail(elapsed, "parse_error", f"响应不是 JSON{hint}: {snippet}", status)

    if not isinstance(data, dict):
        return _fail(elapsed, "parse_error", "响应 JSON 不是对象", status)

    text = data.get(answer_field)
    if text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)
    tables = data.get("tableData") or []
    papers = data.get("paperData") or []
    table_n = len(tables) if isinstance(tables, list) else 0
    paper_n = len(papers) if isinstance(papers, list) else 0

    if not text.strip() and table_n == 0 and paper_n == 0:
        return _fail(elapsed, "incomplete_answer",
                     "text/tableData/paperData 均为空（可能是兜底话术或接口返回结构变化）", status)
    if min_answer_len > 0 and len(text) < min_answer_len:
        return _fail(elapsed, "incomplete_answer",
                     f"text 长度 {len(text)} < {min_answer_len}", status)

    return {
        "ok": True, "answer": text, "ttfb_ms": elapsed, "elapsed_ms": elapsed,
        "http_status": status, "length": len(text), "tables": table_n, "papers": paper_n,
        "fail_category": None, "fail_reason": "",
    }


def read_questions(file_path: str) -> list:
    """读取语料文件，每行一个问题（UTF-8，兼容 BOM）。"""
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"读取语料文件失败: {str(e)}")
        return []


def parse_cookie_pairs(cookie: str) -> dict:
    """把 "a=1; b=2" 解析成字典（值内可含 '='）。"""
    pairs = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            pairs[name] = value
    return pairs


def parse_headers(items) -> dict:
    """把 --header "Name: Value" 列表解析为字典（值内可含冒号）。"""
    headers = {}
    for raw in items or []:
        if ":" not in raw:
            raise SystemExit(f"错误: --header 格式应为 'Name: Value': {raw}")
        name, value = raw.split(":", 1)
        if not name.strip():
            raise SystemExit(f"错误: --header 缺少名称: {raw}")
        headers[name.strip()] = value.strip()
    return headers


class ChatUser:
    """一个模拟用户 = 一个独立 requests.Session（可绑定独立 Cookie）。"""

    def __init__(self, user_id: str, cookie_tag: str, cookie: str,
                 questions: list, opts: dict, stats: perf_stats.StatsCollector,
                 output_dir: str, verify: bool):
        self.user_id = user_id
        self.cookie_tag = cookie_tag
        self.questions = questions
        self.opts = opts
        self.stats = stats
        self.verify = verify

        self.session = requests.Session()
        # Cookie 放进 Session 的 cookie jar：服务端通过 Set-Cookie 轮换 JSESSIONID 时
        # 后续请求会自动使用新值；--header "Cookie: ..." 仍可整体覆盖。
        cookie_pairs = parse_cookie_pairs(cookie)
        if cookie_pairs:
            self.session.cookies.update(cookie_pairs)
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": opts.get("user_agent") or DEFAULT_UA,
        })
        if opts.get("headers"):
            self.session.headers.update(opts["headers"])

        os.makedirs(output_dir, exist_ok=True)
        self.csv_file = os.path.join(
            output_dir, f"custom_agent_chat_responses_{_safe_name(user_id)}.csv")
        with open(self.csv_file, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([
                "timestamp", "user_id", "cookie_tag", "round", "req_type", "http_status",
                "ok", "question", "answer", "ttfb_ms", "response_time_ms",
                "response_length", "tables", "papers", "fail_category", "fail_reason",
            ])

    def _write_csv(self, round_no: int, req_type: str, question: str, res: dict):
        with open(self.csv_file, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), self.user_id, self.cookie_tag,
                round_no, req_type, res.get("http_status") or "",
                "1" if res["ok"] else "0",
                question.replace("\n", " ").replace("\r", " "),
                (res["answer"] or "").replace("\n", " ").replace("\r", " "),
                round(res["ttfb_ms"], 1), round(res["elapsed_ms"], 1),
                res["length"], res["tables"], res["papers"],
                res.get("fail_category") or "",
                (res.get("fail_reason") or "").replace("\n", " "),
            ])

    def run(self):
        last = len(self.questions) - 1
        for i, question in enumerate(self.questions):
            res = chat_once(
                self.session, self.opts["url"], question,
                message_field=self.opts["message_field"],
                answer_field=self.opts["answer_field"],
                timeout=self.opts["timeout"],
                verify=self.verify,
                min_answer_len=self.opts["min_answer_len"],
            )
            req_type = perf_stats.REQ_NEW if i == 0 else perf_stats.REQ_CONTINUE
            self.stats.record(
                req_type, res["ok"], ttfb_ms=res["ttfb_ms"], latency_ms=res["elapsed_ms"],
                length=res["length"], fail_category=res["fail_category"],
            )
            progress.emit({
                "type": "round_done", "user_id": self.user_id, "round": i + 1,
                "ok": res["ok"], "req_type": req_type,
                "ttfb_ms": round(res["ttfb_ms"], 2),
                "elapsed_ms": round(res["elapsed_ms"], 2),
            })
            if not res["ok"]:
                print(f"[{self.user_id}] 第 {i + 1} 轮失败 "
                      f"({res.get('fail_category')}): {res.get('fail_reason')}")
            self._write_csv(i + 1, req_type, question, res)
            if self.opts["sleep"] > 0 and i < last:
                time.sleep(self.opts["sleep"])
        self.session.close()


def worker(user_queue: queue.Queue, questions: list, opts: dict,
           stats: perf_stats.StatsCollector, output_dir: str, verify: bool):
    while True:
        try:
            user_id, cookie_tag, cookie = user_queue.get_nowait()
        except queue.Empty:
            break
        print(f"启动用户: {user_id}（Cookie: {cookie_tag}）")
        user = ChatUser(user_id, cookie_tag, cookie, questions, opts, stats,
                        output_dir, verify)
        try:
            user.run()
        except Exception as e:
            print(f"[{user_id}] 用户线程异常: {type(e).__name__}: {e}")
        finally:
            user.session.close()
        print(f"完成用户: {user_id}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="自定义智能体接口（Cookie 鉴权 / 非流式 JSON）并发压测")
    parser.add_argument("--url", default=None,
                        help="接口地址（默认取 .env / 环境变量 CUSTOM_AGENT_URL；未配置时报错）")
    parser.add_argument("--cookie", default=None,
                        help="单个 Cookie，如 \"JSESSIONID=...\"（默认取 CUSTOM_AGENT_COOKIE）")
    parser.add_argument("--cookie-file", default=None,
                        help="Cookie 文件，每行一个；支持 \"别名<TAB>Cookie\"")
    parser.add_argument("--corpus", default=os.path.join(config.PROJECT_ROOT, "corpus", "test1.txt"),
                        help="语料文件（每行一个问题）")
    parser.add_argument("--users", type=int, default=5, help="模拟用户数")
    parser.add_argument("--threads", type=int, default=5, help="并发线程数")
    parser.add_argument("--rounds", type=int, default=10,
                        help="每用户轮数（超过语料行数时按语料行数截断）")
    parser.add_argument("--sleep", type=float, default=1.0, help="每轮思考间隔（秒）")
    parser.add_argument("--timeout", type=float, default=120.0, help="单请求读取超时（秒）")
    parser.add_argument("--message-field", default="message", help="请求体中的问题字段名")
    parser.add_argument("--answer-field", default="text", help="响应体中的回答字段名")
    parser.add_argument("--min-answer-len", type=int, default=0,
                        help="回答最小长度（0=只要求 text/tableData/paperData 非全空）")
    parser.add_argument("--user-agent", default=None, help="自定义 User-Agent")
    parser.add_argument("--header", action="append", default=[], metavar="'Name: Value'",
                        help="附加请求头（可重复），如 --header 'Referer: https://...'")
    parser.add_argument("--insecure", action="store_true",
                        help="跳过 HTTPS 证书校验（仅自签名环境）")
    parser.add_argument("--report", default=None,
                        help="Markdown 汇总路径（默认项目根 压测汇总.md）")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.users < 1 or args.threads < 1 or args.rounds < 1:
        raise SystemExit("错误: --users / --threads / --rounds 必须 >= 1")

    url = (args.url or DEFAULT_URL)
    if not url:
        raise SystemExit(
            "错误: 未提供接口地址。请用 --url，或在 .env 中设置 CUSTOM_AGENT_URL"
            "（示例：https://<custom-agent-host>/bmserv/api/agent/unified/chat）")
    if not url.lower().startswith(("http://", "https://")):
        raise SystemExit(f"错误: 接口地址非法: {url}")

    # Cookie 来源：--cookie-file / CUSTOM_AGENT_COOKIE_FILE，再追加 --cookie / CUSTOM_AGENT_COOKIE
    cookie_file = (args.cookie_file
                   or os.environ.get("CUSTOM_AGENT_COOKIE_FILE", "").strip())
    cookies = load_cookie_file(cookie_file) if cookie_file else []
    raw_cookie = args.cookie if args.cookie is not None else os.environ.get("CUSTOM_AGENT_COOKIE", "")
    if raw_cookie and raw_cookie.strip():
        cookie = normalize_cookie(raw_cookie)
        if "=" not in cookie:
            raise SystemExit("错误: --cookie 不是合法 Cookie（应形如 JSESSIONID=...）")
        cookies.append(("cli", cookie))
    if not cookies:
        raise SystemExit(
            "错误: 未提供登录 Cookie。请用 --cookie/--cookie-file，或在 .env 中设置 "
            "CUSTOM_AGENT_COOKIE（浏览器 F12 → Network → 该请求 → 请求头 Cookie）")

    unique_cookies = len({c for _, c in cookies})
    if unique_cookies < len(cookies):
        print(f"警告: Cookie 列表含 {len(cookies) - unique_cookies} 条重复值，"
              f"重复会话不会得到独立上下文")

    questions = read_questions(args.corpus)
    if not questions:
        raise SystemExit(f"错误: 语料为空或不存在: {args.corpus}")
    rounds = min(args.rounds, len(questions))
    if rounds < args.rounds:
        print(f"提示: 语料仅 {len(questions)} 行，每用户轮数由 {args.rounds} 调整为 {rounds}")
    questions = questions[:rounds]

    if len(cookies) < args.users:
        print("=" * 78)
        print(f"警告: 只有 {len(cookies)} 个 Cookie，却要模拟 {args.users} 个用户。")
        print("      同一 Cookie 的多个用户共享服务端会话：上下文可能互相污染，")
        print("      测得的延迟/失败也可能来自会话级串行化，而非真实并发能力。")
        print("      需要真实多用户并发时，请为每个用户准备独立登录会话（cookie 文件）。")
        print("=" * 78)

    threads_n = min(args.threads, args.users)
    if threads_n < args.threads:
        print(f"提示: 线程数受用户数限制，由 {args.threads} 调整为 {threads_n}")

    output_dir = config.resolve_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    verify = False if args.insecure else config.parse_verify_ssl()
    stats = perf_stats.StatsCollector()
    opts = {
        "url": url, "message_field": args.message_field,
        "answer_field": args.answer_field, "timeout": args.timeout,
        "sleep": args.sleep, "min_answer_len": args.min_answer_len,
        "user_agent": args.user_agent, "headers": parse_headers(args.header),
    }

    print("========== 自定义智能体压测配置 ==========")
    print(f"接口: {url}")
    print(f"Cookie: {len(cookies)} 个（首个: {mask_cookie(cookies[0][1])}）")
    print(f"用户: {args.users} | 线程: {threads_n} | 每用户轮数: {rounds} | "
          f"思考间隔: {args.sleep}s | 超时: {args.timeout}s")
    print(f"语料: {args.corpus}（{len(questions)} 条）")
    print("口径: 接口非流式，TTFB = 整轮耗时；ITL/生成速率不可测")
    print("备注: Cookie 为登录态，有效期有限；401/403/登录页记为 http_error/parse_error")

    params = {
        "users": args.users, "threads": threads_n, "rounds": rounds,
        "sleep_s": args.sleep, "timeout_s": args.timeout,
        "corpus": args.corpus, "url": url, "auth": "cookie",
        "streaming": False, "cookie_sessions": len(cookies),
    }

    progress.emit({
        "type": "start", "script": "custom_agent_concurrent.py",
        "protocol": "custom_agent", "test_type": "concurrent",
        "params": params, "expected_rounds": args.users * rounds,
    })

    user_queue = queue.Queue()
    for i in range(args.users):
        cookie_tag, cookie = cookies[i % len(cookies)]
        user_queue.put((f"user_{i + 1:03d}", cookie_tag, cookie))

    start = time.time()
    workers = [
        threading.Thread(target=worker,
                         args=(user_queue, questions, opts, stats, output_dir, verify),
                         daemon=True)
        for _ in range(threads_n)
    ]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    wall_time = time.time() - start

    print("所有用户测试完成")
    stats.print_summary(wall_time, detail=os.path.join(
        output_dir, "custom_agent_chat_responses_*.csv"))

    json_path = None
    if os.environ.get("LLMPERF_SUMMARY", "1").strip() != "0":
        json_path = perf_stats.save_report_json(
            output_dir, script="custom_agent_concurrent.py", protocol="custom_agent",
            endpoint=url, model="custom-agent", params=params,
            wall_time=wall_time, stats=stats)
        print(f"JSON 汇总: {json_path}")

    report_path = args.report or os.path.join(config.PROJECT_ROOT, "压测汇总.md")
    perf_stats.save_report_md(
        report_path, script="custom_agent_concurrent.py", protocol="custom_agent",
        endpoint=url, model="custom-agent", params=params,
        wall_time=wall_time, stats=stats, json_path=json_path)
    print(f"Markdown 汇总: {report_path}")

    auto_post_run(json_path)
    progress.emit({"type": "done", "summary_json": json_path or ""})


if __name__ == "__main__":
    main()

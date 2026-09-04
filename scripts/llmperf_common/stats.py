# -*- coding: utf-8 -*-
"""
llmperf_common.stats —— 统一压测统计（仅标准库，零第三方依赖）

- FAIL_CATEGORIES / classify_exception：失败原因分类
  （不 import openai/requests/httpx，按异常模块名/类名模式分派，见 5.1 映射表）
- StatsCollector：线程安全、按请求类型分组（NewChat / ContinueChat / Aggregated）的指标收集
- save_report_md / save_report_json：追加式 Markdown 汇总 + 机器可读 JSON 结果（schema v1）

样本口径（v1.1 修订 E）：itl_ms / gen_chars_per_sec 仅当 ok=True 时并入分组样本。
失败轮次：多线程 / Locust 脚本不写逐轮 CSV（失败仅按 fail_category 计入失败分布，
中途流断已收的测量值不落盘）；基线脚本由调用方在逐轮 CSV 中保留失败行（空 answer）。
JSON 中无样本指标输出 null。
"""
import os
import json
import threading
from datetime import datetime

REQ_NEW = "NewChat"            # 新建会话
REQ_CONTINUE = "ContinueChat"  # 多轮追问
REQ_AGGREGATED = "Aggregated"  # 汇总合计
ROW_ORDER = [REQ_NEW, REQ_CONTINUE, REQ_AGGREGATED]

FAIL_CATEGORIES = {
    "timeout":            "请求/读取超时",
    "connection_error":   "连接异常（DNS/拒绝/复位等）",
    "http_error":         "HTTP 状态码非 200（具体状态码记入 fail_reason）",
    "stream_error_event": "服务端流内错误（Dify event=error / OpenAI chunk.error）",
    "parse_error":        "SSE 数据 JSON 解析失败",
    "incomplete_answer":  "回答长度低于阈值（ANSWER_MIN_LEN=10）",
    "unknown":            "其他未归类异常",
}

_PROTOCOL_DESC = {
    "dify": "Dify 风格 `/v1/chat-messages`（conversation_id 多轮）",
    "openai_compat": "OpenAI 兼容 `/chat/completions`（messages 多轮）",
}


def classify_exception(e) -> str:
    """按异常模块名/类名模式分派失败分类，零第三方 import。

    判定优先级：**Timeout 先于 Connection**——openai.APITimeoutError 同时继承自
    openai.APIConnectionError，顺序反了会把超时误归为 connection_error。
    """
    module = type(e).__module__ or ""
    name = type(e).__name__ or ""
    if module.startswith("openai"):
        if "Timeout" in name:
            return "timeout"
        if "Connection" in name:
            return "connection_error"
        if "Status" in name or name in (
                "AuthenticationError", "PermissionDeniedError", "NotFoundError",
                "RateLimitError", "BadRequestError", "ConflictError",
                "UnprocessableEntityError", "InternalServerError"):
            return "http_error"
        return "unknown"
    if module.startswith("httpx"):
        if "Timeout" in name:
            return "timeout"
        if "Connect" in name or "Connection" in name or "RemoteProtocol" in name:
            return "connection_error"
        return "unknown"
    if module.startswith("requests"):
        if "Timeout" in name:
            return "timeout"
        if "Connection" in name:
            return "connection_error"
        if "SSL" in name:  # SSLError 继承 ConnectionError，但类名不含 Connection（评审修订）
            return "connection_error"
        if "HTTPError" in name:
            return "http_error"
        return "unknown"
    if module.startswith("urllib3"):
        # requests/httpx 包装前的原始底层异常（协议错误/新连接失败等，连接类居多）
        return "timeout" if "Timeout" in name else "connection_error"
    # 兜底：裸 OSError 族（socket / http.client 透出的原始异常，模块名非以上三方）
    if isinstance(e, TimeoutError):
        return "timeout"
    if isinstance(e, (ConnectionError, OSError)):
        return "connection_error"
    return "unknown"


class StatsCollector:
    """线程安全的压测指标收集器（按请求类型分组，与 Locust 一致）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._rows = {
            name: {"total": 0, "fails": 0, "ttfbs": [], "latencies": [], "lengths": [],
                   "itls": [], "gens": []}
            for name in ROW_ORDER
        }
        self._fail_categories = {}  # 失败分类计数（仅失败请求计入）

    def record(self, name, ok, *, ttfb_ms=0.0, latency_ms=0.0, length=0,
               itl_ms=None, gen_chars_per_sec=None, fail_category=None):
        """记录一次请求（同时计入所属分组与 Aggregated 汇总）。

        v1.1 修订 E：itl_ms / gen_chars_per_sec 仅当 ok=True 时并入分组样本；
        失败请求按 fail_category 计入失败分布（失败轮不写逐轮 CSV——基线脚本除外，
        是否写失败行由调用方决定）。

        如果 name 不在预定义的 _rows 键中，自动创建该分组（适配 Locust 脚本的
        "ChatCompletions" 等自定义请求类型名）。
        """
        with self._lock:
            for key in (name, REQ_AGGREGATED):
                if key not in self._rows:
                    self._rows[key] = {"total": 0, "fails": 0, "ttfbs": [], "latencies": [],
                                       "lengths": [], "itls": [], "gens": []}
                row = self._rows[key]
                row["total"] += 1
                if ok:
                    row["ttfbs"].append(ttfb_ms)
                    row["latencies"].append(latency_ms)
                    row["lengths"].append(length)
                    if itl_ms:
                        row["itls"].extend(itl_ms)
                    if gen_chars_per_sec is not None:
                        row["gens"].append(gen_chars_per_sec)
                else:
                    row["fails"] += 1
            if not ok:
                cat = fail_category if fail_category in FAIL_CATEGORIES else "unknown"
                self._fail_categories[cat] = self._fail_categories.get(cat, 0) + 1

    def row(self, name):
        return self._rows[name]

    def fail_categories(self):
        """失败分类计数（字典拷贝，避免调用方误改内部状态）"""
        return dict(self._fail_categories)

    @staticmethod
    def _pct(data, p):
        """百分位；空数据返回 None（JSON 输出 null，MD/控制台由调用方格式化为 -）"""
        if not data:
            return None
        s = sorted(data)
        idx = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
        return s[idx]

    def _latency_cells(self, row):
        """精简百分位列: Median / Avg / Min / p90 / p95 / p99 / Max（毫秒）"""
        lats = row["latencies"]
        if not lats:
            return ["-"] * 7
        return [f"{self._pct(lats, 50):.0f}", f"{sum(lats) / len(lats):.0f}", f"{min(lats):.0f}",
                f"{self._pct(lats, 90):.0f}", f"{self._pct(lats, 95):.0f}",
                f"{self._pct(lats, 99):.0f}", f"{max(lats):.0f}"]

    def md_row(self, name, wall_time):
        """生成指定请求类型的 Markdown 统计行"""
        row = self._rows[name]
        cells = self._latency_cells(row)
        rps = f"{row['total'] / wall_time:.2f}" if wall_time > 0 else "-"
        type_label = "POST" if name != REQ_AGGREGATED else "-"
        return (f"| {type_label} | {name} | {row['total']} | {row['fails']} | "
                + " | ".join(cells) + f" | {rps} |")

    def print_summary(self, wall_time, detail="results/chat_responses_*.csv"):
        """打印 Locust 风格汇总表（两协议统一格式）"""
        print("\n========== 压测汇总 ==========")
        agg = self._rows[REQ_AGGREGATED]
        total = agg["total"]
        if total == 0:
            print("没有产生任何请求")
            return

        print(f"{'Type':<6} {'Name':<14} {'# reqs':>7} {'# fails':>8} {'Median':>9} {'Avg':>8} "
              f"{'Min':>8} {'p90':>8} {'p95':>8} {'p99':>8} {'Max':>8} {'RPS':>8}")
        for name in ROW_ORDER:
            row = self._rows[name]
            cells = self._latency_cells(row)
            rps = f"{row['total'] / wall_time:.2f}" if wall_time > 0 else "-"
            type_label = "POST" if name != REQ_AGGREGATED else "-"  # Aggregated 是汇总行，不标 POST
            print(f"{type_label:<6} {name:<14} {row['total']:>7} {row['fails']:>8} {cells[0]:>9} "
                  f"{cells[1]:>8} {cells[2]:>8} {cells[3]:>8} {cells[4]:>8} {cells[5]:>8} "
                  f"{cells[6]:>8} {rps:>8}")

        fails = agg["fails"]
        success = total - fails
        print(f"\n总请求数: {total} | 成功: {success} | 失败: {fails} | 成功率: {success / total * 100:.1f}%")
        if wall_time > 0:
            print(f"总耗时: {wall_time:.1f}s | 总 RPS: {total / wall_time:.2f} req/s | "
                  f"成功请求吞吐: {success / wall_time:.2f} req/s")
        ttfbs = agg["ttfbs"]
        if ttfbs:
            print(f"TTFB(ms)     avg={sum(ttfbs) / len(ttfbs):.0f}  p50={self._pct(ttfbs, 50):.0f}  "
                  f"p95={self._pct(ttfbs, 95):.0f}  max={max(ttfbs):.0f}")
        itls = agg["itls"]
        if itls:
            print(f"ITL(ms)      avg={sum(itls) / len(itls):.0f}  p50={self._pct(itls, 50):.0f}  "
                  f"p95={self._pct(itls, 95):.0f}  max={max(itls):.0f}")
        gens = agg["gens"]
        if gens:
            print(f"生成速率(字符/s) avg={sum(gens) / len(gens):.1f}  p50={self._pct(gens, 50):.1f}")
        if self._fail_categories:
            dist = ", ".join(f"{k}={v}" for k, v in sorted(self._fail_categories.items()))
            print(f"失败分布: {dist}")
        print(f"明细: {detail}")

    def _metrics_json(self, name):
        """分组指标 JSON 结构；无样本字段为 null（schema v1）"""
        row = self._rows[name]
        ttfbs, lats, itls, gens, lens = (row["ttfbs"], row["latencies"], row["itls"],
                                         row["gens"], row["lengths"])

        def _full(data):
            return {
                "avg": sum(data) / len(data) if data else None,
                "p50": self._pct(data, 50), "p90": self._pct(data, 90),
                "p95": self._pct(data, 95), "p99": self._pct(data, 99),
                "min": min(data) if data else None,
                "max": max(data) if data else None,
            }

        return {
            "ttfb_ms": _full(ttfbs),
            "latency_ms": _full(lats),
            "itl_ms": _full(itls),
            "gen_chars_per_sec": {
                "avg": sum(gens) / len(gens) if gens else None,
                "p50": self._pct(gens, 50),
            },
            "response_length": {
                "avg": sum(lens) / len(lens) if lens else None,
                "p50": self._pct(lens, 50), "p95": self._pct(lens, 95),
                "max": max(lens) if lens else None,
            },
        }


def _fmt_metric_row(label, data):
    """MD 指标行（avg/p50/p95/max）；空样本返回 None"""
    if not data:
        return None
    return (f"| {label} | {sum(data) / len(data):.0f} | "
            f"{StatsCollector._pct(data, 50):.0f} | {StatsCollector._pct(data, 95):.0f} | "
            f"{max(data):.0f} |")


def save_report_md(report_path, *, script, protocol, endpoint, model, params,
                   wall_time, stats, json_path=None):
    """将压测汇总以 Markdown 段落追加写入报告文件（每次运行追加一条记录）。

    - 失败分布行：仅在有失败时输出（v1.1 修订 C，与 5.1 一致）；
    - JSON 结果行：指向本次运行产出的 summary_*.json 相对路径（5.3）。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    agg = stats.row(REQ_AGGREGATED)
    total = agg["total"]
    fails = agg["fails"]
    success = total - fails
    rate = (success / total * 100) if total else 0.0
    total_rps = total / wall_time if wall_time > 0 else 0.0
    succ_rps = success / wall_time if wall_time > 0 else 0.0

    lines = []
    # 文件不存在时先写入标题头
    if not os.path.exists(report_path):
        lines += [
            "# 压测汇总",
            "",
            "> 由 `scripts/multi_thread_record.py` 与 `scripts/openai_compat_multi_thread_record.py` "
            "经 `llmperf_common/stats.py` 统一生成，每次运行追加一条记录。",
            "",
        ]
    lines += [
        f"## 压测记录 {now}",
        "",
        f"- **协议**: {_PROTOCOL_DESC.get(protocol, protocol)}",
        f"- **服务地址**: {endpoint}",
        f"- **模型**: {model or '-'}",
        f"- **压测参数**: 用户数={params['users']} | 线程数={params['threads']} | "
        f"每用户轮数={params['rounds']} | 思考间隔={params['sleep_s']}s | 超时={params['timeout_s']}s",
        f"- **语料**: {params['corpus']}",
        f"- **总耗时**: {wall_time:.1f}s | **总 RPS**: {total_rps:.2f} req/s | "
        f"**成功请求吞吐**: {succ_rps:.2f} req/s",
    ]
    if fails:
        dist = ", ".join(f"{k}={v}" for k, v in sorted(stats.fail_categories().items()))
        lines.append(f"- **失败分布**: {dist}")
    if json_path:
        rel = os.path.relpath(json_path, os.path.dirname(report_path)).replace("\\", "/")
        lines.append(f"- **JSON 结果**: {rel}")
    lines += [
        "",
        "| 总请求数 | 成功 | 失败 | 成功率 |",
        "|---|---|---|---|",
        f"| {total} | {success} | {fails} | {rate:.1f}% |",
        "",
        "| Type | Name | # reqs | # fails | Median (ms) | Avg (ms) | Min (ms) | p90 (ms) | p95 (ms) | p99 (ms) | Max (ms) | RPS |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in ROW_ORDER:
        lines.append(stats.md_row(name, wall_time))
    metric_rows = []
    if agg["ttfbs"]:
        metric_rows.append(_fmt_metric_row("TTFB (ms)", agg["ttfbs"]))
        metric_rows.append(_fmt_metric_row("整轮耗时 (ms)", agg["latencies"]))
    if agg["itls"]:
        metric_rows.append(_fmt_metric_row("ITL (ms)", agg["itls"]))
    if agg["gens"]:
        gens = agg["gens"]
        metric_rows.append(f"| 生成速率 (字符/秒) | {sum(gens) / len(gens):.1f} | "
                           f"{StatsCollector._pct(gens, 50):.1f} | - | - |")
    if agg["lengths"]:
        metric_rows.append(_fmt_metric_row("响应长度 (字符)", agg["lengths"]))
    if metric_rows:
        lines += ["", "| 指标 | avg | p50 | p95 | max |", "|---|---|---|---|---|"] + metric_rows
    lines += ["", "---", ""]

    with open(report_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def save_report_json(output_dir, *, script, protocol, endpoint, model, params,
                     wall_time, stats):
    """写出 results/summary_<dify|openai>_<yyyyMMdd_HHmmss>.json，返回文件绝对路径。

    schema v1（见方案 5.3）；无样本指标为 null；protocol 标签 dify / openai_compat。
    """
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "openai" if protocol == "openai_compat" else "dify"
    path = os.path.join(output_dir, f"summary_{tag}_{ts}.json")

    agg = stats.row(REQ_AGGREGATED)
    total = agg["total"]
    fails = agg["fails"]
    success = total - fails

    # 修复 #13：除 ROW_ORDER（NewChat/ContinueChat/Aggregated）外，把脚本自定义分组
    # （如 OpenAI Locust 的 "ChatCompletions"）也纳入 by_request_type 输出——此前自定义组
    # 数据只累计进 Aggregated、命名组恒 0，HTML 报告/对比页出现空组误导
    group_names = list(ROW_ORDER) + [
        n for n in stats._rows
        if n not in ROW_ORDER and (stats._rows[n]["total"] > 0 or stats._rows[n]["fails"] > 0)
    ]
    doc = {
        "schema_version": 1,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "script": script,
        "protocol": protocol,
        "endpoint": endpoint,
        "model": model or "",
        "params": params,
        "wall_time_s": round(wall_time, 1),
        "totals": {
            "requests": total,
            "success": success,
            "fails": fails,
            "success_rate": round(success / total * 100, 1) if total else 0.0,
            "rps_total": round(total / wall_time, 3) if wall_time > 0 else 0.0,
            "rps_success": round(success / wall_time, 3) if wall_time > 0 else 0.0,
        },
        "fail_categories": {k: stats.fail_categories()[k] for k in sorted(stats.fail_categories())},
        "by_request_type": {
            name: {
                "reqs": stats.row(name)["total"],
                "fails": stats.row(name)["fails"],
                "metrics": stats._metrics_json(name),
            }
            for name in group_names
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return path

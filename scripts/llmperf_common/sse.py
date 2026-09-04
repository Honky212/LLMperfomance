# -*- coding: utf-8 -*-
"""
llmperf_common.sse —— SSE 解析 + 流式计时（仅标准库，零第三方依赖）

- StreamResult：统一解析结果（answer / conversation_id / ttfb_ms / elapsed_ms /
  chunk_count / itl_ms / gen_chars_per_sec / fail_category / fail_reason）
- StreamTimer：流式计时器（TTFB / ITL / 生成速率采集，G1）
- consume_dify_sse / consume_openai_sse：requests/Locust 用行迭代器解析
- extract_openai_delta：SDK chunk 安全取 delta.content
- failed_result：请求建立阶段等解析器之外失败的构造工厂

设计原则（见方案 3.2）：
1. 解析器只消费"行迭代器"或"SDK chunk"，不绑定任何 HTTP 客户端；
2. 成败由调用方映射（Locust 据此调用 response.success()/failure()），公共模块不替调用方决定；
3. 继承 0831 修复后的全部既定口径：首个真实内容计 TTFB、conversation_id 只更新不清空、
   长度阈值校验；Dify 错误事件双格式兼容（修订 B）；
4. 迭代期网络中断由解析器内部捕获归类（同包 stats.classify_exception），"不向外抛异常"承诺闭环；
5. 失败轮次仍如实产出 itl_ms / gen_chars_per_sec（留档行为由调用方决定：
   基线脚本写入逐轮 CSV 失败行；多线程 / Locust 脚本不写 CSV，仅用于失败计数），
   是否进全局聚合由 stats 层过滤。
"""
import json
import time
from dataclasses import dataclass, field

from llmperf_common.stats import classify_exception


@dataclass
class StreamResult:
    answer: str                     # 完整回答文本
    conversation_id: str            # 仅 Dify 协议；OpenAI 版恒为 ""
    ttfb_ms: float | None           # 首真实内容到达耗时（无内容为 None）
    elapsed_ms: float               # 完整往返耗时（含流式消费）
    chunk_count: int                # 内容块数量（非空内容块）
    itl_ms: list                     # 相邻内容块间隔样本（<2 块时为空列表）
    gen_chars_per_sec: float | None  # 生成速率（口径见 4.1；分母<=0 时为 None）
    fail_category: str | None       # 失败分类（见 stats.FAIL_CATEGORIES），None 表示成功
    fail_reason: str | None         # 原始失败原因文本（供日志/上报）


class StreamTimer:
    """流式计时器：请求发出前构造；每到一个内容块调用 on_content()；
    结束调用 result(answer, ...) 产出 StreamResult（含 TTFB / ITL / 生成速率）。

    - 首个内容块定格 TTFB，不产生 ITL 样本；
    - 内容块 < 2 时 itl_ms 为空列表，gen_chars_per_sec 为 None；
    - on_content 可显式传 now（便于离线单测注入时刻）；
    - clock 参数仅供离线单测注入确定性时刻，脚本运行使用默认 time.time。
    """

    def __init__(self, start_time: float | None = None, clock=None):
        self._clock = clock if clock is not None else time.time
        self.start_time = start_time if start_time is not None else self._clock()
        self._first_ts: float | None = None
        self._prev_ts: float | None = None
        self._last_ts: float | None = None
        self._chunk_count = 0
        self.itl_ms: list = []

    def on_content(self, now: float | None = None) -> None:
        ts = self._clock() if now is None else now
        if self._first_ts is None:
            self._first_ts = ts
        else:
            self.itl_ms.append((ts - self._prev_ts) * 1000)  # 相邻内容块间隔
        self._prev_ts = ts
        self._last_ts = ts
        self._chunk_count += 1

    @property
    def ttfb_ms(self) -> float | None:
        return (self._first_ts - self.start_time) * 1000 if self._first_ts is not None else None

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    def gen_chars_per_sec(self, chars: int) -> float | None:
        """纯生成阶段字符产出速率 = chars / (末块时刻 - 首块时刻)；分母<=0 返回 None"""
        if self._first_ts is None or self._last_ts is None or self._last_ts <= self._first_ts:
            return None
        return chars / (self._last_ts - self._first_ts)

    def result(self, answer: str, *, conversation_id: str = "", end_time: float | None = None,
               fail_category: str | None = None, fail_reason: str | None = None) -> StreamResult:
        """流结束调用：组装 StreamResult（elapsed 以当前时刻/end_time 计）"""
        end = self._clock() if end_time is None else end_time
        return StreamResult(
            answer=answer,
            conversation_id=conversation_id,
            ttfb_ms=self.ttfb_ms,
            elapsed_ms=(end - self.start_time) * 1000,
            chunk_count=self.chunk_count,
            itl_ms=list(self.itl_ms),
            gen_chars_per_sec=self.gen_chars_per_sec(len(answer)),
            fail_category=fail_category,
            fail_reason=fail_reason,
        )


def failed_result(fail_category: str, fail_reason: str | None, *,
                  start_time: float | None = None) -> StreamResult:
    """构造失败 StreamResult（请求建立阶段异常 / HTTP 非 200 等解析器之外的失败）"""
    elapsed_ms = 0.0
    if start_time is not None:
        elapsed_ms = (time.time() - start_time) * 1000
    return StreamResult(answer="", conversation_id="", ttfb_ms=None, elapsed_ms=elapsed_ms,
                        chunk_count=0, itl_ms=[], gen_chars_per_sec=None,
                        fail_category=fail_category, fail_reason=fail_reason)


def itl_summary(itl_ms: list):
    """逐轮 ITL 汇总：(avg_ms, p95_ms)；无样本返回 (None, None)（供逐轮 CSV 列使用）"""
    if not itl_ms:
        return None, None
    s = sorted(itl_ms)
    p95 = s[min(len(s) - 1, int(round(95 / 100.0 * (len(s) - 1))))]
    return sum(s) / len(s), p95


def extract_openai_delta(chunk) -> str:
    """安全提取 chunk 的 delta.content（兼容 SDK 对象与 dict；空/异常结构返回 ""）"""
    try:
        if isinstance(chunk, dict):
            choices = chunk.get("choices") or []
            if not choices:
                return ""
            first = choices[0]
            delta = first.get("delta") if isinstance(first, dict) else first.delta
            content = delta.get("content") if isinstance(delta, dict) else (
                delta.content if delta is not None else None)
        else:
            choices = chunk.choices
            if not choices:
                return ""
            delta = choices[0].delta
            content = delta.content if delta is not None else None
    except (AttributeError, IndexError, TypeError):
        return ""
    return content or ""


def consume_dify_sse(lines, *, start_time, min_answer_len=10, clock=None) -> StreamResult:
    """requests/Locust 用：data: 前缀解析 + message/error 事件 + conversation_id 提取。

    - TTFB 只计首个真实回答内容（event=='message' 且 answer 非空）；
    - conversation_id 仅在 chunk 携带非空值时更新（不被无该字段的 chunk 清空）；
    - 错误事件双格式（修订 B）：`data:{"event":"error"}` 与标准 SSE `event: error` + data 两行格式
      （pending_event 一次性消费/清空，`event:` 冒号后允许无空格并 strip，非 error 事件行忽略）；
    - 回答长度 < min_answer_len → incomplete_answer；
    - 迭代期网络中断等异常由本函数捕获归类，不向外抛。
    """
    timer = StreamTimer(start_time=start_time, clock=clock)
    parts = []
    conversation_id = ""
    fail_category = None
    fail_reason = None
    pending_event = None  # 上一条非 data 行的 event: 值（一次性消费）

    try:
        for line in lines:
            if not line:
                continue
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded.startswith("event:"):
                pending_event = decoded[len("event:"):].strip()
                continue
            if not decoded.startswith("data:"):
                continue
            json_str = decoded[5:].strip()
            event_line = pending_event  # 消费待处理 event（一次性，防陈旧污染）
            pending_event = None
            try:
                chunk = json.loads(json_str)
            except json.JSONDecodeError:
                fail_category = fail_category or "parse_error"
                fail_reason = fail_reason or f"JSON解析失败: {json_str}"
                continue
            event = chunk.get("event", event_line or "")
            if event == "message":
                piece = chunk.get("answer", "")
                if piece:
                    timer.on_content()
                    parts.append(piece)
                cid = chunk.get("conversation_id", "")
                if cid:
                    conversation_id = cid
            elif event == "error":
                fail_category = fail_category or "stream_error_event"
                fail_reason = fail_reason or str(chunk.get("message", "unknown error"))[:200]
    except Exception as e:
        # 迭代期网络中断等（解析器兜底，保证"不向外抛异常"承诺）
        if fail_category is None:
            fail_category = classify_exception(e)
            fail_reason = str(e)[:200]

    answer = "".join(parts)
    if fail_category is None and len(answer) < min_answer_len:
        fail_category = "incomplete_answer"
        fail_reason = f"回答长度 {len(answer)} < {min_answer_len}"
    return timer.result(answer, conversation_id=conversation_id,
                        fail_category=fail_category, fail_reason=fail_reason)


def consume_openai_sse(lines, *, start_time, min_answer_len=10, clock=None) -> StreamResult:
    """requests/Locust 用：data: 前缀解析 + delta.content + [DONE] / chunk.error 处理。"""
    timer = StreamTimer(start_time=start_time, clock=clock)
    parts = []
    fail_category = None
    fail_reason = None

    try:
        for line in lines:
            if not line:
                continue
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded.startswith("data:"):
                continue
            data_str = decoded[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                fail_category = fail_category or "parse_error"
                fail_reason = fail_reason or f"Invalid JSON chunk: {data_str[:100]}"
                continue
            if chunk.get("error"):
                fail_category = fail_category or "stream_error_event"
                fail_reason = fail_reason or str(chunk["error"])[:200]
                continue
            content = extract_openai_delta(chunk)
            if content:
                timer.on_content()
                parts.append(content)
    except Exception as e:
        if fail_category is None:
            fail_category = classify_exception(e)
            fail_reason = str(e)[:200]

    answer = "".join(parts)
    if fail_category is None and len(answer) < min_answer_len:
        fail_category = "incomplete_answer"
        fail_reason = f"回答长度 {len(answer)} < {min_answer_len}"
    return timer.result(answer, fail_category=fail_category, fail_reason=fail_reason)

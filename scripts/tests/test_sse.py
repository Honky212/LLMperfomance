# -*- coding: utf-8 -*-
"""test_sse.py —— llmperf_common.sse 离线单测（P3 落地，仅标准库 unittest）

覆盖（对应方案 3.2 / 4.1 与 P3 验证标准）：
- StreamTimer：TTFB / ITL 样本数=内容块数-1 / 生成速率 / 空样本
- consume_dify_sse：TTFB 只计首个真实内容、conversation_id 只更新不清空、
  Dify 错误事件双格式（修订 B）、pending_event 生命周期、parse_error、incomplete_answer、
  迭代期网络中断兜底归类
- consume_openai_sse：[DONE] 停止、role-only 块不计、chunk.error、parse_error、incomplete、迭代异常
- extract_openai_delta：空/异常结构安全返回 ""
- failed_result：请求建立阶段失败的构造

运行：
  python -m unittest discover -s scripts/tests -t scripts -v   （项目根）
  python scripts/tests/test_sse.py                            （直跑）
"""
import os
import sys
import time
import unittest
from types import SimpleNamespace

# 兼容 IDE/直跑：确保 scripts/ 在 sys.path（`-t scripts` 运行时可省略）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from llmperf_common import sse


class AutoClock:
    """每次调用返回递增时刻（step_ms 步进），供 consume_* 注入确定性的流式时间"""

    def __init__(self, start=1000.0, step_ms=5.0):
        self.t = start
        self.step = step_ms / 1000.0

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def _lines_then_raise(exc):
    def gen():
        yield b'data: {"event": "message", "answer": "hi"}'
        raise exc
    return gen()


class TestStreamTimer(unittest.TestCase):

    def test_ttfb_itl_gen(self):
        t = sse.StreamTimer(start_time=1.0)
        t.on_content(now=1.01)
        t.on_content(now=1.03)
        t.on_content(now=1.05)
        self.assertAlmostEqual(t.ttfb_ms, 10.0, places=6)   # 首个内容 1.01-1.0 → 10ms
        self.assertEqual(t.chunk_count, 3)
        self.assertEqual(len(t.itl_ms), 2)                  # 样本数 = 块数 - 1
        self.assertAlmostEqual(t.itl_ms[0], 20.0, places=6)
        self.assertAlmostEqual(t.itl_ms[1], 20.0, places=6)
        self.assertAlmostEqual(t.gen_chars_per_sec(200), 5000.0, places=6)  # 200字符 / 40ms

    def test_single_chunk_no_itl_no_gen(self):
        t = sse.StreamTimer(start_time=1.0)
        t.on_content(now=1.01)
        self.assertEqual(t.itl_ms, [])
        self.assertIsNone(t.gen_chars_per_sec(5))
        sr = t.result("hi")
        self.assertEqual(sr.chunk_count, 1)
        self.assertAlmostEqual(sr.ttfb_ms, 10.0, places=6)
        self.assertIsNone(sr.fail_category)
        self.assertGreater(sr.elapsed_ms, 0)

    def test_no_content(self):
        t = sse.StreamTimer(start_time=1000.0)
        self.assertIsNone(t.ttfb_ms)
        self.assertEqual(t.chunk_count, 0)
        self.assertIsNone(t.gen_chars_per_sec(0))


class TestConsumeDifySse(unittest.TestCase):

    def _data(self, event, **kw):
        payload = dict(event=event)
        payload.update(kw)
        return f'data: {json_dumps(payload)}'.encode()

    def test_normal_stream(self):
        clock = AutoClock()
        lines = [
            self._data("message", answer="你", conversation_id="c1"),
            self._data("message", answer="好", conversation_id="c1"),
            self._data("message", answer="！"),
        ]
        sr = sse.consume_dify_sse(lines, start_time=clock.t, clock=clock, min_answer_len=1)
        self.assertEqual(sr.answer, "你好！")
        self.assertEqual(sr.conversation_id, "c1")   # 无 cid 的块不清空
        self.assertEqual(sr.ttfb_ms, 0.0)            # 首块即时钟起点
        self.assertEqual(sr.chunk_count, 3)
        self.assertEqual(len(sr.itl_ms), 2)          # 块数 - 1，步进 5ms
        self.assertAlmostEqual(sr.itl_ms[0], 5.0, places=6)
        self.assertAlmostEqual(sr.itl_ms[1], 5.0, places=6)
        self.assertAlmostEqual(sr.gen_chars_per_sec, 300.0, places=6)  # 3字符 / 10ms
        self.assertIsNone(sr.fail_category)

    def test_status_events_not_content(self):
        clock = AutoClock()
        lines = [
            self._data("message_end"),
            self._data("message", answer="好"),
            self._data("ping"),
        ]
        sr = sse.consume_dify_sse(lines, start_time=clock.t, clock=clock, min_answer_len=1)
        self.assertEqual(sr.answer, "好")
        self.assertEqual(sr.chunk_count, 1)          # 空 answer / 非 message 事件不计内容块

    def test_empty_answer_chunk_not_counted(self):
        lines = [self._data("message", answer=""), self._data("message", answer="好")]
        sr = sse.consume_dify_sse(lines, start_time=time.time(), min_answer_len=1)
        self.assertEqual(sr.chunk_count, 1)
        self.assertEqual(sr.answer, "好")

    def test_error_embedded_json(self):
        lines = [self._data("error", message="boom")]
        sr = sse.consume_dify_sse(lines, start_time=time.time())
        self.assertEqual(sr.fail_category, "stream_error_event")
        self.assertIn("boom", sr.fail_reason)

    def test_error_two_line_sse_format(self):
        """修订 B：标准 SSE `event: error` + data 两行格式"""
        lines = [
            b"event: error",
            b'data: {"status": 500, "code": "internal_server_error", "message": "Internal Server Error"}',
        ]
        sr = sse.consume_dify_sse(lines, start_time=time.time())
        self.assertEqual(sr.fail_category, "stream_error_event")
        self.assertIn("Internal Server Error", sr.fail_reason)

    def test_pending_event_lifecycle(self):
        # event: ping 被下一条 data 消费后不污染后续 message 块
        lines = [
            b"event: ping",
            self._data("message", answer="好"),
            self._data("message", answer="！"),
        ]
        sr = sse.consume_dify_sse(lines, start_time=time.time(), min_answer_len=1)
        self.assertEqual(sr.answer, "好！")
        self.assertIsNone(sr.fail_category)
        # 无空格 event:error 写法
        lines2 = [b"event:error", b'data: {"status": 500, "message": "x"}']
        sr2 = sse.consume_dify_sse(lines2, start_time=time.time())
        self.assertEqual(sr2.fail_category, "stream_error_event")

    def test_parse_error(self):
        lines = [b"data: {invalid json", self._data("message", answer="好")]
        sr = sse.consume_dify_sse(lines, start_time=time.time())
        self.assertEqual(sr.fail_category, "parse_error")
        self.assertEqual(sr.answer, "好")

    def test_incomplete_answer(self):
        sr = sse.consume_dify_sse([self._data("message", answer="短")],
                                  start_time=time.time(), min_answer_len=10)
        self.assertEqual(sr.fail_category, "incomplete_answer")

    def test_iteration_network_error(self):
        """迭代期网络中断：解析器兜底归类，不向外抛"""
        sr = sse.consume_dify_sse(_lines_then_raise(ConnectionError("reset")),
                                  start_time=time.time())
        self.assertEqual(sr.fail_category, "connection_error")
        self.assertEqual(sr.answer, "hi")  # 已收到的内容保留


class TestConsumeOpenaiSse(unittest.TestCase):

    def _delta(self, content=None, role=None):
        d = {}
        if content is not None:
            d["content"] = content
        if role is not None:
            d["role"] = role
        return f'data: {json_dumps({"choices": [{"delta": d}]})}'.encode()

    def test_normal_stream_with_done(self):
        lines = [self._delta("你"), self._delta("好"), b"data: [DONE]", self._delta("被截断")]
        sr = sse.consume_openai_sse(lines, start_time=time.time(), min_answer_len=1)
        self.assertEqual(sr.answer, "你好")
        self.assertEqual(sr.chunk_count, 2)
        self.assertIsNone(sr.fail_category)

    def test_role_only_chunk_not_counted(self):
        lines = [self._delta(role="assistant"), self._delta("好")]
        sr = sse.consume_openai_sse(lines, start_time=time.time(), min_answer_len=1)
        self.assertEqual(sr.answer, "好")
        self.assertEqual(sr.chunk_count, 1)

    def test_chunk_error(self):
        lines = [f'data: {json_dumps({"error": "rate limited"})}'.encode()]
        sr = sse.consume_openai_sse(lines, start_time=time.time())
        self.assertEqual(sr.fail_category, "stream_error_event")

    def test_parse_and_incomplete(self):
        sr = sse.consume_openai_sse([b"data: {bad"], start_time=time.time())
        self.assertEqual(sr.fail_category, "parse_error")
        sr2 = sse.consume_openai_sse([self._delta("短")], start_time=time.time(), min_answer_len=10)
        self.assertEqual(sr2.fail_category, "incomplete_answer")

    def test_iteration_network_error(self):
        sr = sse.consume_openai_sse(_lines_then_raise(OSError("reset")),
                                    start_time=time.time())
        self.assertEqual(sr.fail_category, "connection_error")


class TestExtractOpenaiDelta(unittest.TestCase):

    def test_normal(self):
        chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="x"))])
        self.assertEqual(sse.extract_openai_delta(chunk), "x")

    def test_empty_variants(self):
        self.assertEqual(sse.extract_openai_delta(SimpleNamespace(choices=[])), "")
        self.assertEqual(sse.extract_openai_delta(SimpleNamespace(choices=[SimpleNamespace(delta=None)])), "")
        self.assertEqual(sse.extract_openai_delta(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))])), "")
        self.assertEqual(sse.extract_openai_delta(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=""))])), "")
        self.assertEqual(sse.extract_openai_delta(None), "")


class TestItlSummary(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(sse.itl_summary([]), (None, None))

    def test_single_sample(self):
        self.assertEqual(sse.itl_summary([10.0]), (10.0, 10.0))

    def test_multi(self):
        avg, p95 = sse.itl_summary([10.0, 20.0, 30.0])
        self.assertAlmostEqual(avg, 20.0, places=6)
        self.assertAlmostEqual(p95, 30.0, places=6)


class TestFailedResult(unittest.TestCase):

    def test_fields(self):
        start = time.time()
        sr = sse.failed_result("timeout", "read timed out", start_time=start)
        self.assertEqual(sr.fail_category, "timeout")
        self.assertEqual(sr.fail_reason, "read timed out")
        self.assertEqual(sr.answer, "")
        self.assertGreaterEqual(sr.elapsed_ms, 0)


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)

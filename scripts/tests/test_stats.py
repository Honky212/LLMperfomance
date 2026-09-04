# -*- coding: utf-8 -*-
"""test_stats.py —— llmperf_common.stats 离线单测（P2 落地，仅标准库 unittest）

覆盖：
- classify_exception：openai / httpx / requests 全分类 + Timeout 优先于 Connection 顺序
- StatsCollector：分组记录、聚合、失败分类计数、修订 E 的失败轮次样本排除、空样本 None 语义
- save_report_md：失败分布行"仅在有失败时输出"、JSON 链接行
- save_report_json：schema v1 合法性、无样本指标为 null

运行：
  python -m unittest discover -s scripts/tests -t scripts -v   （项目根）
  python scripts/tests/test_stats.py                            （直跑）
"""
import os
import sys
import json
import tempfile
import unittest

# 兼容 IDE/直跑：确保 scripts/ 在 sys.path（`-t scripts` 运行时可省略）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from llmperf_common import stats as st


# ---- 构造"外部库"异常：仅伪造 __module__ / 类名，不依赖第三方包 ----

class FakeOpenAITimeoutError(Exception):
    __module__ = "openai"


class FakeOpenAITimeoutConnectionError(Exception):
    """类名同时含 Timeout 与 Connection，必须判为 timeout（优先级顺序）"""
    __module__ = "openai"


class AuthenticationError(Exception):
    """类名与真实 openai.AuthenticationError 一致（已知状态子类精确匹配）"""
    __module__ = "openai"


class FakeOpenAIAPIError(Exception):
    __module__ = "openai"


class FakeHttpxReadTimeout(Exception):
    __module__ = "httpx"


class FakeHttpxConnectError(Exception):
    __module__ = "httpx"


class FakeHttpxRemoteProtocolError(Exception):
    __module__ = "httpx"


class TestClassifyException(unittest.TestCase):

    def test_requests_real_exceptions(self):
        import requests
        self.assertEqual(st.classify_exception(requests.exceptions.Timeout()), "timeout")
        self.assertEqual(st.classify_exception(requests.exceptions.ConnectTimeout()), "timeout")
        self.assertEqual(st.classify_exception(requests.exceptions.ConnectionError()), "connection_error")
        self.assertEqual(st.classify_exception(requests.exceptions.SSLError()), "connection_error")
        self.assertEqual(st.classify_exception(requests.exceptions.HTTPError()), "http_error")
        self.assertEqual(st.classify_exception(requests.exceptions.RequestException()), "unknown")

    def test_openai_fake_exceptions(self):
        self.assertEqual(st.classify_exception(FakeOpenAITimeoutError()), "timeout")
        self.assertEqual(st.classify_exception(FakeOpenAITimeoutConnectionError()), "timeout")
        self.assertEqual(st.classify_exception(AuthenticationError()), "http_error")
        self.assertEqual(st.classify_exception(FakeOpenAIAPIError()), "unknown")

    def test_httpx_fake_exceptions(self):
        self.assertEqual(st.classify_exception(FakeHttpxReadTimeout()), "timeout")
        self.assertEqual(st.classify_exception(FakeHttpxConnectError()), "connection_error")
        self.assertEqual(st.classify_exception(FakeHttpxRemoteProtocolError()), "connection_error")

    def test_unknown(self):
        self.assertEqual(st.classify_exception(ValueError("x")), "unknown")
        self.assertEqual(st.classify_exception(RuntimeError()), "unknown")


class TestStatsCollector(unittest.TestCase):

    def test_grouped_recording_and_aggregation(self):
        c = st.StatsCollector()
        c.record(st.REQ_NEW, True, ttfb_ms=100.0, latency_ms=1000.0, length=50)
        c.record(st.REQ_NEW, True, ttfb_ms=200.0, latency_ms=2000.0, length=80)
        c.record(st.REQ_CONTINUE, True, ttfb_ms=150.0, latency_ms=1500.0, length=60)
        c.record(st.REQ_CONTINUE, False, ttfb_ms=0.0, latency_ms=3000.0, length=0,
                 fail_category="timeout")
        agg = c.row(st.REQ_AGGREGATED)
        self.assertEqual(agg["total"], 4)
        self.assertEqual(agg["fails"], 1)
        self.assertEqual(len(agg["ttfbs"]), 3)
        self.assertEqual(c.row(st.REQ_NEW)["total"], 2)
        self.assertEqual(c.row(st.REQ_CONTINUE)["fails"], 1)
        self.assertEqual(c.fail_categories(), {"timeout": 1})

    def test_failed_round_new_metrics_excluded(self):
        """修订 E：失败轮次的 itl/gen 样本不进全局聚合"""
        c = st.StatsCollector()
        c.record(st.REQ_NEW, True, ttfb_ms=100.0, latency_ms=1000.0, length=50,
                 itl_ms=[10.0, 20.0], gen_chars_per_sec=5.0)
        c.record(st.REQ_NEW, False, ttfb_ms=0.0, latency_ms=500.0, length=3,
                 itl_ms=[30.0], gen_chars_per_sec=1.0, fail_category="incomplete_answer")
        agg = c.row(st.REQ_AGGREGATED)
        self.assertEqual(agg["itls"], [10.0, 20.0])
        self.assertEqual(agg["gens"], [5.0])

    def test_percentile_empty_is_none(self):
        """JSON null 语义：空样本 _pct 返回 None 而非 0"""
        self.assertIsNone(st.StatsCollector._pct([], 50))
        c = st.StatsCollector()
        m = c._metrics_json(st.REQ_NEW)
        self.assertIsNone(m["ttfb_ms"]["avg"])
        self.assertIsNone(m["itl_ms"]["p95"])
        self.assertIsNone(m["gen_chars_per_sec"]["avg"])

    def test_fail_category_unknown_fallback(self):
        c = st.StatsCollector()
        c.record(st.REQ_NEW, False, ttfb_ms=0.0, latency_ms=10.0, length=0)  # fail_category=None
        self.assertEqual(c.fail_categories(), {"unknown": 1})


def _params():
    return {"users": 2, "threads": 2, "rounds": 2, "sleep_s": 0.0,
            "timeout_s": 120, "corpus": "corpus/test2.txt"}


def _collector_with_failures():
    c = st.StatsCollector()
    c.record(st.REQ_NEW, True, ttfb_ms=100.0, latency_ms=1000.0, length=50)
    c.record(st.REQ_CONTINUE, False, ttfb_ms=0.0, latency_ms=2000.0, length=0,
             fail_category="timeout")
    c.record(st.REQ_CONTINUE, False, ttfb_ms=0.0, latency_ms=2000.0, length=0,
             fail_category="incomplete_answer")
    return c


class TestReportOutput(unittest.TestCase):

    def test_md_failure_distribution_only_when_fails(self):
        """修订 C：失败分布行仅在有失败时输出"""
        with tempfile.TemporaryDirectory() as d:
            # 有失败：应含失败分布行
            md1 = os.path.join(d, "汇总.md")
            c1 = _collector_with_failures()
            st.save_report_md(md1, script="multi_thread_record.py", protocol="dify",
                              endpoint="http://e", model="", params=_params(),
                              wall_time=10.0, stats=c1, json_path=os.path.join(d, "summary_dify_x.json"))
            with open(md1, encoding="utf-8") as f:
                txt1 = f.read()
            self.assertIn("失败分布", txt1)
            self.assertIn("timeout=1", txt1)
            self.assertIn("incomplete_answer=1", txt1)
            self.assertIn("JSON 结果", txt1)
            self.assertIn("summary_dify_x.json", txt1)
            self.assertIn("NewChat", txt1)
            self.assertIn("Aggregated", txt1)

            # 全成功：无失败分布行、无 JSON 链接行
            md2 = os.path.join(d, "汇总2.md")
            c2 = st.StatsCollector()
            c2.record(st.REQ_NEW, True, ttfb_ms=100.0, latency_ms=1000.0, length=50)
            st.save_report_md(md2, script="openai_compat_multi_thread_record.py",
                              protocol="openai_compat", endpoint="http://e", model="m",
                              params=_params(), wall_time=10.0, stats=c2, json_path=None)
            with open(md2, encoding="utf-8") as f:
                txt2 = f.read()
            self.assertNotIn("失败分布", txt2)
            self.assertNotIn("JSON 结果", txt2)
            self.assertIn("OpenAI 兼容", txt2)

    def test_md_contains_g1_metric_rows(self):
        """P4：汇总表含 ITL 与生成速率行（有样本时）"""
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            c = st.StatsCollector()
            c.record(st.REQ_NEW, True, ttfb_ms=100.0, latency_ms=1000.0, length=50,
                     itl_ms=[10.0, 20.0, 30.0], gen_chars_per_sec=12.5)
            md = os.path.join(d, "汇总.md")
            st.save_report_md(md, script="multi_thread_record.py", protocol="dify",
                              endpoint="http://e", model="", params=_params(),
                              wall_time=10.0, stats=c, json_path=None)
            with open(md, encoding="utf-8") as f:
                txt = f.read()
            self.assertIn("| ITL (ms) |", txt)
            self.assertIn("| 生成速率 (字符/秒) |", txt)
            # 控制台汇总含 ITL / 生成速率行
            buf = io.StringIO()
            with redirect_stdout(buf):
                c.print_summary(10.0)
            out = buf.getvalue()
            self.assertIn("ITL(ms)", out)
            self.assertIn("生成速率(字符/s)", out)

    def test_json_schema_and_null_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            c = _collector_with_failures()
            path = st.save_report_json(d, script="multi_thread_record.py", protocol="dify",
                                       endpoint="http://e", model="", params=_params(),
                                       wall_time=10.0, stats=c)
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            self.assertEqual(doc["schema_version"], 1)
            self.assertEqual(doc["protocol"], "dify")
            self.assertEqual(doc["totals"], {"requests": 3, "success": 1, "fails": 2,
                                             "success_rate": 33.3, "rps_total": 0.3,
                                             "rps_success": 0.1})
            self.assertEqual(doc["fail_categories"], {"timeout": 1, "incomplete_answer": 1})
            self.assertEqual(set(doc["by_request_type"]), {"NewChat", "ContinueChat", "Aggregated"})
            # NewChat 有 1 个成功样本 → ttfb avg 有值；无样本指标为 null
            self.assertEqual(doc["by_request_type"]["NewChat"]["metrics"]["ttfb_ms"]["avg"], 100.0)
            self.assertIsNone(doc["by_request_type"]["NewChat"]["metrics"]["itl_ms"]["avg"])
            self.assertIsNone(doc["by_request_type"]["ContinueChat"]["metrics"]["ttfb_ms"]["avg"])
            self.assertEqual(doc["by_request_type"]["Aggregated"]["fails"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

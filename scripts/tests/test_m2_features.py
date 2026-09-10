# -*- coding: utf-8 -*-
"""test_m2_features.py —— M2 新增功能离线单测（HTML 报告 + 规则引擎）

覆盖：
- html_report.generate_report()：离线生成 HTML、含三栏口径表、基线 RPS 标注
- analyze_summary.run_rule_engine()：6 条规则 + null 安全 + 阈值可配
- analyze_summary.format_findings_md()：Markdown 表格格式

运行：python -m unittest discover -s scripts/tests -t scripts -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from llmperf_common import html_report
from analyze_summary import run_rule_engine, format_findings_md, load_thresholds, DEFAULT_THRESHOLDS


# ---- 测试夹具：最小合法 summary JSON ----

def _make_summary(**overrides):
    """构造最小合法 summary doc"""
    doc = {
        "schema_version": 1,
        "timestamp": "2026-09-03T10:00:00",
        "script": "dify_concurrent.py",
        "protocol": "dify",
        "endpoint": "http://test/v1/chat-messages",
        "model": "",
        "params": {"users": 2, "threads": 2, "rounds": 5},
        "wall_time_s": 100.0,
        "totals": {
            "requests": 10, "success": 10, "fails": 0,
            "success_rate": 100.0, "rps_total": 0.1, "rps_success": 0.1,
        },
        "fail_categories": {},
        "by_request_type": {
            "NewChat": {"reqs": 5, "fails": 0, "metrics": {
                "ttfb_ms": {"avg": 500, "p50": 450, "p95": 800, "p99": 900, "min": 200, "max": 1000},
                "latency_ms": {"avg": 2000, "p50": 1800, "p95": 3000, "p99": 3500, "min": 1000, "max": 4000},
                "itl_ms": {"avg": 50, "p50": 45, "p95": 80, "p99": 100, "min": 20, "max": 120},
                "gen_chars_per_sec": {"avg": 30, "p50": 28},
                "response_length": {"avg": 500, "p50": 480, "p95": 800, "max": 1000},
            }},
            "ContinueChat": {"reqs": 5, "fails": 0, "metrics": {
                "ttfb_ms": {"avg": 600, "p50": 550, "p95": 900, "p99": 1000, "min": 300, "max": 1100},
                "latency_ms": {"avg": 2500, "p50": 2300, "p95": 3500, "p99": 4000, "min": 1200, "max": 4500},
                "itl_ms": {"avg": None, "p50": None, "p95": None, "p99": None, "min": None, "max": None},
                "gen_chars_per_sec": {"avg": None, "p50": None},
                "response_length": {"avg": 600, "p50": 580, "p95": 900, "max": 1100},
            }},
            "Aggregated": {"reqs": 10, "fails": 0, "metrics": {
                "ttfb_ms": {"avg": 550, "p50": 500, "p95": 850, "p99": 950, "min": 200, "max": 1100},
                "latency_ms": {"avg": 2250, "p50": 2050, "p95": 3250, "p99": 3750, "min": 1000, "max": 4500},
                "itl_ms": {"avg": 50, "p50": 45, "p95": 80, "p99": 100, "min": 20, "max": 120},
                "gen_chars_per_sec": {"avg": 30, "p50": 28},
                "response_length": {"avg": 550, "p50": 530, "p95": 850, "max": 1100},
            }},
        },
    }
    doc.update(overrides)
    return doc


class TestHtmlReportGeneration(unittest.TestCase):
    """html_report.generate_report 离线生成契约"""

    def test_generates_valid_html(self):
        """生成的 HTML 包含必要元素"""
        doc = _make_summary()
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
            json_path = f.name
        try:
            html_path = html_report.generate_report(json_path)
            self.assertTrue(os.path.isfile(html_path))
            with open(html_path, "r", encoding="utf-8") as hf:
                content = hf.read()
            # 必要元素检查
            self.assertIn("<!DOCTYPE html>", content)
            self.assertIn("echarts", content.lower())
            self.assertIn("指标口径说明", content)  # 三栏口径表
            self.assertIn("NewChat", content)
            self.assertIn("Aggregated", content)
            self.assertIn("100.0%", content)  # 成功率
        finally:
            os.unlink(json_path)
            if os.path.exists(html_path):
                os.unlink(html_path)

    def test_baseline_rps_warning(self):
        """基线脚本 HTML 中 RPS 带警告标注（V2.2 修订 #8）"""
        doc = _make_summary(script="dify_baseline.py")
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
            json_path = f.name
        try:
            html_path = html_report.generate_report(json_path)
            with open(html_path, "r", encoding="utf-8") as hf:
                content = hf.read()
            self.assertIn("仅供参考", content)
            self.assertIn("单线程串行", content)
        finally:
            os.unlink(json_path)
            if os.path.exists(html_path):
                os.unlink(html_path)

    def test_no_fail_pie_when_empty(self):
        """无失败时不渲染饼图"""
        doc = _make_summary()  # fail_categories={}
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
            json_path = f.name
        try:
            html_path = html_report.generate_report(json_path)
            with open(html_path, "r", encoding="utf-8") as hf:
                content = hf.read()
            self.assertNotIn("failPie", content)
        finally:
            os.unlink(json_path)
            if os.path.exists(html_path):
                os.unlink(html_path)

    def test_existing_summaries_generate(self):
        """存量 3 份 summary JSON 均可生成 HTML（回填验证）"""
        results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results")
        summaries = [f for f in os.listdir(results_dir) if f.startswith("summary_") and f.endswith(".json")]
        self.assertGreaterEqual(len(summaries), 3, "应至少有 3 份存量 summary JSON")
        for name in summaries[:3]:
            path = os.path.join(results_dir, name)
            html_path = html_report.generate_report(path)
            self.assertTrue(os.path.isfile(html_path), f"{name} 应生成 HTML")
            size = os.path.getsize(html_path)
            self.assertGreater(size, 1000, f"{name} HTML 应 >1KB")
            os.unlink(html_path)


class TestRuleEngine(unittest.TestCase):
    """analyze_summary.run_rule_engine 规则契约"""

    def test_all_clear_when_healthy(self):
        """所有指标正常时返回 all_clear"""
        doc = _make_summary()
        findings = run_rule_engine(doc, DEFAULT_THRESHOLDS)
        rules = [f["rule"] for f in findings]
        self.assertIn("all_clear", rules)

    def test_low_success_rate_detected(self):
        """成功率低于阈值触发 low_success_rate"""
        doc = _make_summary()
        doc["totals"]["success_rate"] = 80.0
        findings = run_rule_engine(doc, DEFAULT_THRESHOLDS)
        rules = [f["rule"] for f in findings]
        self.assertIn("low_success_rate", rules)

    def test_failures_detected(self):
        """存在失败分类触发 has_failures"""
        doc = _make_summary()
        doc["fail_categories"] = {"timeout": 2, "http_error": 1}
        findings = run_rule_engine(doc, DEFAULT_THRESHOLDS)
        rules = [f["rule"] for f in findings]
        self.assertIn("has_failures", rules)

    def test_high_ttfb_p95_detected(self):
        """TTFB p95 超阈值触发 high_ttfb_p95"""
        doc = _make_summary()
        doc["by_request_type"]["Aggregated"]["metrics"]["ttfb_ms"]["p95"] = 10000
        findings = run_rule_engine(doc, DEFAULT_THRESHOLDS)
        rules = [f["rule"] for f in findings]
        self.assertIn("high_ttfb_p95", rules)

    def test_null_metrics_safe(self):
        """ITL/gen 全 null 时不报错，不触发 itl 规则"""
        doc = _make_summary()
        doc["by_request_type"]["Aggregated"]["metrics"]["itl_ms"] = {
            "avg": None, "p50": None, "p95": None, "p99": None, "min": None, "max": None
        }
        findings = run_rule_engine(doc, DEFAULT_THRESHOLDS)
        rules = [f["rule"] for f in findings]
        self.assertNotIn("high_itl_p95", rules)

    def test_insufficient_samples(self):
        """请求数 <10 触发 insufficient_samples"""
        doc = _make_summary()
        doc["totals"]["requests"] = 3
        findings = run_rule_engine(doc, DEFAULT_THRESHOLDS)
        rules = [f["rule"] for f in findings]
        self.assertIn("insufficient_samples", rules)

    def test_custom_thresholds(self):
        """自定义阈值生效"""
        doc = _make_summary()
        doc["by_request_type"]["Aggregated"]["metrics"]["ttfb_ms"]["p95"] = 600
        # 默认阈值 5000 不触发；改为 500 应触发
        custom = DEFAULT_THRESHOLDS.copy()
        custom["ttfb_p95_ms"] = 500
        findings = run_rule_engine(doc, custom)
        rules = [f["rule"] for f in findings]
        self.assertIn("high_ttfb_p95", rules)

    def test_tail_latency_ratio(self):
        """TTFB p95/avg >3 触发尾部延迟"""
        doc = _make_summary()
        doc["by_request_type"]["Aggregated"]["metrics"]["ttfb_ms"]["avg"] = 100
        doc["by_request_type"]["Aggregated"]["metrics"]["ttfb_ms"]["p95"] = 500
        findings = run_rule_engine(doc, DEFAULT_THRESHOLDS)
        rules = [f["rule"] for f in findings]
        self.assertIn("ttfb_tail_latency", rules)


class TestFindingsFormat(unittest.TestCase):
    """format_findings_md 输出格式"""

    def test_md_contains_table_header(self):
        doc = _make_summary()
        findings = run_rule_engine(doc, DEFAULT_THRESHOLDS)
        md = format_findings_md(findings, doc)
        self.assertIn("| 严重度 | 规则 | 说明 |", md)
        self.assertIn("dify_concurrent.py", md)


class TestAutoPostRun(unittest.TestCase):
    """html_report.auto_post_run 自动入口开关契约"""

    def _write_temp_summary(self, doc=None):
        if doc is None:
            doc = _make_summary()
        f = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8")
        json.dump(doc, f, ensure_ascii=False)
        f.close()
        return f.name

    def test_html_generated_by_default(self):
        """LLMPERF_HTML_REPORT 默认 1，生成 HTML"""
        path = self._write_temp_summary()
        try:
            env = os.environ.copy()
            env.pop("LLMPERF_HTML_REPORT", None)
            env.pop("LLMPERF_ANALYSIS", None)
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                html_report.auto_post_run(path)
            html_path = path.replace(".json", ".html")
            self.assertTrue(os.path.isfile(html_path))
            os.unlink(html_path)
        finally:
            os.unlink(path)

    def test_html_skipped_when_disabled(self):
        """LLMPERF_HTML_REPORT=0 时不生成 HTML"""
        path = self._write_temp_summary()
        try:
            with unittest.mock.patch.dict(os.environ, {"LLMPERF_HTML_REPORT": "0", "LLMPERF_ANALYSIS": "0"}):
                html_report.auto_post_run(path)
            html_path = path.replace(".json", ".html")
            self.assertFalse(os.path.exists(html_path))
        finally:
            os.unlink(path)

    def test_analysis_generated_when_enabled(self):
        """LLMPERF_ANALYSIS=1 时生成 analysis MD"""
        path = self._write_temp_summary()
        try:
            with unittest.mock.patch.dict(os.environ, {"LLMPERF_HTML_REPORT": "0", "LLMPERF_ANALYSIS": "1"}):
                html_report.auto_post_run(path)
            # 检查同目录下有 analysis_*.md
            out_dir = os.path.dirname(path)
            md_files = [f for f in os.listdir(out_dir) if f.startswith("analysis_") and f.endswith(".md")]
            self.assertGreaterEqual(len(md_files), 1)
            for mf in md_files:
                os.unlink(os.path.join(out_dir, mf))
        finally:
            os.unlink(path)

    def test_no_crash_on_missing_file(self):
        """传入不存在的路径时静默返回"""
        html_report.auto_post_run("/nonexistent/path/summary.json")  # 不应抛异常

    def test_no_crash_on_none(self):
        """传入 None 时静默返回"""
        html_report.auto_post_run(None)


if __name__ == "__main__":
    unittest.main()

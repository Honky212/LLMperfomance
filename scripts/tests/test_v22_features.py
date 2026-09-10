# -*- coding: utf-8 -*-
"""test_v22_features.py —— V2.2 新增功能离线单测

覆盖：
- config.resolve_output_dir()：未设置 LLMPERF_OUTPUT_DIR 时回落 PROJECT_ROOT/results；
  设置后返回该值拼接 subdir
- progress.emit()：未激活时零开销（不写 stdout）；激活时输出合法 LLMPERF_PROGRESS 行
- 基线脚本 argparse 默认值与裸跑路径一致性
- StatsCollector 基线全 NewChat 语义（修订 #11）

运行：
  python -m unittest discover -s scripts/tests -t scripts -v
"""
import os
import sys
import io
import json
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from llmperf_common import config
from llmperf_common import progress
from llmperf_common import stats as st


class TestResolveOutputDir(unittest.TestCase):
    """V2.2 §4.5 resolve_output_dir 双路径契约"""

    def test_default_returns_project_results(self):
        """未设置 LLMPERF_OUTPUT_DIR 时返回 PROJECT_ROOT/results"""
        with mock.patch.dict(os.environ, {}, clear=True):
            # 确保环境变量不存在
            os.environ.pop("LLMPERF_OUTPUT_DIR", None)
            result = config.resolve_output_dir()
            expected = os.path.join(config.PROJECT_ROOT, "results")
            self.assertEqual(result, expected)

    def test_default_with_subdir(self):
        """未设置时带 subdir 返回 PROJECT_ROOT/results/<subdir>"""
        os.environ.pop("LLMPERF_OUTPUT_DIR", None)
        result = config.resolve_output_dir("locust")
        expected = os.path.join(config.PROJECT_ROOT, "results", "locust")
        self.assertEqual(result, expected)

    def test_env_var_override(self):
        """设置 LLMPERF_OUTPUT_DIR 时返回该值"""
        custom = "/tmp/test_run_42_20260903_120000"
        with mock.patch.dict(os.environ, {"LLMPERF_OUTPUT_DIR": custom}):
            result = config.resolve_output_dir()
            self.assertEqual(result, custom)

    def test_env_var_override_with_subdir(self):
        """设置 LLMPERF_OUTPUT_DIR 时带 subdir 拼接"""
        custom = "/tmp/test_run_42_20260903_120000"
        with mock.patch.dict(os.environ, {"LLMPERF_OUTPUT_DIR": custom}):
            result = config.resolve_output_dir("locust")
            expected = os.path.join(custom, "locust")
            self.assertEqual(result, expected)

    def test_empty_string_falls_back(self):
        """空字符串视为未设置，回落默认路径"""
        with mock.patch.dict(os.environ, {"LLMPERF_OUTPUT_DIR": ""}):
            result = config.resolve_output_dir()
            expected = os.path.join(config.PROJECT_ROOT, "results")
            self.assertEqual(result, expected)


class TestProgressEmit(unittest.TestCase):
    """V2.2 §4.4 progress.emit 开关与格式契约"""

    def test_disabled_is_noop(self):
        """未激活时 emit 不写 stdout"""
        with mock.patch.object(progress, "_ENABLED", False):
            captured = io.StringIO()
            with mock.patch("sys.stdout", captured):
                progress.emit({"type": "start", "test_type": "baseline"})
            self.assertEqual(captured.getvalue(), "")

    def test_enabled_writes_valid_line(self):
        """激活时输出一行 LLMPERF_PROGRESS <JSON>"""
        with mock.patch.object(progress, "_ENABLED", True):
            captured = io.StringIO()
            with mock.patch("sys.stdout", captured):
                progress.emit({"type": "round_done", "ok": True, "req_type": "NewChat"})
            lines = captured.getvalue().strip().split("\n")
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith("LLMPERF_PROGRESS "))
            payload = json.loads(lines[0][len("LLMPERF_PROGRESS "):])
            self.assertEqual(payload["type"], "round_done")
            self.assertTrue(payload["ok"])

    def test_emit_exception_swallowed(self):
        """emit 内部异常不影响主流程"""
        with mock.patch.object(progress, "_ENABLED", True):
            with mock.patch("sys.stdout") as mock_stdout:
                mock_stdout.write.side_effect = RuntimeError("pipe broken")
                # 不应抛出
                progress.emit({"type": "done"})


class TestBaselineAllNewChat(unittest.TestCase):
    """V2.2 修订 #11：基线脚本所有轮次统一记为 NewChat"""

    def test_baseline_records_all_as_newchat(self):
        """模拟基线脚本 5 轮全部 record 为 NewChat，ContinueChat reqs=0"""
        collector = st.StatsCollector()
        for i in range(5):
            collector.record(st.REQ_NEW, True, ttfb_ms=100.0 * (i + 1),
                             latency_ms=500.0 * (i + 1), length=100,
                             itl_ms=[50.0], gen_chars_per_sec=20.0)
        new_row = collector.row(st.REQ_NEW)
        cont_row = collector.row(st.REQ_CONTINUE)
        agg_row = collector.row(st.REQ_AGGREGATED)

        self.assertEqual(new_row["total"], 5)
        self.assertEqual(cont_row["total"], 0)
        self.assertEqual(agg_row["total"], 5)
        # Aggregated 应与 NewChat 一致（因为 ContinueChat 无数据）
        self.assertEqual(agg_row["fails"], new_row["fails"])


class TestBaselineArgparseDefaults(unittest.TestCase):
    """V2.2 §3.2.1：基线脚本 argparse 默认值与裸跑路径一致"""

    def test_dify_baseline_help_exits_zero(self):
        """dify_baseline.py --help 正常退出且输出含 --corpus/--rounds/--output"""
        import subprocess
        script = os.path.join(config.PROJECT_ROOT, "scripts", "dify_baseline.py")
        result = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "LLM_API_URL": "http://test", "API_KEY": "app-test123456"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--corpus", result.stdout)
        self.assertIn("--rounds", result.stdout)
        self.assertIn("--output", result.stdout)

    def test_openai_compat_help_exits_zero(self):
        """openai_compat_baseline.py --help 正常退出"""
        import subprocess
        script = os.path.join(config.PROJECT_ROOT, "scripts", "openai_compat_baseline.py")
        result = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "OPENAI_BASE_URL": "http://test",
                 "OPENAI_API_KEY": "sk-test123456", "MODEL_NAME": "test"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--corpus", result.stdout)


if __name__ == "__main__":
    unittest.main()

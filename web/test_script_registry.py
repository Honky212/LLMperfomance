# -*- coding: utf-8 -*-
"""script_registry 离线单测（Web 优化 V2.0 Phase 1 产出）。

运行：python web/test_script_registry.py
覆盖：schema 完整性、default_params、validate_params（含 run_time 正则 P2-7）、
build_command（python / locust 两种翻译 + --report 重定向）、resolve_corpora（内置语料）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import script_registry as R  # noqa: E402


class TestRegistrySchema(unittest.TestCase):
    def test_seven_scripts_including_fake(self):
        self.assertEqual(len(R.SCRIPT_REGISTRY), 7)
        self.assertIn("web/fake_script.py", R.SCRIPT_REGISTRY)  # P0-3
        for script, schema in R.SCRIPT_REGISTRY.items():
            for key in ("label", "test_type", "protocol", "launcher", "fields"):
                self.assertIn(key, schema, f"{script} 缺 {key}")
            self.assertIn(schema["launcher"], ("python", "locust"))
            self.assertIn(schema["protocol"], ("dify", "openai_compat", "any"))

    def test_list_scripts_shape(self):
        items = R.list_scripts()
        self.assertEqual(len(items), 7)
        for it in items:
            self.assertIn("fields", it)
            for f in it["fields"]:
                self.assertIn("key", f)


class TestDefaultParams(unittest.TestCase):
    def test_dify_concurrent_defaults(self):
        p = R.default_params("scripts/dify_concurrent.py")
        self.assertEqual(p["users"], 5)
        self.assertEqual(p["corpus"], "corpus/test1.txt")

    def test_fake_defaults(self):
        p = R.default_params("web/fake_script.py")
        self.assertEqual(p["rounds"], 5)


class TestValidateParams(unittest.TestCase):
    def test_ok(self):
        ok, errs = R.validate_params("scripts/dify_concurrent.py",
                                     R.default_params("scripts/dify_concurrent.py"))
        self.assertTrue(ok, errs)

    def test_int_min(self):
        ok, errs = R.validate_params("scripts/dify_concurrent.py", {"rounds": 0})
        self.assertFalse(ok)
        self.assertIn("rounds", errs)

    def test_run_time_pattern(self):  # P2-7
        base = R.default_params("scripts/dify_endurance_locust.py")
        ok, _ = R.validate_params("scripts/dify_endurance_locust.py", dict(base, run_time="30m"))
        self.assertTrue(ok)
        ok, errs = R.validate_params("scripts/dify_endurance_locust.py", dict(base, run_time="abc"))
        self.assertFalse(ok)
        self.assertIn("run_time", errs)

    def test_empty_optional_allowed(self):  # wait_time 留空 = 随机 1~3s
        base = R.default_params("scripts/dify_endurance_locust.py")
        ok, errs = R.validate_params("scripts/dify_endurance_locust.py", dict(base, wait_time=""))
        self.assertTrue(ok, errs)

    def test_bad_corpus(self):
        ok, errs = R.validate_params("scripts/dify_concurrent.py",
                                     {"corpus": "corpus/nope.txt"})
        self.assertFalse(ok)
        self.assertIn("corpus", errs)

    def test_unknown_script(self):
        ok, errs = R.validate_params("scripts/xxx.py", {})
        self.assertFalse(ok)


class TestResolveCorpora(unittest.TestCase):
    def test_builtin_to_abs(self):
        params = R.resolve_corpora("scripts/dify_concurrent.py",
                                   {"corpus": "corpus/test1.txt"})
        abs_path = params["corpus"]
        self.assertTrue(os.path.isabs(abs_path))
        self.assertTrue(os.path.isfile(abs_path))

    def test_abs_idempotent(self):
        p = {"corpus": r"C:\x\corpus\y.txt"}
        self.assertEqual(R.resolve_corpora("scripts/dify_concurrent.py", p)["corpus"],
                         r"C:\x\corpus\y.txt")


class TestBuildCommand(unittest.TestCase):
    def test_fake_env_translation(self):  # P0-3：rounds → env FAKE_ROUNDS，非 CLI
        launcher, args, env = R.build_command("web/fake_script.py", {"rounds": 3}, "results/run_1")
        self.assertEqual(launcher, "python")
        self.assertEqual(args, [])
        self.assertEqual(env, {"FAKE_ROUNDS": "3"})

    def test_python_cli_and_report_redirect(self):  # §5.4：--report 重定向 result_dir
        params = R.resolve_corpora("scripts/dify_concurrent.py",
                                   R.default_params("scripts/dify_concurrent.py"))
        launcher, args, env = R.build_command("scripts/dify_concurrent.py",
                                              params, "results/run_9")
        self.assertEqual(launcher, "python")
        self.assertEqual(args[0], "--corpus")
        self.assertTrue(os.path.isabs(args[1]))
        self.assertIn("--report", args)
        self.assertTrue(args[args.index("--report") + 1].startswith("results/run_9"))

    def test_locust_full_cli(self):
        params = R.default_params("scripts/dify_endurance_locust.py")
        params.update({"users": 2, "spawn_rate": 1, "run_time": "1m", "wait_time": ""})
        params = R.resolve_corpora("scripts/dify_endurance_locust.py", params)
        launcher, args, env = R.build_command("scripts/dify_endurance_locust.py",
                                              params, "results/run_7")
        self.assertEqual(launcher, "locust")
        self.assertEqual(args[:4], ["-f", "scripts/dify_endurance_locust.py", "--headless", "-u"])
        self.assertIn("--html", args)
        self.assertEqual(env["LOCUST_CORPUS"], params["corpus"])
        self.assertNotIn("LOCUST_WAIT_TIME", env)  # 留空不注入
        self.assertEqual(env["LOCUST_TIMEOUT"], "300")

    def test_locust_max_turns(self):
        p = dict(R.default_params("scripts/openai_compat_endurance_locust.py"), max_turns=0)
        _, _, env = R.build_command("scripts/openai_compat_endurance_locust.py",
                                    R.resolve_corpora("scripts/openai_compat_endurance_locust.py", p),
                                    "results/run_1")
        self.assertEqual(env["LOCUST_MAX_TURNS"], "0")

    def test_float_formatting(self):
        params = R.resolve_corpora("scripts/dify_concurrent.py",
                                   dict(R.default_params("scripts/dify_concurrent.py"),
                                        sleep=2.0, timeout=120.5))
        _, args, _ = R.build_command("scripts/dify_concurrent.py", params, "results/run_1")
        self.assertIn("2", args)   # 2.0 → "2"
        self.assertIn("120.5", args)


if __name__ == "__main__":
    unittest.main(verbosity=2)

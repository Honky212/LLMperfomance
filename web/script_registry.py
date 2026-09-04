# -*- coding: utf-8 -*-
"""
web.script_registry —— 脚本参数注册表（Web 优化 V2.0 §4）

单一事实来源：前端表单渲染、后端参数翻译、脚本名→launcher 映射、
协议一致性校验、test_type 推导、语料解析，全部读它。

字段约定（field dict）：
- key      字段键（前端提交 params 的键名 / DB params_json 键名）
- kind     int / float / text / corpus（corpus 渲染为语料下拉）
- target   cli（进 args，需带 flag，如 "--corpus"）
           env（进子进程环境变量，需带 env_name，如 "LOCUST_CORPUS"）
           locust（仅 locust launcher 使用，需带 locust_arg，如 "-u"）
- default  默认值；None 表示"留空 = 走脚本自身缺省"，翻译时跳过
- min/max  int/float 范围；required 语义上由 default 兜底，显式提交非法值才 422

本模块不 import db（保持可离线单测）；语料存在性校验/解析在 resolve_corpora
内做延迟 import（server 进程已把 web/ 与 scripts/ 加入 sys.path）。
"""
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPORA_DIR = PROJECT_ROOT / "web" / "data" / "corpora"

# locust -t 时长格式：数字 + 单单位 s/m/h（V2.0 P2-7）
RUN_TIME_RE = re.compile(r"^\d+[smh]$")

SCRIPT_REGISTRY = {
    # ---- fake 冒烟脚本（V2.0 P0-3：必须入注册表，rounds 走 env 保持 FAKE_ROUNDS 兼容）----
    "web/fake_script.py": {
        "label": "Fake Script (M3 smoke test)",
        "test_type": "concurrent",
        "protocol": "any",
        "launcher": "python",
        "fields": [
            {"key": "rounds", "kind": "int", "label": "轮数", "default": 5,
             "min": 1, "max": 100, "target": "env", "env_name": "FAKE_ROUNDS"},
        ],
    },

    # ---- ① Dify 单线程基线 ----
    "scripts/single_dialog_ttfb.py": {
        "label": "Baseline - Dify（单线程基线 TTFB）",
        "test_type": "baseline",
        "protocol": "dify",
        "launcher": "python",
        "fields": [
            {"key": "corpus", "kind": "corpus", "label": "语料", "default": "corpus/50QA-1.txt",
             "target": "cli", "flag": "--corpus"},
            {"key": "rounds", "kind": "int", "label": "轮数(0=语料全量)", "default": 0,
             "min": 0, "target": "cli", "flag": "--rounds"},
        ],
    },

    # ---- ② OpenAI 单线程基线 ----
    "scripts/openai_compat_dialog_ttfb.py": {
        "label": "Baseline - OpenAI（单线程基线 TTFB）",
        "test_type": "baseline",
        "protocol": "openai_compat",
        "launcher": "python",
        "fields": [
            {"key": "corpus", "kind": "corpus", "label": "语料", "default": "corpus/50QA-1.txt",
             "target": "cli", "flag": "--corpus"},
            {"key": "rounds", "kind": "int", "label": "轮数(0=语料全量)", "default": 0,
             "min": 0, "target": "cli", "flag": "--rounds"},
        ],
    },

    # ---- ③ Dify 多线程并发 ----
    "scripts/multi_thread_record.py": {
        "label": "Concurrent - Dify（多线程并发）",
        "test_type": "concurrent",
        "protocol": "dify",
        "launcher": "python",
        "report_to_result_dir": True,   # --report 重定向 result_dir/压测汇总.md（V2.0 §5.4）
        "fields": [
            {"key": "corpus", "kind": "corpus", "label": "语料", "default": "corpus/50QA-1.txt",
             "target": "cli", "flag": "--corpus"},
            {"key": "users", "kind": "int", "label": "用户总数", "default": 5,
             "min": 1, "target": "cli", "flag": "--users"},
            {"key": "threads", "kind": "int", "label": "并发线程数", "default": 5,
             "min": 1, "target": "cli", "flag": "--threads"},
            {"key": "rounds", "kind": "int", "label": "每人轮数", "default": 10,
             "min": 1, "target": "cli", "flag": "--rounds"},
            {"key": "sleep", "kind": "float", "label": "思考时间(s)", "default": 1.0,
             "min": 0, "target": "cli", "flag": "--sleep"},
            {"key": "timeout", "kind": "float", "label": "单请求超时(s)", "default": 120.0,
             "min": 1, "target": "cli", "flag": "--timeout"},
        ],
    },

    # ---- ④ OpenAI 多线程并发 ----
    "scripts/openai_compat_multi_thread_record.py": {
        "label": "Concurrent - OpenAI（多线程并发）",
        "test_type": "concurrent",
        "protocol": "openai_compat",
        "launcher": "python",
        "report_to_result_dir": True,
        "fields": [
            {"key": "corpus", "kind": "corpus", "label": "语料", "default": "corpus/test1.txt",
             "target": "cli", "flag": "--corpus"},
            {"key": "users", "kind": "int", "label": "用户总数", "default": 5,
             "min": 1, "target": "cli", "flag": "--users"},
            {"key": "threads", "kind": "int", "label": "并发线程数", "default": 5,
             "min": 1, "target": "cli", "flag": "--threads"},
            {"key": "rounds", "kind": "int", "label": "每人轮数", "default": 10,
             "min": 1, "target": "cli", "flag": "--rounds"},
            {"key": "sleep", "kind": "float", "label": "思考时间(s)", "default": 1.0,
             "min": 0, "target": "cli", "flag": "--sleep"},
            {"key": "timeout", "kind": "float", "label": "单请求超时(s)", "default": 600.0,
             "min": 1, "target": "cli", "flag": "--timeout"},
        ],
    },

    # ---- ⑤ Dify 耐力（Locust）----
    "scripts/locust_multi_dialog.py": {
        "label": "Endurance - Dify (Locust)",
        "test_type": "endurance",
        "protocol": "dify",
        "launcher": "locust",
        "fields": [
            {"key": "corpus", "kind": "corpus", "label": "语料", "default": "corpus/50QA-1.txt",
             "target": "env", "env_name": "LOCUST_CORPUS"},
            {"key": "users", "kind": "int", "label": "并发用户数", "default": 50,
             "min": 1, "target": "locust", "locust_arg": "-u"},
            {"key": "spawn_rate", "kind": "int", "label": "启动速率(/s)", "default": 5,
             "min": 1, "target": "locust", "locust_arg": "-r"},
            {"key": "run_time", "kind": "text", "label": "运行时长", "default": "30m",
             "pattern": RUN_TIME_RE, "hint": "如 30m / 2h / 90s",
             "target": "locust", "locust_arg": "-t"},
            {"key": "wait_time", "kind": "float", "label": "思考时间(s，留空=随机1~3)", "default": None,
             "min": 0, "target": "env", "env_name": "LOCUST_WAIT_TIME"},
            {"key": "timeout", "kind": "float", "label": "响应超时(s)", "default": 300.0,
             "min": 1, "target": "env", "env_name": "LOCUST_TIMEOUT"},
        ],
    },

    # ---- ⑥ OpenAI 耐力（Locust）----
    "scripts/openai_compat_locust_multi_dialog.py": {
        "label": "Endurance - OpenAI (Locust)",
        "test_type": "endurance",
        "protocol": "openai_compat",
        "launcher": "locust",
        "fields": [
            {"key": "corpus", "kind": "corpus", "label": "语料", "default": "corpus/test1.txt",
             "target": "env", "env_name": "LOCUST_CORPUS"},
            {"key": "users", "kind": "int", "label": "并发用户数", "default": 50,
             "min": 1, "target": "locust", "locust_arg": "-u"},
            {"key": "spawn_rate", "kind": "int", "label": "启动速率(/s)", "default": 5,
             "min": 1, "target": "locust", "locust_arg": "-r"},
            {"key": "run_time", "kind": "text", "label": "运行时长", "default": "30m",
             "pattern": RUN_TIME_RE, "hint": "如 30m / 2h / 90s",
             "target": "locust", "locust_arg": "-t"},
            {"key": "wait_time", "kind": "float", "label": "思考时间(s，留空=随机1~3)", "default": None,
             "min": 0, "target": "env", "env_name": "LOCUST_WAIT_TIME"},
            {"key": "timeout", "kind": "float", "label": "响应超时(s)", "default": 300.0,
             "min": 1, "target": "env", "env_name": "LOCUST_TIMEOUT"},
            {"key": "max_turns", "kind": "int", "label": "保留历史轮数(0=不限)", "default": 10,
             "min": 0, "target": "env", "env_name": "LOCUST_MAX_TURNS"},
        ],
    },
}


# ---- 基础查询 ----

def get_script(script: str) -> dict | None:
    return SCRIPT_REGISTRY.get(script)


def list_scripts() -> list[dict]:
    """供 GET /api/scripts 与前端下拉；字段仅暴露公共元数据。"""
    out = []
    for key, schema in SCRIPT_REGISTRY.items():
        fields = []
        for f in schema["fields"]:
            meta = {k: f.get(k) for k in
                    ("key", "label", "kind", "default", "min", "max", "hint")}
            if f.get("kind") == "text" and f.get("pattern"):
                meta["pattern"] = f["pattern"].pattern
            fields.append(meta)
        out.append({
            "script": key,
            "label": schema["label"],
            "test_type": schema["test_type"],
            "protocol": schema["protocol"],
            "launcher": schema["launcher"],
            "fields": fields,
        })
    return out


def field_keys(script: str) -> list[str]:
    schema = get_script(script)
    return [f["key"] for f in schema["fields"]] if schema else []


def default_params(script: str) -> dict:
    """每字段默认值组装成 dict（旧请求缺省 params 时兜底；corpus 默认仍为相对路径，解析在 resolve_corpora）。"""
    schema = get_script(script)
    if not schema:
        return {}
    return {f["key"]: f.get("default") for f in schema["fields"]}


def test_type_of(script: str) -> str | None:
    schema = get_script(script)
    return schema["test_type"] if schema else None


def script_protocol(script: str) -> str:
    """dify | openai_compat | any（P1-4 协议一致性校验用）。"""
    schema = get_script(script)
    return schema["protocol"] if schema else "any"


def get_launcher(script: str) -> str:
    schema = get_script(script)
    return schema["launcher"] if schema else "python"


# ---- 参数校验 ----

def _cast(field: dict, value):
    """按 kind 做类型转换与范围校验，返回 (ok, error_msg)。"""
    key, kind = field["key"], field["kind"]
    if kind == "int":
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return False, f"{key}: 需要整数"
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and iv < lo:
            return False, f"{key}: 不能小于 {lo}"
        if hi is not None and iv > hi:
            return False, f"{key}: 不能大于 {hi}"
        return True, None
    if kind == "float":
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return False, f"{key}: 需要数值"
        lo = field.get("min")
        if lo is not None and fv < lo:
            return False, f"{key}: 不能小于 {lo}"
        return True, None
    if kind == "text":
        pat = field.get("pattern")
        if pat and not pat.match(str(value)):
            return False, f"{key}: 格式应为 {field.get('hint', pat.pattern)}"
        return True, None
    if kind == "corpus":
        s = str(value)
        if s.startswith("uploaded:"):
            try:
                int(s[len("uploaded:"):])
            except ValueError:
                return False, f"{key}: 上传语料标识非法"
            return True, None
        if os.path.isabs(s):  # 理论不会发生（解析在提交后），放行
            return True, None
        # 内置语料相对路径需真实存在于 corpus/ 下
        if (PROJECT_ROOT / s).is_file():
            return True, None
        return False, f"{key}: 语料不存在（{s}）"
    return True, None


def validate_params(script: str, params: dict) -> tuple[bool, dict]:
    """按字段 schema 做类型/范围/格式校验；返回 (ok, {field: error_msg})。"""
    schema = get_script(script)
    if not schema:
        return False, {"script": f"未知脚本: {script}"}
    errors = {}
    for f in schema["fields"]:
        key = f["key"]
        value = params.get(key)
        # 留空（None / ""）视为"走脚本缺省"，跳过（default=None 的可选字段）
        if value is None or (isinstance(value, str) and value.strip() == ""):
            continue
        ok, msg = _cast(f, value)
        if not ok:
            errors[key] = msg
    return (not errors), errors


def resolve_corpora(script: str, params: dict) -> dict:
    """把语料字段解析为绝对路径并就地写回 params（V2.0 §5.1-4）。

    - "corpus/xxx.txt"      → PROJECT_ROOT/corpus/xxx.txt（内置）
    - "uploaded:{id}"       → CORPORA_DIR/{stored_name}（上传语料，校验 DB 行存在）
    - 已是绝对路径           → 原样保留（幂等）
    上传语料 id 不存在/非法时抛 ValueError，由调用方转 422。
    """
    schema = get_script(script)
    if not schema:
        return params
    for f in schema["fields"]:
        if f["kind"] != "corpus":
            continue
        key = f["key"]
        value = params.get(key)
        if not value:
            continue
        s = str(value)
        if s.startswith("uploaded:"):
            try:
                cid = int(s[len("uploaded:"):])
            except ValueError:
                raise ValueError(f"{key}: 上传语料标识非法")
            import db  # 延迟 import，保持模块可离线测试
            row = db.get_corpus(cid)
            if not row:
                raise ValueError(f"{key}: 上传语料 #{cid} 不存在")
            params[key] = str(CORPORA_DIR / row["stored_name"])
        elif not os.path.isabs(s):
            params[key] = str(PROJECT_ROOT / s)
    return params


# ---- 参数 → 命令行/环境变量翻译 ----

def _fmt_value(value):
    """命令行为字符串；float 去掉多余的 .0 展示。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def build_command(script: str, params: dict, result_dir: str | os.PathLike) -> tuple[str, list[str], dict]:
    """集中完成参数翻译（V2.0 §4.3 / §5.3）。

    返回 (launcher, cmd_args, env_extra)：
    - launcher=python：cmd_args 为 `python -u script.py` 之后追加的参数；
    - launcher=locust：cmd_args 为 `python -m locust` 之后追加的完整 locust CLI（含 -f/--headless/-u/-r/-t/--html/--csv）；
    - env_extra 为需注入子进程的环境变量（LOCUST_* / FAKE_ROUNDS 等）。
    params 中的语料字段须已由 resolve_corpora 解析为绝对路径。
    """
    schema = get_script(script)
    if not schema:
        raise ValueError(f"未知脚本: {script}")
    launcher = schema["launcher"]
    result_dir = str(result_dir)
    args: list[str] = []
    env_extra: dict = {}

    if launcher == "locust":
        args = ["-f", script, "--headless"]
        for f in schema["fields"]:
            if f["target"] != "locust":
                continue
            value = params.get(f["key"])
            if value is None:
                continue
            args += [f["locust_arg"], _fmt_value(value)]
        args += ["--html", os.path.join(result_dir, "locust_report.html"),
                 "--csv", os.path.join(result_dir, "locust")]
    else:
        for f in schema["fields"]:
            if f["target"] != "cli":
                continue
            value = params.get(f["key"])
            if value is None:
                continue
            args += [f["flag"], _fmt_value(value)]
        if schema.get("report_to_result_dir"):
            # V2.0 §5.4：多线程脚本 --report 统一重定向到 result_dir，避免并发写坏项目根 压测汇总.md
            args += ["--report", os.path.join(result_dir, "压测汇总.md")]

    # env 字段统一翻译（两种 launcher 均适用）
    for f in schema["fields"]:
        if f["target"] != "env":
            continue
        value = params.get(f["key"])
        if value is None or (isinstance(value, str) and value.strip() == ""):
            continue  # 留空 = 走脚本自身缺省（如 LOCUST_WAIT_TIME 随机 1~3s）
        env_extra[f["env_name"]] = _fmt_value(value)

    return launcher, args, env_extra

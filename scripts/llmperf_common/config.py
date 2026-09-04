# -*- coding: utf-8 -*-
"""
llmperf_common.config —— 配置统一加载与校验（仅标准库，零第三方依赖）

收编 6 个脚本各自重复的配置段：
- .env 加载（系统环境变量优先；保留 OPENAI_API_KEY 系统/文件不一致检测警告）
- VERIFY_SSL 解析（必须在 load_env() 之后调用，0831-3 已证实顺序错误的后果）
- API Key 自检（占位 / 重复前缀 → 快速失败 SystemExit）与启动日志脱敏
- 两种协议的配置聚合（require_dify_config / require_openai_config）

用法：
    from llmperf_common import config
    config.load_env()
    VERIFY_SSL = config.parse_verify_ssl()
    cfg = config.require_openai_config()   # {"url", "key", "model", "max_tokens"}
"""
import os

# 项目根目录 = 本文件上三级（scripts/llmperf_common/config.py → scripts/ → 项目根）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_output_dir(subdir: str = "") -> str:
    """统一结果输出目录解析（V2.2 §4.5 修订 #1）。

    - LLMPERF_OUTPUT_DIR 已设置：返回该值（执行器注入时为绝对路径）拼接 subdir；
    - 未设置：返回 PROJECT_ROOT/results/<subdir>，与改造前裸跑默认路径完全一致。

    subdir 示例：""（顶层）、"locust"（Locust 逐用户 CSV 子目录）。
    返回值不带尾部分隔符；调用方自行 os.path.join 文件名。
    """
    base = os.environ.get("LLMPERF_OUTPUT_DIR", "").strip()
    if base:
        return os.path.join(base, subdir) if subdir else base
    return os.path.join(PROJECT_ROOT, "results", subdir) if subdir else os.path.join(PROJECT_ROOT, "results")

_DIFY_KEY_PLACEHOLDER = "app-xxxx"
_OPENAI_KEY_PLACEHOLDER = "sk-xxxx"
_BASE_URL_SUFFIX = "/chat/completions"


def load_env() -> None:
    """加载项目根 .env（保持 load_dotenv 默认行为：不覆盖已存在的系统环境变量）。

    未安装 python-dotenv 时打印提示；保留既有检测：
    系统环境变量 OPENAI_API_KEY 与 .env 文件值不一致时打印警告（系统变量优先）。
    """
    try:
        from dotenv import load_dotenv, dotenv_values
    except ImportError:
        print("提示: 未安装 python-dotenv，不会加载 .env 文件，可执行 pip install python-dotenv")
        return
    _env_file = os.path.join(PROJECT_ROOT, ".env")
    _sys_key_before = os.environ.get("OPENAI_API_KEY")  # 记录加载前的系统环境变量
    _file_key = dotenv_values(_env_file).get("OPENAI_API_KEY")
    load_dotenv(_env_file)
    if _sys_key_before is not None and _file_key and _sys_key_before != _file_key:
        print("警告: 检测到系统环境变量 OPENAI_API_KEY 优先于 .env 文件，当前实际使用的是"
              "系统环境变量中的 Key；如需改用 .env 请先执行 $env:OPENAI_API_KEY=$null (PowerShell)")


def parse_verify_ssl() -> bool:
    """解析 VERIFY_SSL（须在 load_env() 之后调用，否则 .env 中的值不生效）；
    返回 False 时打印醒目警告。"""
    verify = os.environ.get("VERIFY_SSL", "true").strip().lower() not in ("0", "false", "no")
    if not verify:
        print("警告: VERIFY_SSL=false，已禁用 SSL 证书校验，仅适用于本地自签名环境")
    return verify


def mask_key(key: str) -> str:
    """启动日志脱敏：前6 + **** + 后4；过短返回 <过短>。"""
    if len(key) >= 10:
        return key[:6] + "****" + key[-4:]
    return "<过短>"


def normalize_base_url(url: str) -> str:
    """去掉误填的 /chat/completions 后缀（openai SDK 会自动拼接）。"""
    url = (url or "").strip()
    if url.endswith(_BASE_URL_SUFFIX):
        url = url[: -len(_BASE_URL_SUFFIX)]
    return url


# ---- Key / 模型单项校验（Locust 脚本 host 可来自命令行 --host，按需组合使用）----

def check_dify_key(key: str) -> None:
    """校验 Dify API Key：空 / app-xxxx 占位 / app-app- 重复前缀 → SystemExit。"""
    if not key or key.startswith(_DIFY_KEY_PLACEHOLDER):
        raise SystemExit("错误: API_KEY 未配置，请在 .env 中填写")
    if key.startswith("app-app-"):
        raise SystemExit("错误: API_KEY 疑似重复粘贴了 'app-' 前缀（app-app-），请检查 .env")


def check_openai_key(key: str) -> None:
    """校验 OpenAI 兼容 Key：空 / sk-xxxx 占位 / sk-sk- 重复前缀 → SystemExit。"""
    if not key or key.startswith(_OPENAI_KEY_PLACEHOLDER):
        raise SystemExit("错误: OPENAI_API_KEY 未配置，请登录后于「令牌」页面创建并填入 .env")
    if key.startswith("sk-sk-"):
        raise SystemExit("错误: OPENAI_API_KEY 疑似重复粘贴了 'sk-' 前缀，请只保留一个 sk-")


def check_model(model: str) -> None:
    """校验模型名：缺失 → SystemExit。"""
    if not model:
        raise SystemExit("错误: MODEL_NAME 未配置，请在 .env 中填写")


# ---- 配置聚合（含完整自检，快速失败）----

def require_dify_config() -> dict:
    """返回 {"url", "key"}；LLM_API_URL / API_KEY 缺失、占位或重复前缀 → SystemExit。"""
    url = os.environ.get("LLM_API_URL", "").strip()
    if not url:
        raise SystemExit("错误: LLM_API_URL 未配置，请在 .env 中填写")
    key = os.environ.get("API_KEY", "").strip()
    check_dify_key(key)
    return {"url": url, "key": key}


def require_openai_config() -> dict:
    """返回 {"url", "key", "model", "max_tokens"}；缺失/占位/重复前缀 → SystemExit。

    url 已做 /chat/completions 后缀归一；max_tokens 非法时回退 0 并打印警告。
    """
    url = normalize_base_url(os.environ.get("OPENAI_BASE_URL", "").strip())
    if not url:
        raise SystemExit("错误: OPENAI_BASE_URL 未配置，请在 .env 中填写")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    check_openai_key(key)
    model = os.environ.get("MODEL_NAME", "").strip()
    check_model(model)
    try:
        max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "0") or 0)
    except ValueError:
        print("警告: OPENAI_MAX_TOKENS 非法，使用默认值 0（不限制）")
        max_tokens = 0
    return {"url": url, "key": key, "model": model, "max_tokens": max_tokens}

"""OpenAI 兼容协议版：单线程多轮对话 + TTFB（首包时间）性能测试

适用于任何 OpenAI 兼容接口：DeepSeek 官方 API、vLLM、Ollama、
one-api/new-api 令牌代理等。
协议: POST {OPENAI_BASE_URL}/chat/completions（SSE 流式）
多轮: OpenAI 协议无 conversation_id，通过 messages 消息历史维持上下文。
配置: 项目根目录 .env（OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME）
"""
import os
import sys
import time
import argparse
import csv

import httpx

from llmperf_common import config
from llmperf_common import sse
from llmperf_common import stats as stats_mod
from llmperf_common import progress
from llmperf_common.html_report import auto_post_run

config.load_env()

from openai import OpenAI

VERIFY_SSL = config.parse_verify_ssl()

# 配置自检（OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME）由公共模块统一，模块加载时快速失败
_CFG = config.require_openai_config()

client = OpenAI(
    base_url=_CFG["url"],
    api_key=_CFG["key"] or "EMPTY",  # 部分本地服务不校验密钥，但 SDK 要求非空
    timeout=120.0,  # 模型首次调用可能冷启动，给足超时时间
    http_client=httpx.Client(verify=VERIFY_SSL),
)


def chat(messages):
    """发送一轮对话（流式），返回回答文本与 G1 指标"""
    kwargs = {
        "model": _CFG["model"],
        "messages": messages,
        "stream": True,
    }
    if _CFG["max_tokens"] > 0:
        kwargs["max_tokens"] = _CFG["max_tokens"]

    try:
        request_start_time = time.time()
        stream = client.chat.completions.create(**kwargs)

        timer = sse.StreamTimer(start_time=request_start_time)
        result = []
        fail_category = None
        fail_reason = None
        for chunk in stream:
            # 流内 error chunk 检测（兼容 SDK 对象与 dict）：部分兼容网关以
            # error chunk 而非 HTTP 非 200 返回错误；SDK 直接抛异常的路径由下方 except 归类
            err = chunk.get("error") if isinstance(chunk, dict) else getattr(chunk, "error", None)
            if err:
                fail_category = "stream_error_event"
                fail_reason = str(err)[:200]
                break
            delta = sse.extract_openai_delta(chunk)
            if delta:
                timer.on_content()
                result.append(delta)

        answer = "".join(result)
        sr = timer.result(answer)
        return {"answer": answer,
                "ttfb_ms": sr.ttfb_ms,
                "elapsed_ms": sr.elapsed_ms,
                "itl_ms": sr.itl_ms,
                "gen_chars_per_sec": sr.gen_chars_per_sec,
                "chunk_count": sr.chunk_count,
                # 透传流内失败分类：供主循环判定 ok，避免部分内容失败被误计成功
                "fail_category": fail_category,
                "fail_reason": fail_reason}

    except Exception as e:
        print(f"请求失败: {str(e)}")
        return {"answer": "请求失败", "fail_category": stats_mod.classify_exception(e)}


def parse_args():
    p = argparse.ArgumentParser(description="OpenAI 兼容基线 TTFB 测试（单线程多轮对话）")
    p.add_argument("--corpus", default=os.path.join(config.PROJECT_ROOT, "corpus", "test1.txt"),
                   help="语料文件路径（默认 corpus/test1.txt）")
    p.add_argument("--rounds", type=int, default=0,
                   help="最多执行轮数；0 = 语料全量（默认）")
    p.add_argument("--output", default=None,
                   help="逐轮 CSV 完整路径；未指定时走 resolve_output_dir()/openai_dialogue_log.csv")
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()

    # 启动时打印当前生效配置（Key 脱敏），便于排查配置来源问题
    _k_masked = config.mask_key(_CFG["key"])
    print(f"当前配置: base_url={_CFG['url']} | model={_CFG['model']} | key={_k_masked}")

    output_dir = config.resolve_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    csv_path = args.output if args.output else os.path.join(output_dir, "openai_dialogue_log.csv")

    with open(args.corpus, "r", encoding='utf-8') as f:
        questions = [line.strip() for line in f if line.strip()]
    if args.rounds > 0:
        questions = questions[:args.rounds]

    sys_prompt = "请回答总结上述内容。"

    collector = stats_mod.StatsCollector()
    progress.emit({
        "type": "start",
        "script": "openai_compat_baseline.py",
        "protocol": "openai_compat",
        "test_type": "baseline",
        "params": {"corpus": os.path.basename(args.corpus), "rounds": len(questions),
                   "model": _CFG["model"]},
        "expected_rounds": len(questions),
    })

    messages = []
    session_log = []
    wall_start = time.time()

    for idx, question in enumerate(questions, 1):
        full_query = f"{question}\n\n{sys_prompt}"

        print(f'----------- 第 {idx} 轮对话 ------------')
        start_time = time.time()

        messages.append({"role": "user", "content": full_query})
        result = chat(messages)

        answer = result.get("answer", "")
        sr_fail = result.get("fail_category")  # None = 流内无失败（含 error chunk / 异常路径）
        ok = (answer != "请求失败") and (sr_fail is None) and len(answer) >= 10
        if ok:
            messages.append({"role": "assistant", "content": answer})
        else:
            messages.pop()

        fail_cat = None if ok else (sr_fail or result.get("fail_category") or "incomplete_answer")
        ttfb_ms = result.get("ttfb_ms") or 0.0
        elapsed_ms = result.get("elapsed_ms") or ((time.time() - start_time) * 1000)
        itl_ms_list = result.get("itl_ms") or []
        gen = result.get("gen_chars_per_sec")
        length = len(answer) if ok else 0

        # V2.2 修订 #11：基线脚本所有轮次统一记为 NewChat
        collector.record(
            stats_mod.REQ_NEW, ok,
            ttfb_ms=ttfb_ms, latency_ms=elapsed_ms, length=length,
            itl_ms=itl_ms_list if ok else None,
            gen_chars_per_sec=gen if ok else None,
            fail_category=fail_cat,
        )

        progress.emit({
            "type": "round_done",
            "user_id": "baseline",
            "round": idx,
            "ok": ok,
            "req_type": stats_mod.REQ_NEW,
            "ttfb_ms": round(ttfb_ms, 2),
            "elapsed_ms": round(elapsed_ms, 2),
        })

        itl_avg, itl_p95 = sse.itl_summary(itl_ms_list)
        session_log.append({
            "question": question,
            "answer": answer,
            "time": time.time() - start_time,
            "ttfb": (ttfb_ms / 1000) if ttfb_ms else 0,
            "itl_avg_ms": round(itl_avg, 1) if itl_avg is not None else "",
            "itl_p95_ms": round(itl_p95, 1) if itl_p95 is not None else "",
            "gen_chars_per_sec": round(gen, 1) if gen is not None else "",
            "chunk_count": result.get("chunk_count", 0),
        })

        print(f"问题：{question}")
        print(f"回答：{answer}")
        print(f"耗时：{session_log[-1]['time']:.2f}秒")
        print(f"首包时间：{session_log[-1]['ttfb']:.3f}秒")
        _itl_disp = f"ITL：avg={itl_avg:.1f}ms p95={itl_p95:.1f}ms" if itl_avg is not None else "ITL：<样本不足>"
        _gen_disp = f"生成速率：{gen:.1f} 字符/s" if gen is not None else "生成速率：-"
        print(f"{_itl_disp} | {_gen_disp} | 内容块：{session_log[-1]['chunk_count']}")

    with open(csv_path, "w", encoding='utf-8-sig', newline='') as f:
        fieldnames = ["question", "answer", "time", "ttfb",
                      "itl_avg_ms", "itl_p95_ms", "gen_chars_per_sec", "chunk_count"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(session_log)

    summary_path = None
    if os.environ.get("LLMPERF_SUMMARY", "1").strip() != "0":
        wall_time = time.time() - wall_start
        summary_path = stats_mod.save_report_json(
            output_dir,
            script="openai_compat_baseline.py",
            protocol="openai_compat",
            endpoint=_CFG["url"] + "/chat/completions",
            model=_CFG["model"],
            params={"corpus": os.path.basename(args.corpus), "rounds": len(questions)},
            wall_time=wall_time,
            stats=collector,
        )

    # V2.2 §3.3 自动入口
    auto_post_run(summary_path)

    progress.emit({"type": "done", "summary_json": summary_path or ""})

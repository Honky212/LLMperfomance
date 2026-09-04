import os
import sys
import time
import argparse
import requests
import csv

from llmperf_common import config
from llmperf_common import sse
from llmperf_common import stats as stats_mod
from llmperf_common import progress
from llmperf_common.html_report import auto_post_run

# 配置加载与自检（LLM_API_URL / API_KEY）由公共模块统一，模块加载时快速失败
config.load_env()
_CFG = config.require_dify_config()
API_URL = _CFG["url"]
API_KEY = _CFG["key"]


def chat(query, conversation_id=''):
    url = API_URL + "/v1/chat-messages"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + API_KEY,
        "Accept": "text/event-stream"
    }

    payload = {
        "inputs": {},
        "query": query,
        "user": "test-user",
        "conversation_id": conversation_id,
        "response_mode": "streaming"
    }

    try:
        request_start_time = time.time()  # 必须在发请求前计时，TTFB 才包含连接 + 等待响应头的时间
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            stream=True,
            timeout=(3.05, 120)  # 连接超时3.05s，读取超时120s
        )
        response.raise_for_status()

        # P3：SSE 解析统一收敛到 llmperf_common.sse（TTFB 首真实内容 / conversation_id 只更新不清空 /
        # Dify 错误事件双格式 / ITL 与生成速率采集）
        sr = sse.consume_dify_sse(response.iter_lines(), start_time=request_start_time)

        return {"answer": sr.answer,
                "conversation_id": sr.conversation_id,
                "ttfb_ms": sr.ttfb_ms,
                "elapsed_ms": sr.elapsed_ms,
                "itl_ms": sr.itl_ms,
                "gen_chars_per_sec": sr.gen_chars_per_sec,
                "chunk_count": sr.chunk_count,
                # 透传流内失败分类（parse_error / stream_error_event / 迭代期断流等）：
                # 供主循环判定 ok，避免"已收内容>=阈值"的中途失败被误计为成功
                "fail_category": sr.fail_category,
                "fail_reason": sr.fail_reason}

    except requests.exceptions.RequestException as e:
        print(f"请求失败: {str(e)}")
        return {"answer": "请求失败", "fail_category": stats_mod.classify_exception(e)}


def parse_args():
    """argparse 全部提供与现状一致的默认值，裸跑行为不变（V2.2 §3.2.1）。"""
    p = argparse.ArgumentParser(description="Dify 基线 TTFB 测试（单线程多轮对话）")
    p.add_argument("--corpus", default=os.path.join(config.PROJECT_ROOT, "corpus", "50QA-1.txt"),
                   help="语料文件路径（默认 corpus/50QA-1.txt）")
    p.add_argument("--rounds", type=int, default=0,
                   help="最多执行轮数；0 = 语料全量（默认）")
    p.add_argument("--output", default=None,
                   help="逐轮 CSV 完整路径；未指定时走 resolve_output_dir()/dialogue_log0.csv")
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()

    # 输出目录统一走 resolve_output_dir（V2.2 修订 #1）
    output_dir = config.resolve_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    csv_path = args.output if args.output else os.path.join(output_dir, "dialogue_log0.csv")

    # 加载问题
    with open(args.corpus, "r", encoding='utf-8') as f:
        questions = [line.strip() for line in f if line.strip()]
    if args.rounds > 0:
        questions = questions[:args.rounds]

    sys_prompt = "请回答总结上述内容。"

    # StatsCollector + progress.start（V2.2 §3.2.1 / §4.4）
    collector = stats_mod.StatsCollector()
    progress.emit({
        "type": "start",
        "script": "single_dialog_ttfb.py",
        "protocol": "dify",
        "test_type": "baseline",
        "params": {"corpus": os.path.basename(args.corpus), "rounds": len(questions)},
        "expected_rounds": len(questions),
    })

    conversation_id = ''
    session_log = []
    wall_start = time.time()

    for idx, question in enumerate(questions, 1):
        full_query = f"{question}\n\n{sys_prompt}"

        print(f'----------- 第 {idx} 轮对话 ------------')
        start_time = time.time()

        result = chat(full_query, conversation_id)

        if result.get('conversation_id'):
            conversation_id = result['conversation_id']

        # 判定 ok / fail_category：请求层失败（answer=="请求失败"，chat() 已归类）或流内失败
        # （chat() 透传 sr.fail_category，如 parse_error / stream_error_event / 迭代期断流）
        # 或回答过短（<10）均计失败；不再仅凭 answer 长度推断成功
        answer = result.get('answer', '')
        sr_fail = result.get("fail_category")  # None = 流内无失败
        ok = (answer != "请求失败") and (sr_fail is None) and len(answer) >= 10
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

        # progress.round_done
        progress.emit({
            "type": "round_done",
            "user_id": "baseline",
            "round": idx,
            "ok": ok,
            "req_type": stats_mod.REQ_NEW,
            "ttfb_ms": round(ttfb_ms, 2),
            "elapsed_ms": round(elapsed_ms, 2),
        })

        # 逐轮 CSV 记录（保持原有字段与格式）
        itl_avg, itl_p95 = sse.itl_summary(itl_ms_list)
        session_log.append({
            "question": question,
            "answer": answer,
            "time": time.time() - start_time,
            "conversation_id": conversation_id,
            "ttfb": (ttfb_ms / 1000) if ttfb_ms else 0,
            "itl_avg_ms": round(itl_avg, 1) if itl_avg is not None else "",
            "itl_p95_ms": round(itl_p95, 1) if itl_p95 is not None else "",
            "gen_chars_per_sec": round(gen, 1) if gen is not None else "",
            "chunk_count": result.get("chunk_count", 0),
        })

        # 控制台输出（与改造前格式一致）
        print(f"问题：{question}")
        print(f"回答：{answer}")
        print(f"耗时：{session_log[-1]['time']:.2f}秒")
        print(f"会话ID：{conversation_id}\n")
        print(f"首包时间：{session_log[-1]['ttfb']:.3f}秒")
        _itl_disp = f"ITL：avg={itl_avg:.1f}ms p95={itl_p95:.1f}ms" if itl_avg is not None else "ITL：<样本不足>"
        _gen_disp = f"生成速率：{gen:.1f} 字符/s" if gen is not None else "生成速率：-"
        print(f"{_itl_disp} | {_gen_disp} | 内容块：{session_log[-1]['chunk_count']}")

    # 写出逐轮 CSV
    with open(csv_path, "w", encoding='utf-8-sig', newline='') as f:
        fieldnames = ["question", "answer", "time", "ttfb", "conversation_id",
                      "itl_avg_ms", "itl_p95_ms", "gen_chars_per_sec", "chunk_count"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(session_log)

    # summary JSON（受 LLMPERF_SUMMARY 控制，V2.2 修订 #5）
    summary_path = None
    if os.environ.get("LLMPERF_SUMMARY", "1").strip() != "0":
        wall_time = time.time() - wall_start
        summary_path = stats_mod.save_report_json(
            output_dir,
            script="single_dialog_ttfb.py",
            protocol="dify",
            endpoint=API_URL + "/v1/chat-messages",
            model="",
            params={"corpus": os.path.basename(args.corpus), "rounds": len(questions)},
            wall_time=wall_time,
            stats=collector,
        )

    # V2.2 §3.3 自动入口：HTML 报告 + 规则诊断（受环境变量控制）
    auto_post_run(summary_path)

    progress.emit({"type": "done", "summary_json": summary_path or ""})

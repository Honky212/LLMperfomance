# -*- coding: utf-8 -*-
"""M3 假脚本：模拟压测脚本的进度协议输出，用于执行器离线验证。

行为：
1. 发 progress.start
2. 循环 N 轮（默认 5），每轮 sleep 0.3s 后发 progress.round_done
3. 写出伪造 summary JSON 到 LLMPERF_OUTPUT_DIR（或当前目录）
4. 发 progress.done

支持环境变量：
  FAKE_ROUNDS=N        轮数（默认 5）
  FAKE_FAIL_AT=N       第 N 轮发 error 并退出（模拟失败）
  FAKE_HANG=1          发 start 后无限 sleep（模拟取消场景）
  LLMPERF_OUTPUT_DIR   输出目录
"""
import json
import os
import sys
import time


def emit(event):
    line = "LLMPERF_PROGRESS " + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main():
    rounds = int(os.environ.get("FAKE_ROUNDS", "5"))
    fail_at = int(os.environ.get("FAKE_FAIL_AT", "0"))
    hang = os.environ.get("FAKE_HANG", "").strip() == "1"
    output_dir = os.environ.get("LLMPERF_OUTPUT_DIR", ".")

    emit({
        "type": "start",
        "script": "fake_script.py",
        "protocol": "dify",
        "test_type": "concurrent",
        "params": {"rounds": rounds},
        "expected_rounds": rounds,
    })

    if hang:
        # 模拟长任务：发 start 后挂起，等待被取消
        while True:
            time.sleep(1)

    ok_count = 0
    for i in range(1, rounds + 1):
        time.sleep(0.3)

        if fail_at > 0 and i == fail_at:
            emit({"type": "error", "message": f"Simulated failure at round {i}"})
            sys.exit(1)

        ttfb = 100.0 + i * 50
        elapsed = 500.0 + i * 100
        emit({
            "type": "round_done",
            "user_id": "fake_user",
            "round": i,
            "ok": True,
            "req_type": "NewChat" if i == 1 else "ContinueChat",
            "ttfb_ms": round(ttfb, 2),
            "elapsed_ms": round(elapsed, 2),
        })
        ok_count += 1

    # 写伪造 summary
    os.makedirs(output_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    summary = {
        "schema_version": 1,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": "fake_script.py",
        "protocol": "dify",
        "endpoint": "http://fake/v1/chat-messages",
        "model": "fake-model",
        "params": {"rounds": rounds},
        "wall_time_s": round(rounds * 0.3, 1),
        "totals": {
            "requests": ok_count, "success": ok_count, "fails": 0,
            "success_rate": 100.0, "rps_total": round(ok_count / max(rounds * 0.3, 0.1), 3),
            "rps_success": round(ok_count / max(rounds * 0.3, 0.1), 3),
        },
        "fail_categories": {},
        "by_request_type": {
            "NewChat": {"reqs": 1, "fails": 0, "metrics": {
                "ttfb_ms": {"avg": 150, "p50": 150, "p95": 150, "p99": 150, "min": 150, "max": 150},
                "latency_ms": {"avg": 600, "p50": 600, "p95": 600, "p99": 600, "min": 600, "max": 600},
                "itl_ms": {"avg": None, "p50": None, "p95": None, "p99": None, "min": None, "max": None},
                "gen_chars_per_sec": {"avg": None, "p50": None},
                "response_length": {"avg": 100, "p50": 100, "p95": 100, "max": 100},
            }},
            "ContinueChat": {"reqs": max(ok_count - 1, 0), "fails": 0, "metrics": {
                "ttfb_ms": {"avg": 250, "p50": 250, "p95": 350, "p99": 350, "min": 200, "max": 350},
                "latency_ms": {"avg": 800, "p50": 800, "p95": 1000, "p99": 1000, "min": 700, "max": 1000},
                "itl_ms": {"avg": None, "p50": None, "p95": None, "p99": None, "min": None, "max": None},
                "gen_chars_per_sec": {"avg": None, "p50": None},
                "response_length": {"avg": 100, "p50": 100, "p95": 100, "max": 100},
            }},
            "Aggregated": {"reqs": ok_count, "fails": 0, "metrics": {
                "ttfb_ms": {"avg": 230, "p50": 230, "p95": 330, "p99": 330, "min": 150, "max": 350},
                "latency_ms": {"avg": 760, "p50": 760, "p95": 960, "p99": 960, "min": 600, "max": 1000},
                "itl_ms": {"avg": None, "p50": None, "p95": None, "p99": None, "min": None, "max": None},
                "gen_chars_per_sec": {"avg": None, "p50": None},
                "response_length": {"avg": 100, "p50": 100, "p95": 100, "max": 100},
            }},
        },
    }
    summary_path = os.path.join(output_dir, f"summary_fake_{ts}.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    emit({"type": "done", "summary_json": summary_path})
    print(f"[fake] Done. Summary: {summary_path}")


if __name__ == "__main__":
    main()

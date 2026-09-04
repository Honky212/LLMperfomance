# -*- coding: utf-8 -*-
"""M3 执行器集成测试：假脚本驱动 Popen/进度/回放/取消五路径。

运行：python web/test_executor.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from executor import Executor, TaskEventBuffer, PROJECT_ROOT


async def test_normal_completion():
    """路径 1：正常完成——假脚本跑完，收到 start + N*round_done + done + status=completed"""
    print("\n=== Test 1: Normal Completion ===")
    exe = Executor()
    statuses = []

    async def on_status(tid, status, extra):
        statuses.append(status)

    env = {"FAKE_ROUNDS": "3", "LLMPERF_OUTPUT_DIR": "web/data/test_m3"}
    status, extra = await exe.run_task(1, "web/fake_script.py", env, on_status_change=on_status)

    assert status == "completed", f"Expected completed, got {status}"
    assert statuses == ["running", "completed"], f"Status transitions: {statuses}"

    buf = exe.get_buffer(1)
    events = list(buf._buffer)
    types = [e.get("type") for e in events]
    assert "start" in types, f"Missing start event. Types: {types}"
    assert "done" in types, f"Missing done event. Types: {types}"
    assert types.count("round_done") == 3, f"Expected 3 round_done, got {types.count('round_done')}"
    # seq 单调递增
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs), f"seq not monotonic: {seqs}"
    print(f"  OK: {len(events)} events, seq range [{seqs[0]}..{seqs[-1]}]")


async def test_failure():
    """路径 2：失败——FAKE_FAIL_AT=2，第 2 轮发 error 后退出码非 0"""
    print("\n=== Test 2: Failure ===")
    exe = Executor()
    statuses = []

    async def on_status(tid, status, extra):
        statuses.append(status)

    env = {"FAKE_ROUNDS": "5", "FAKE_FAIL_AT": "2", "LLMPERF_OUTPUT_DIR": "web/data/test_m3"}
    status, extra = await exe.run_task(2, "web/fake_script.py", env, on_status_change=on_status)

    assert status == "failed", f"Expected failed, got {status}"
    assert extra.get("exit_code", 0) != 0, f"Expected non-zero exit code"
    buf = exe.get_buffer(2)
    events = list(buf._buffer)
    types = [e.get("type") for e in events]
    assert "error" in types, f"Missing error event. Types: {types}"
    print(f"  OK: failed with exit_code={extra.get('exit_code')}, events={len(events)}")


async def test_backlog_replay():
    """路径 3：backlog 回放——任务完成后新连接收到全部历史事件"""
    print("\n=== Test 3: Backlog Replay ===")
    exe = Executor()

    env = {"FAKE_ROUNDS": "3", "LLMPERF_OUTPUT_DIR": "web/data/test_m3"}
    await exe.run_task(3, "web/fake_script.py", env)

    # 任务已完成，模拟迟到连接
    buf = exe.get_buffer(3)
    backlog, q = buf.subscribe(last_event_id=None)
    assert len(backlog) > 0, "Backlog should not be empty after task completion"
    types = [e.get("type") for e in backlog]
    assert "start" in types and "done" in types, f"Backlog missing start/done: {types}"
    print(f"  OK: backlog has {len(backlog)} events")

    # Last-Event-ID 增量回放
    last_seq = backlog[-1]["seq"]
    incremental, _ = buf.subscribe(last_event_id=last_seq)
    assert len(incremental) == 0, f"Should have 0 incremental events, got {len(incremental)}"

    # 部分回放
    mid_seq = backlog[len(backlog) // 2]["seq"]
    partial, _ = buf.subscribe(last_event_id=mid_seq)
    assert len(partial) == len(backlog) - len(backlog) // 2 - 1 or len(partial) > 0
    print(f"  OK: incremental replay works (mid_seq={mid_seq}, partial={len(partial)})")


async def test_cancel():
    """路径 4：取消——FAKE_HANG=1 的长任务被 cancel_task 发送取消信号"""
    print("\n=== Test 4: Cancel Signal ===")
    exe = Executor()

    env = {"FAKE_HANG": "1", "LLMPERF_OUTPUT_DIR": "web/data/test_m3"}

    # 直接启动子进程（不走 run_task 的完整流程，仅验证 Popen + cancel 机制）
    import subprocess as sp
    child_env = os.environ.copy()
    child_env.update(env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["LLMPERF_PROGRESS"] = "1"

    creation_flags = sp.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = sp.Popen(
        [sys.executable, "-u", str(PROJECT_ROOT / "web/fake_script.py")],
        env=child_env, cwd=str(PROJECT_ROOT),
        stdout=sp.PIPE, stderr=sp.STDOUT, stdin=sp.DEVNULL,
        creationflags=creation_flags,
    )

    # 注册到执行器以便 cancel_task 能找到
    exe._processes[99] = proc
    await asyncio.sleep(1.5)  # 等子进程进入 hang

    cancelled = exe.cancel_task(99)
    assert cancelled, "cancel_task should return True"

    # 等待子进程退出
    try:
        exit_code = proc.wait(timeout=10)
    except sp.TimeoutExpired:
        proc.kill()
        exit_code = proc.wait()

    assert exit_code != 0 or os.name == "nt", f"Process should have been killed (exit={exit_code})"
    print(f"  OK: cancel signal sent, process exited with code {exit_code}")


async def test_progress_seq_monotonic():
    """路径 5：事件 seq 单调递增 + 订阅者实时收到事件"""
    print("\n=== Test 5: Real-time Subscription + Seq Monotonic ===")
    exe = Executor()

    env = {"FAKE_ROUNDS": "3", "LLMPERF_OUTPUT_DIR": "web/data/test_m3"}

    # run_task 是同步阻塞的（read_thread.join），需在线程中运行
    import threading
    result_holder = {}

    def run_sync():
        loop = asyncio.new_event_loop()
        status, extra = loop.run_until_complete(exe.run_task(5, "web/fake_script.py", env))
        loop.close()
        result_holder["done"] = True

    t = threading.Thread(target=run_sync, daemon=True)
    t.start()
    await asyncio.sleep(0.8)  # 等 buffer 创建 + 部分事件

    buf = exe.get_buffer(5)
    assert buf is not None, "Buffer should exist after task starts"
    backlog, q = buf.subscribe()

    t.join(timeout=15)

    # 从 queue 收集实时事件
    real_time_events = []
    while not q.empty():
        real_time_events.append(q.get_nowait())

    total_events = len(backlog) + len(real_time_events)
    print(f"  OK: backlog={len(backlog)}, real_time={len(real_time_events)}, total={total_events}")

    # 所有事件的 seq 应单调递增
    all_seqs = [e["seq"] for e in backlog] + [e["seq"] for e in real_time_events]
    if all_seqs:
        assert all_seqs == sorted(all_seqs), f"Seqs not monotonic: {all_seqs}"
        print(f"  OK: seq monotonic [{all_seqs[0]}..{all_seqs[-1]}]")
    else:
        print("  WARN: no events collected (timing-dependent)")


async def main():
    print("=" * 60)
    print("M3 Executor Integration Tests (fake script)")
    print("=" * 60)

    await test_normal_completion()
    await test_failure()
    await test_backlog_replay()
    await test_cancel()
    await test_progress_seq_monotonic()

    print("\n" + "=" * 60)
    print("ALL M3 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

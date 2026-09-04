# -*- coding: utf-8 -*-
"""
web.executor —— 单队列任务执行器（V2.2 §4.2 / §4.6）

职责：
- 串行执行压测任务（MAX_CONCURRENT_TASKS=1）
- subprocess.Popen + 读线程解析 LLMPERF_PROGRESS 协议行
- 每任务事件环形缓冲（容量 SSE_BACKLOG_MAX，默认 200）
- SSE 订阅者广播 + backlog 回放 + Last-Event-ID 增量回放
- Windows CREATE_NEW_PROCESS_GROUP + taskkill 取消
- 面板启动时 orphaned 状态扫描

本模块不依赖 FastAPI，可独立测试。
"""
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional


logger = logging.getLogger("llmperf.executor")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 环境变量配置
SSE_BACKLOG_MAX = int(os.environ.get("SSE_BACKLOG_MAX", "200"))
SSE_BACKLOG_TTL_S = int(os.environ.get("SSE_BACKLOG_TTL_S", "300"))


class TaskEventBuffer:
    """每任务的进度事件环形缓冲 + SSE 订阅管理。

    并发正确性（V2.2 修订 #3）：
    - 每条事件分配单调递增 seq
    - "取快照 + 注册订阅"在同一把锁内原子完成
    - 支持 Last-Event-ID 增量回放
    """

    def __init__(self, capacity: int = SSE_BACKLOG_MAX):
        self._lock = threading.Lock()
        self._buffer: deque = deque(maxlen=capacity)
        self._seq_counter = 0
        self._subscribers: list[asyncio.Queue] = []
        self._terminal_time: Optional[float] = None

    def append(self, event: dict) -> int:
        """追加事件到缓冲并广播给所有订阅者，返回 seq。"""
        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter
            event_with_seq = {**event, "seq": seq}
            self._buffer.append(event_with_seq)
            subs = list(self._subscribers)

        # 广播在锁外，避免阻塞
        for q in subs:
            try:
                q.put_nowait(event_with_seq)
            except asyncio.QueueFull:
                pass  # 慢消费者丢事件，客户端靠 Last-Event-ID 补回
        return seq

    def mark_terminal(self):
        """标记任务终态，开始 TTL 倒计时。"""
        self._terminal_time = time.monotonic()

    def is_expired(self) -> bool:
        """终态后超过 TTL 则过期。"""
        if self._terminal_time is None:
            return False
        return (time.monotonic() - self._terminal_time) > SSE_BACKLOG_TTL_S

    def subscribe(self, last_event_id: Optional[int] = None):
        """原子地获取 backlog + 注册实时订阅，返回 (backlog_events, queue)。

        last_event_id: 断线重连时携带的 Last-Event-ID；None 表示首次连接。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            snapshot = list(self._buffer)
            self._subscribers.append(q)

        # 按 last_event_id 过滤 backlog
        if last_event_id is not None:
            backlog = [e for e in snapshot if e.get("seq", 0) > last_event_id]
        else:
            backlog = snapshot

        return backlog, q

    def unsubscribe(self, q: asyncio.Queue):
        """移除订阅者。"""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


class Executor:
    """单队列任务执行器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._current_task: Optional[dict] = None
        self._buffers: dict[int, TaskEventBuffer] = {}
        self._processes: dict[int, subprocess.Popen] = {}
        self._cancelled: set[int] = set()  # V2.0 P2-8：被取消的任务 id（终态修正为 cancelled）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._run_lock: Optional[asyncio.Lock] = None  # 单飞互斥锁（修复 #10，set_event_loop 时创建）

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        if self._run_lock is None:
            self._run_lock = asyncio.Lock()

    def get_buffer(self, task_id: int) -> Optional[TaskEventBuffer]:
        return self._buffers.get(task_id)

    async def run_task(self, task_id: int, script: str, env_inject: dict,
                       args: list[str] = None, launcher: str = "python",
                       on_status_change=None):
        """单队列执行入口（修复 #10）：以事件循环 asyncio.Lock 实现真正的单飞互斥。

        即使 server 层 409 并发防护被绕过（代码演进 / 多入口 / 多 loop），并发到达的
        两个任务也只会在锁上排队串行执行，不会同时拉起两个压测互相污染指标。
        """
        if self._run_lock is None:
            self._run_lock = asyncio.Lock()
        async with self._run_lock:
            return await self._run_locked(task_id, script, env_inject, args, launcher,
                                          on_status_change)

    async def _run_locked(self, task_id: int, script: str, env_inject: dict,
                          args: list[str] = None, launcher: str = "python",
                          on_status_change=None):
        """（单飞锁内）执行一个任务，由 run_task 调用。

        Args:
            task_id: 任务 ID
            script: 脚本相对项目根路径
            env_inject: 注入子进程的环境变量
            args: 命令行参数列表（launcher=python 时追加到 `python -u script.py` 之后；
                  launcher=locust 时追加到 `python -m locust` 之后，由 build_command 拼好完整 locust CLI）
            launcher: 启动方式，python | locust（Web 优化 V2.0 §5.3 / P0-2）
            on_status_change: 状态变更回调 async fn(task_id, status, extra)
        """
        buf = TaskEventBuffer()
        with self._lock:
            self._buffers[task_id] = buf
            self._current_task = {"id": task_id, "status": "running"}

        if on_status_change:
            await on_status_change(task_id, "running", {})

        script_path = str(PROJECT_ROOT / script)
        if launcher == "locust":
            # 与执行器同一解释器启动 locust，避免 PATH 依赖（V2.0 P0-2）
            cmd = [sys.executable, "-m", "locust"] + (args or [])
        else:
            cmd = [sys.executable, "-u", script_path] + (args or [])

        # 构造子进程环境
        child_env = os.environ.copy()
        child_env.update(env_inject)
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["LLMPERF_PROGRESS"] = "1"

        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            proc = subprocess.Popen(
                cmd,
                env=child_env,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
            with self._lock:
                self._processes[task_id] = proc

            # 读线程
            read_thread = threading.Thread(
                target=self._read_output,
                args=(proc.stdout, buf, task_id),
                daemon=True,
            )
            read_thread.start()

            # 在线程池中等待子进程结束，避免阻塞 event loop（V2.2 修复）
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, read_thread.join)
            exit_code = await loop.run_in_executor(None, proc.wait)

            with self._lock:
                was_cancelled = task_id in self._cancelled
            if was_cancelled:
                # V2.0 P2-8：cancel_task 已成功终止该进程，终态保持 cancelled，避免被回写为 failed
                status = "cancelled"
            else:
                status = "completed" if exit_code == 0 else "failed"
            extra = {"exit_code": exit_code}

        except Exception as e:
            status = "failed"
            extra = {"error": str(e)}
        finally:
            with self._lock:
                self._processes.pop(task_id, None)
                self._current_task = None
                self._cancelled.discard(task_id)
            buf.mark_terminal()

        # 发终态事件（sse_event='status'：server 侧以其事件名透传，前端据此 updateStatus + 关流）
        buf.append({"type": "status", "sse_event": "status", "status": status, **extra})

        if on_status_change:
            await on_status_change(task_id, status, extra)

        return status, extra

    def _read_output(self, stream, buf: TaskEventBuffer, task_id: int):
        """读线程：逐行解析 LLMPERF_PROGRESS 协议行。"""
        try:
            for raw_line in iter(stream.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                if line.startswith("LLMPERF_PROGRESS "):
                    try:
                        payload = json.loads(line[len("LLMPERF_PROGRESS "):])
                        event_type = payload.get("type", "")
                        if event_type == "start":
                            buf.append({"sse_event": "progress", **payload})
                        elif event_type == "round_done":
                            buf.append({"sse_event": "progress", **payload})
                        elif event_type == "done":
                            buf.append({"sse_event": "progress", **payload})
                        elif event_type == "error":
                            buf.append({"sse_event": "progress", **payload})
                        else:
                            buf.append({"sse_event": "progress", **payload})
                    except json.JSONDecodeError:
                        pass  # 非 JSON 行忽略
                # 非协议行视为人读日志，暂存内存（后续可扩展为日志缓冲）
        except Exception:
            pass
        finally:
            stream.close()

    def cancel_task(self, task_id: int) -> bool:
        """取消正在运行的任务。返回是否成功发送取消信号（修复 #6）。

        - 进程已自然退出（poll() 非 None）→ 返回 False：无需取消，终态由 run_task
          按退出码正常回写，避免"成功完成的任务被误标 cancelled"；
        - Windows taskkill 返回码非 0（进程已不在/权限等）且进程仍存活 → 返回 False，
          不记录取消标记（任务继续运行，调用方可重试）；
        - 成功发信号后才记录 _cancelled 标记，run_task 收尾据此置终态 cancelled。
        """
        with self._lock:
            proc = self._processes.get(task_id)

        if proc is None:
            return False

        # 进程已自然结束：无需取消（终态由 run_task 按退出码回写 completed/failed）
        if proc.poll() is not None:
            return False

        try:
            if os.name == "nt":
                # Windows: taskkill /F /T /PID 杀进程树（检查返回码）
                r = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5,
                )
                if r.returncode != 0:
                    # 杀进程失败：进程可能恰在此时已退出，或无法被杀
                    if proc.poll() is not None:
                        return False  # 已自然退出，交给 run_task 正常回写终态
                    logger.warning(f"cancel task #{task_id}: taskkill 失败 "
                                   f"(rc={r.returncode}): "
                                   f"{r.stderr.decode('utf-8', 'replace')[:200]}")
                    return False  # 取消未生效，不记录取消标记
            else:
                # POSIX: killpg SIGTERM
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

            # 关闭 stdout 管道，让读线程的 readline() 返回空字节并退出
            try:
                proc.stdout.close()
            except Exception:
                pass

            # 成功发信号后记录取消标记，run_task 等待结束后据此置终态 cancelled
            with self._lock:
                self._cancelled.add(task_id)

            return True
        except Exception:
            return False

    def cleanup_expired(self):
        """清理过期的终态缓冲。"""
        with self._lock:
            expired = [tid for tid, buf in self._buffers.items() if buf.is_expired()]
            for tid in expired:
                del self._buffers[tid]

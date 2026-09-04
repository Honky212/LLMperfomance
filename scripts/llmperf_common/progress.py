# -*- coding: utf-8 -*-
"""
llmperf_common.progress —— LLMPERF_PROGRESS 进度协议埋点（V2.2 §4.4）

激活条件：环境变量 LLMPERF_PROGRESS=1
行格式：LLMPERF_PROGRESS <紧凑 JSON>（单行，UTF-8，写入 stdout）
未激活时 emit() 为空操作，零开销。

事件类型与必填字段见 V2.2 附录 A；start 事件须含 test_type + expected_rounds
（endurance 类型为 null），前端据此切换百分比/累计计数展示。
"""
import json
import os
import sys

_ENABLED = os.environ.get("LLMPERF_PROGRESS", "").strip() == "1"


def emit(event: dict) -> None:
    """向 stdout 输出一行 LLMPERF_PROGRESS 协议行；未启用时立即返回。

    调用方保证 event 是 JSON 可序列化的 dict；本函数不做字段校验，
    字段契约由调用方按 V2.2 附录 A 遵守。
    """
    if not _ENABLED:
        return
    try:
        line = "LLMPERF_PROGRESS " + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        # flush 保证管道下实时到达读线程；python -u 已禁用块缓冲，此处双保险
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        # 进度协议是旁路观测，任何异常不得影响主流程
        pass

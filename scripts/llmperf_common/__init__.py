# -*- coding: utf-8 -*-
"""llmperf_common —— LLMperfomance 公共模块（仅标准库，零第三方依赖）。

包含 config / sse / stats 三个子模块，供 scripts/ 下 6 个测试脚本复用：
- config: .env 加载、VERIFY_SSL、Key 自检/脱敏、配置聚合（P1 已落地）
- sse:    SSE 解析 + 流式计时（ITL / 生成速率采集，P3 落地）
- stats:  统一 StatsCollector + 失败分类 + MD/JSON 报告（P2 落地）
"""

# -*- coding: utf-8 -*-
"""scripts/tests —— llmperf_common 离线单测包（仅标准库 unittest）。

存在 __init__.py 以便 `python -m unittest discover -s scripts/tests -t scripts` 以包方式发现；
测试文件头部另有 sys.path 注入，支持 `python scripts/tests/test_*.py` 直跑。
"""

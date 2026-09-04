# -*- coding: utf-8 -*-
"""
analyze_summary.py —— 智能诊断脚本（V2.2 §3.4）

两层设计：
1. 规则引擎层（必跑、离线、零成本）：读取 summary JSON，按可配置阈值输出确定性发现
2. LLM 叙述层（可选 --with-llm）：把规则发现 + summary 交给 OpenAI 兼容接口生成自然语言诊断

用法:
  python scripts/analyze_summary.py results/summary_dify_xxx.json
  python scripts/analyze_summary.py results/summary_dify_xxx.json --with-llm
  python scripts/analyze_summary.py results/summary_dify_xxx.json --thresholds scripts/analysis_thresholds.json

输出: results/analysis_<时间戳>.md
"""
import argparse
import json
import os
import sys
from datetime import datetime

# 确保 llmperf_common 可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llmperf_common import config
from llmperf_common import stats as perf_stats

# 默认阈值（V2.2 §3.4：不硬编码，可通过配置文件覆盖）
DEFAULT_THRESHOLDS = {
    "ttfb_p95_ms": 5000,
    "success_rate_min": 95,
    "itl_p95_ms": 500,
    # 注（修复 #14）：V2.2 草案中的 "ttfb_degradation_percent"（TTFB 随时间退化检测）从未实现——
    # summary JSON 无跨时段时序样本，规则引擎无从比较；此前仅声明未消费，属死配置，已删除。
    # 若后续接入逐轮 CSV 时序分析，可重新引入并实现对应规则。
}


def load_thresholds(path: str = None) -> dict:
    """加载阈值配置；文件不存在或路径为空时使用内置默认"""
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("thresholds", DEFAULT_THRESHOLDS)
    return DEFAULT_THRESHOLDS.copy()


def _safe_get(doc: dict, *keys, default=None):
    """null 安全嵌套取值"""
    cur = doc
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def run_rule_engine(doc: dict, thresholds: dict) -> list:
    """规则引擎：返回 findings 列表，每项 {"rule", "severity", "message", "value"}

    全程 null 安全：无样本指标按"无数据"处理而不是报错。
    """
    findings = []
    totals = doc.get("totals", {})
    agg = doc.get("by_request_type", {}).get("Aggregated", {})
    agg_metrics = agg.get("metrics", {})

    # 规则 1：成功率低于阈值
    success_rate = totals.get("success_rate")
    if success_rate is not None:
        min_rate = thresholds.get("success_rate_min", 95)
        if success_rate < min_rate:
            findings.append({
                "rule": "low_success_rate",
                "severity": "high",
                "message": f"成功率 {success_rate}% 低于阈值 {min_rate}%",
                "value": success_rate,
            })

    # 规则 2：存在失败分类
    fail_cats = doc.get("fail_categories", {})
    if fail_cats:
        total_fails = sum(fail_cats.values())
        cat_desc = "; ".join(
            f"{perf_stats.FAIL_CATEGORIES.get(k, k)}: {v}"
            for k, v in sorted(fail_cats.items(), key=lambda x: -x[1])
        )
        findings.append({
            "rule": "has_failures",
            "severity": "medium" if total_fails <= 3 else "high",
            "message": f"共 {total_fails} 次失败，分类: {cat_desc}",
            "value": fail_cats,
        })

    # 规则 3：TTFB p95 超过阈值
    ttfb_p95 = _safe_get(agg_metrics, "ttfb_ms", "p95")
    if ttfb_p95 is not None:
        threshold = thresholds.get("ttfb_p95_ms", 5000)
        if ttfb_p95 > threshold:
            findings.append({
                "rule": "high_ttfb_p95",
                "severity": "medium",
                "message": f"TTFB p95 = {ttfb_p95:.0f}ms，超过阈值 {threshold}ms",
                "value": ttfb_p95,
            })

    # 规则 4：ITL p95 超过阈值
    itl_p95 = _safe_get(agg_metrics, "itl_ms", "p95")
    if itl_p95 is not None:
        threshold = thresholds.get("itl_p95_ms", 500)
        if itl_p95 > threshold:
            findings.append({
                "rule": "high_itl_p95",
                "severity": "medium",
                "message": f"ITL p95 = {itl_p95:.0f}ms，超过阈值 {threshold}ms（token 间延迟偏高）",
                "value": itl_p95,
            })

    # 规则 5：TTFB p95/avg 比值过大（尾部延迟）
    ttfb_avg = _safe_get(agg_metrics, "ttfb_ms", "avg")
    if ttfb_p95 is not None and ttfb_avg is not None and ttfb_avg > 0:
        ratio = ttfb_p95 / ttfb_avg
        if ratio > 3.0:
            findings.append({
                "rule": "ttfb_tail_latency",
                "severity": "low",
                "message": f"TTFB p95/avg 比值 = {ratio:.1f}（>3），存在明显尾部延迟",
                "value": round(ratio, 2),
            })

    # 规则 6：请求数过少（统计意义不足）
    total_reqs = totals.get("requests", 0)
    if total_reqs < 10:
        findings.append({
            "rule": "insufficient_samples",
            "severity": "info",
            "message": f"总请求数仅 {total_reqs}，统计结论可信度有限",
            "value": total_reqs,
        })

    # 无发现时给一条正面反馈
    if not findings:
        findings.append({
            "rule": "all_clear",
            "severity": "info",
            "message": "所有指标均在阈值范围内，未发现异常",
            "value": None,
        })

    return findings


def format_findings_md(findings: list, doc: dict) -> str:
    """将 findings 格式化为 Markdown 表格"""
    script = doc.get("script", "unknown")
    ts = doc.get("timestamp", "")
    lines = [
        f"# 压测诊断报告",
        f"",
        f"- **脚本**: {script}",
        f"- **时间**: {ts}",
        f"- **协议**: {doc.get('protocol', '')}",
        f"- **端点**: {doc.get('endpoint', '')}",
        f"",
        f"## 规则引擎发现",
        f"",
        f"| 严重度 | 规则 | 说明 |",
        f"|--------|------|------|",
    ]
    severity_icon = {"high": "🔴", "medium": "🟡", "low": "🔵", "info": "ℹ️"}
    for f in findings:
        icon = severity_icon.get(f["severity"], "❓")
        lines.append(f"| {icon} {f['severity']} | {f['rule']} | {f['message']} |")

    return "\n".join(lines) + "\n"


def run_llm_analysis(doc: dict, findings: list) -> str:
    """LLM 叙述层（可选）：调用 OpenAI 兼容接口生成自然语言诊断

    失败时返回错误描述字符串，不抛异常。
    """
    try:
        config.load_env()
        # 优先 ANALYSIS_*，回退 OPENAI_*
        base_url = os.environ.get("ANALYSIS_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
        api_key = os.environ.get("ANALYSIS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        model = os.environ.get("ANALYSIS_LLM_MODEL") or os.environ.get("MODEL_NAME", "")

        if not base_url or not api_key:
            return "[LLM 跳过] 未配置 ANALYSIS_LLM_BASE_URL/API_KEY 或 OPENAI_BASE_URL/API_KEY"

        base_url = config.normalize_base_url(base_url)

        prompt = (
            "你是一个 LLM 性能分析专家。以下是压测结果的规则引擎发现和摘要数据。\n"
            "请生成一段简洁的中文诊断报告，包括：1) 整体评价 2) 关键问题 3) 优化建议。\n\n"
            f"## 规则引擎发现\n{json.dumps(findings, ensure_ascii=False, indent=2)}\n\n"
            f"## 摘要数据\n{json.dumps(doc, ensure_ascii=False, indent=2)}\n"
        )

        import httpx
        url = base_url.rstrip("/") + "/chat/completions"
        resp = httpx.post(
            url,
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # 尝试提取 ```markdown 代码块
        if "```" in content:
            parts = content.split("```")
            for i, p in enumerate(parts):
                if i % 2 == 1:  # 代码块内容
                    content = p.strip()
                    if content.startswith("markdown"):
                        content = content[8:].strip()
                    break

        return content or "[LLM 返回空内容]"

    except Exception as e:
        return f"[LLM 失败] {type(e).__name__}: {str(e)[:200]}"


def parse_args():
    p = argparse.ArgumentParser(description="LLM 压测结果智能诊断")
    p.add_argument("summary", help="summary_*.json 文件路径")
    p.add_argument("--thresholds", default=None,
                   help="阈值配置文件路径（JSON，含 thresholds 对象）；缺省用内置默认")
    p.add_argument("--with-llm", action="store_true",
                   help="启用 LLM 叙述层（需配置 ANALYSIS_* 或 OPENAI_* 环境变量）")
    p.add_argument("--output", default=None,
                   help="输出 MD 路径；默认 results/analysis_<时间戳>.md")
    return p.parse_args()


def main():
    args = parse_args()

    with open(args.summary, "r", encoding="utf-8") as f:
        doc = json.load(f)

    thresholds = load_thresholds(args.thresholds)
    findings = run_rule_engine(doc, thresholds)

    # 规则引擎 MD
    md_content = format_findings_md(findings, doc)

    # LLM 叙述层（可选）
    if args.with_llm:
        print("[INFO] Running LLM analysis...")
        llm_report = run_llm_analysis(doc, findings)
        md_content += f"\n## LLM 诊断叙述\n\n{llm_report}\n"

    # 输出
    output_dir = config.resolve_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    if args.output:
        output_path = args.output
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(output_dir, f"analysis_{ts}.md")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"[OK] Analysis report: {output_path}")
    print(f"[OK] Findings: {len(findings)} rules evaluated")


if __name__ == "__main__":
    main()

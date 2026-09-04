# -*- coding: utf-8 -*-
"""
llmperf_common.html_report —— 单文件 HTML 可视化报告生成器（V2.2 §3.3）

输入：一个 summary_*.json 路径 +（可选）同批次逐轮/逐用户 CSV 目录
输出：单文件 report_<同名>.html，ECharts 通过 CDN 加载，数据以 <script> 内嵌 JSON

页面内容：
1. 概览卡片：总请求 / 成功率 / RPS / TTFB 均值 / 生成速率 / 总耗时
2. Locust 风格分组表（NewChat / ContinueChat / Aggregated）
3. 失败分类饼图（fail_categories 为空则不渲染）
4. TTFB / 整轮耗时分组柱状图（p50/p95/p99）
5. 口径说明折叠区：三栏对照表（V2.2 §3.2.2）
6. 基线类型 RPS 标注（V2.2 修订 #8）

两个入口：
- 自动：脚本在写出 summary JSON 后调用 generate_report()
- 手动：python -m llmperf_common.html_report results/summary_xxx.json
"""
import html
import json
import os
import sys
from datetime import datetime


# ECharts CDN（国内友好镜像）
ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"

# 三口径对照表文案（V2.2 §3.2.2 / 附录 E）
CALIBER_TABLE_HTML = """
<details open>
<summary style="cursor:pointer;font-weight:bold;margin:16px 0 8px">📏 指标口径说明（点击展开/收起）</summary>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px;width:100%">
<tr style="background:#f5f5f5"><th>口径名</th><th>定义</th><th>数据来源</th><th>与其他口径关系</th></tr>
<tr><td><b>响应头到达</b></td><td>Locust <code>with self.client.post(...)</code> 进入时（收到响应头）定格的耗时；脚本命名为 <code>&lt;Name&gt;-TTFB</code>（Dify）或 <code>ChatCompletions-TTFB</code>（OpenAI）</td><td>Locust 内置统计（<code>stream=True</code> 下等于响应头到达，非首真实内容）</td><td>≤ 首真实内容</td></tr>
<tr><td><b>首真实内容</b></td><td>SSE 解析器 <code>sse.StreamTimer</code> 在首个非空 <code>answer</code>/<code>delta.content</code> 块到达时定格；字段 <code>ttfb_ms</code></td><td>逐轮 <code>StreamResult.ttfb_ms</code>；summary JSON <code>by_request_type.*.metrics.ttfb_ms</code></td><td>≥ 响应头到达；≤ 完整往返</td></tr>
<tr><td><b>完整往返</b></td><td>请求发出 → 流式正文消费完毕，由脚本 <code>events.request.fire(name="&lt;Name&gt;-Total")</code> 显式上报；字段 <code>elapsed_ms</code></td><td>逐轮 <code>StreamResult.elapsed_ms</code>；summary JSON <code>by_request_type.*.metrics.latency_ms</code></td><td>≥ 首真实内容</td></tr>
</table>
<p style="font-size:12px;color:#666;margin-top:4px">⚠️ 本报告中"首真实内容"对应图表的 TTFB 指标；"完整往返"对应 Latency 指标。Locust 原生 -TTFB 行仅出现在 --csv 报告中。</p>
</details>
"""


def _fmt(val, unit="", precision=1):
    """null 安全格式化：None → '—'"""
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val:.{precision}f}{unit}"
    return f"{val}{unit}"


def _is_baseline(script_name: str) -> bool:
    """判断是否为基线脚本（V2.2 修订 #8：RPS 标注）"""
    s = (script_name or "").lower()
    return "dialog_ttfb" in s or "baseline" in s


def _build_overview_cards(doc: dict) -> str:
    """概览卡片 HTML"""
    totals = doc.get("totals", {})
    agg_metrics = doc.get("by_request_type", {}).get("Aggregated", {}).get("metrics", {})
    ttfb_avg = agg_metrics.get("ttfb_ms", {}).get("avg")
    gen_avg = agg_metrics.get("gen_chars_per_sec", {}).get("avg")
    wall_time = doc.get("wall_time_s", 0)
    script = doc.get("script", "")

    rps_html = _fmt(totals.get("rps_success"), " req/s", 3)
    if _is_baseline(script):
        rps_html += ' <span style="color:#e67e22;font-size:12px">⚠️ 仅供参考（单线程串行）</span>'

    cards = [
        ("总请求", f'{totals.get("requests", 0)}'),
        ("成功率", _fmt(totals.get("success_rate"), "%")),
        ("RPS (成功)", rps_html),
        ("TTFB 均值", _fmt(ttfb_avg, " ms")),
        ("生成速率", _fmt(gen_avg, " 字符/s")),
        ("总耗时", _fmt(wall_time, " s")),
    ]
    items = "".join(
        f'<div style="background:#fff;border-radius:8px;padding:16px;text-align:center;'
        f'box-shadow:0 1px 3px rgba(0,0,0,0.1)">'
        f'<div style="font-size:12px;color:#888">{label}</div>'
        f'<div style="font-size:20px;font-weight:bold;margin-top:4px">{value}</div></div>'
        for label, value in cards
    )
    return f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px">{items}</div>'


def _build_group_table(doc: dict) -> str:
    """Locust 风格分组表"""
    rows_html = ""
    for name in ["NewChat", "ContinueChat", "Aggregated"]:
        group = doc.get("by_request_type", {}).get(name, {})
        m = group.get("metrics", {})
        ttfb = m.get("ttfb_ms", {})
        lat = m.get("latency_ms", {})
        bold = ' style="font-weight:bold;background:#fafafa"' if name == "Aggregated" else ""
        rows_html += (
            f"<tr{bold}>"
            f"<td>{name}</td>"
            f"<td>{group.get('reqs', 0)}</td>"
            f"<td>{group.get('fails', 0)}</td>"
            f"<td>{_fmt(ttfb.get('p50'), ' ms')}</td>"
            f"<td>{_fmt(ttfb.get('avg'), ' ms')}</td>"
            f"<td>{_fmt(ttfb.get('p95'), ' ms')}</td>"
            f"<td>{_fmt(lat.get('p50'), ' ms')}</td>"
            f"<td>{_fmt(lat.get('p95'), ' ms')}</td>"
            f"<td>{_fmt(m.get('response_length', {}).get('avg'))}</td>"
            f"</tr>"
        )
    return (
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px;margin-bottom:24px">'
        '<tr style="background:#f5f5f5"><th>Type</th><th># Reqs</th><th># Fails</th>'
        '<th>TTFB p50</th><th>TTFB Avg</th><th>TTFB p95</th>'
        '<th>Latency p50</th><th>Latency p95</th><th>Avg Length</th></tr>'
        f"{rows_html}</table>"
    )


def _build_fail_pie(doc: dict) -> str:
    """失败分类饼图（无失败返回空字符串）"""
    cats = doc.get("fail_categories", {})
    if not cats:
        return ""
    data = [{"name": k, "value": v} for k, v in sorted(cats.items(), key=lambda x: -x[1])]
    chart_id = "failPie"
    option = {
        "title": {"text": "失败分类分布", "left": "center"},
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
        "series": [{"type": "pie", "radius": ["30%", "60%"], "data": data}],
    }
    return (
        f'<div id="{chart_id}" style="width:100%;height:300px;margin-bottom:24px"></div>'
        f"<script>echarts.init(document.getElementById('{chart_id}')).setOption({_json(option)});</script>"
    )


def _build_latency_bar(doc: dict) -> str:
    """TTFB / Latency 分组柱状图（p50/p95/p99）"""
    groups = []
    categories = []
    for name in ["NewChat", "ContinueChat", "Aggregated"]:
        g = doc.get("by_request_type", {}).get(name, {})
        if g.get("reqs", 0) == 0:
            continue
        categories.append(name)
        groups.append(g.get("metrics", {}))

    if not categories:
        return ""

    def _series(metric_key, label, color):
        return {
            "name": label, "type": "bar", "barGap": "10%",
            "data": [_safe_get(g, metric_key, "p50") for g in groups],
            "itemStyle": {"color": color},
        }

    option = {
        "title": {"text": "TTFB & Latency 分位数对比", "left": "center"},
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0},
        "xAxis": {"type": "category", "data": categories},
        "yAxis": {"type": "value", "name": "ms"},
        "series": [
            _series("ttfb_ms", "TTFB p50", "#5470c6"),
            _series("ttfb_ms", "TTFB p95", "#91cc75"),
            _series("latency_ms", "Latency p50", "#fac858"),
            _series("latency_ms", "Latency p95", "#ee6666"),
        ],
    }
    chart_id = "latencyBar"
    return (
        f'<div id="{chart_id}" style="width:100%;height:350px;margin-bottom:24px"></div>'
        f"<script>echarts.init(document.getElementById('{chart_id}')).setOption({_json(option)});</script>"
    )


def _safe_get(metrics: dict, key: str, sub: str):
    """null 安全取值，None → 0（ECharts 不接受 null 柱状值）"""
    v = metrics.get(key, {}).get(sub)
    return v if v is not None else 0


def _json(obj) -> str:
    """紧凑 JSON，用于嵌入 <script>。

    修复 #15：`<` 转义为 \\u003c，防止数据含 `</script>`/`<!--` 时截断内嵌脚本
    （存储型 XSS 纵深；json.dumps 不转义这些字符）。
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def generate_report(summary_path: str, output_path: str = None) -> str:
    """根据 summary JSON 生成单文件 HTML 报告，返回输出路径。

    Args:
        summary_path: summary_*.json 文件路径
        output_path: 输出 HTML 路径；默认与 summary 同目录、同名 .html
    """
    with open(summary_path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    if output_path is None:
        base = os.path.splitext(summary_path)[0]
        output_path = base + ".html"

    _esc = html.escape  # 修复 #15：summary doc 派生值一律 HTML 转义后再拼接
    title = _esc(f"LLM 压测报告 - {doc.get('script', 'unknown')} ({doc.get('timestamp', '')})")
    protocol_label = "Dify" if doc.get("protocol") == "dify" else "OpenAI 兼容"
    meta_info = (
        f'<div style="font-size:13px;color:#666;margin-bottom:16px">'
        f'协议: {protocol_label} | 端点: {_esc(str(doc.get("endpoint", "")))} | '
        f'模型: {_esc(str(doc.get("model", "—")))} | '
        f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        f'</div>'
    )

    body = (
        _build_overview_cards(doc)
        + _build_group_table(doc)
        + _build_fail_pie(doc)
        + _build_latency_bar(doc)
        + CALIBER_TABLE_HTML
    )

    # 模板变量名避开 report_html 而非 html：本模块顶部 import html 供 _esc 使用（修复 #15）
    report_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script src="{ECHARTS_CDN}"></script>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width: 960px; margin: 0 auto; padding: 24px; background: #f8f9fa; color: #333; }}
h1 {{ font-size: 20px; margin-bottom: 4px; }}
table th, table td {{ text-align: right; }}
table td:first-child, table th:first-child {{ text-align: left; }}
</style>
</head>
<body>
<h1>{title}</h1>
{meta_info}
{body}
<footer style="margin-top:32px;padding-top:16px;border-top:1px solid #eee;font-size:12px;color:#666">
Generated by llmperf_common.html_report · Schema v{_esc(str(doc.get('schema_version', '?')))} · V2.2
</footer>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    return output_path


def auto_post_run(summary_path: str) -> None:
    """脚本结尾统一钩子：按环境变量自动控制 HTML 报告 + 规则诊断生成。

    环境变量：
      LLMPERF_HTML_REPORT=0 → 跳过 HTML 报告（默认 1）
      LLMPERF_ANALYSIS=1   → 额外生成规则诊断 MD（默认 0）

    任何异常静默打印警告，不影响主流程退出码。
    """
    if not summary_path or not os.path.isfile(summary_path):
        return

    # HTML 报告
    if os.environ.get("LLMPERF_HTML_REPORT", "1").strip() != "0":
        try:
            html_path = generate_report(summary_path)
            print(f"[auto] HTML report: {html_path}")
        except Exception as e:
            print(f"[auto] HTML report failed: {e}")

    # 规则诊断
    if os.environ.get("LLMPERF_ANALYSIS", "0").strip() == "1":
        try:
            # 延迟导入避免循环依赖
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from analyze_summary import run_rule_engine, format_findings_md, load_thresholds
            with open(summary_path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            thresholds = load_thresholds()
            findings = run_rule_engine(doc, thresholds)
            md = format_findings_md(findings, doc)
            out_dir = os.path.dirname(summary_path)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            analysis_path = os.path.join(out_dir, f"analysis_{ts}.md")
            with open(analysis_path, "w", encoding="utf-8") as f:
                f.write(md)
            print(f"[auto] Analysis report: {analysis_path}")
        except Exception as e:
            print(f"[auto] Analysis failed: {e}")


def main():
    """CLI 入口：python -m llmperf_common.html_report <summary.json> [output.html]"""
    if len(sys.argv) < 2:
        print("用法: python -m llmperf_common.html_report <summary.json> [output.html]")
        sys.exit(1)
    summary_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    result = generate_report(summary_path, output_path)
    print(f"[OK] HTML report generated: {result}")


if __name__ == "__main__":
    main()

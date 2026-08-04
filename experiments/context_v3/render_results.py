"""Render a saved context-v3 run as a dependency-free HTML report."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _avg_context(summary: dict[str, Any]) -> int:
    rows = summary.get("results", [])
    values = [int(row.get("context", {}).get("context_tokens", 0)) for row in rows]
    return round(sum(values) / max(1, len(values)))


def _leaders(designs: dict[str, Any], metric: str) -> tuple[list[str], float | None]:
    values = {
        name: summary.get(metric)
        for name, summary in designs.items()
        if isinstance(summary.get(metric), (int, float))
    }
    if not values:
        return [], None
    best = max(float(value) for value in values.values())
    return [name for name, value in values.items() if float(value) == best], best


def render(data: dict[str, Any], source_name: str) -> str:
    designs = data.get("designs", {})
    noise_turns = int(data.get("noise_turns", 0) or 0)
    total_records = int(data.get("records", 0) or 0)
    core_records = max(0, total_records - noise_turns)
    case_count = len(data.get("cases", []))
    provider = data.get("provider") or {}
    provider_label = str(provider.get("model") or provider.get("name") or "unknown model")
    overall_names, overall_rate = _leaders(designs, "pass_rate")
    fact_names, fact_rate = _leaders(
        {name: {"value": summary.get("category_rates", {}).get("fact_recall")} for name, summary in designs.items()},
        "value",
    )
    boundary_names, boundary_rate = _leaders(
        {name: {"value": summary.get("category_rates", {}).get("private_boundary")} for name, summary in designs.items()},
        "value",
    )
    dispatch_names, dispatch_rate = _leaders(
        {name: {"value": summary.get("category_rates", {}).get("dispatch_efficiency")} for name, summary in designs.items()},
        "value",
    )
    overall_label = "、".join(overall_names) or "无可用结果"
    fact_label = "、".join(fact_names) or "无可用结果"
    boundary_label = "、".join(boundary_names) or "无可用结果"
    dispatch_label = "、".join(dispatch_names) or "无可用结果"
    rows = []
    failure_sections = []
    for name, summary in designs.items():
        cats = summary.get("category_rates", {})
        rows.append(
            "<tr>"
            f"<th>{html.escape(name)}</th>"
            f"<td>{_pct(summary.get('pass_rate'))}</td>"
            f"<td>{_pct(cats.get('fact_recall'))}</td>"
            f"<td>{_pct(cats.get('private_boundary'))}</td>"
            f"<td>{_pct(cats.get('instruction_following'))}</td>"
            f"<td>{_pct(cats.get('dispatch_efficiency'))}</td>"
            f"<td>{_avg_context(summary):,}</td>"
            f"<td>{int(summary.get('input_tokens', 0)):,}</td>"
            "</tr>"
        )
        failures = []
        for row in summary.get("results", []):
            score = row.get("score", {})
            if score.get("passed"):
                continue
            failed_checks = [key for key, value in score.get("checks", {}).items() if not value]
            failures.append(
                f"<li><code>{html.escape(row.get('case', ''))}</code>: "
                f"{html.escape(', '.join(failed_checks) or 'request error')}</li>"
            )
        failure_sections.append(
            f"<section><h3>{html.escape(name)}</h3>"
            + ("<ul>" + "".join(failures) + "</ul>" if failures else "<p class=good>没有失败用例</p>")
            + "</section>"
        )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Context v3 真实实验结果</title>
<style>
:root{{--ink:#18202b;--muted:#5d6875;--line:#d9dee5;--paper:#f6f8fa;--white:#fff;--blue:#2463a9;--green:#17745b;--red:#b84a46}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1160px;margin:auto;padding:40px 22px 70px}} h1{{font-size:42px;line-height:1.1;margin:0 0 12px}} h2{{margin:42px 0 14px}} h3{{margin:0 0 7px}}
.lead,.note{{color:var(--muted)}} .callout{{margin:22px 0;padding:18px 20px;background:var(--white);border-left:4px solid var(--blue)}}
table{{width:100%;border-collapse:collapse;background:var(--white)}} th,td{{padding:11px 10px;border:1px solid var(--line);text-align:left;vertical-align:top}} thead th{{background:#eaf0f5;font-size:13px}}
th{{font-weight:750}} code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.9em}} section{{background:var(--white);border:1px solid var(--line);padding:16px;margin:10px 0}} ul{{margin:8px 0 0;padding-left:22px}} .good{{color:var(--green);font-weight:700}}
@media(max-width:760px){{h1{{font-size:31px}} table{{font-size:12px}} th,td{{padding:7px 6px}}}}
</style></head><body><main>
<p class="note">Context v3 / {html.escape(provider_label)} / {html.escape(source_name)}</p>
<h1>三种上下文设计的真实对照</h1>
<p class="lead">{noise_turns} 条噪声记录，{core_records} 条核心协作事实，{case_count} 个请求；不做上下文回收。每个设计使用相同模型、工具 schema、角色资料和当前输入。</p>
<div class="callout"><strong>结果先说：</strong>本轮总体通过率最高的是 {html.escape(overall_label)}（{_pct(overall_rate)}）；事实召回最高的是 {html.escape(fact_label)}（{_pct(fact_rate)}），私密边界最高的是 {html.escape(boundary_label)}（{_pct(boundary_rate)}），复杂调度投递最高的是 {html.escape(dispatch_label)}（{_pct(dispatch_rate)}）。这说明 Room 和全格式化各有优势，尚不足以证明需要把 Room 提升为强制业务抽象。</div>
<h2>汇总</h2>
<table><thead><tr><th>设计</th><th>总体</th><th>事实召回</th><th>私密边界</th><th>指令遵循</th><th>调度投递</th><th>平均上下文</th><th>输入 tokens</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p class="note">平均上下文是 provider 请求的估算值；输入 tokens 是模型返回的 usage，受缓存影响，不能直接当作价格。</p>
<h2>读法</h2>
<p>Ledger 的主要失败不是“完全记不住”，而是长历史下把当前私聊回答误当成跨会话动作，向当前目标调用 <code>send</code>；它在本轮私密边界用例中表现最差。Task Room 显著减少了输入规模，并在事实召回上最好；全格式化上下文把所有角色资料交给模型，私密边界最好，但同样需要后端目标校验。三种方案在修正后的“群聊请求 -> 对应私聊”及 fan-out 投递上都通过，说明复杂调度的可靠性不能只用一次模型选择来保证，仍需要真实 send 回执、幂等和目标约束。</p>
<h2>失败用例</h2>{''.join(failure_sections)}
<h2>结论</h2>
<div class="callout"><strong>建议的架构边界：</strong>让 LLM 保持自由规划，系统只做输入格式化、身份/可见性过滤、工具参数校验、目标地址、幂等和真实回执。共享黑板或 Room 可以作为 prompt 里的自然语言组织方式，但不要让模型必须先理解 Room 才能发现新任务关系。</div>
<p class="note">限制：单模型、单次 {case_count}-case run、合成资料、未覆盖回收和重启恢复；通过率是模型决策的离散样本，不是生产成功率。原始脱敏结果：<code>{html.escape(source_name)}</code>。</p>
</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--out", default="docs/experiments/context-v3-results.html")
    args = parser.parse_args()
    source = Path(args.input)
    data = json.loads(source.read_text(encoding="utf-8"))
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(data, str(source)), encoding="utf-8")
    print(f"[report] saved {output}")


if __name__ == "__main__":
    main()

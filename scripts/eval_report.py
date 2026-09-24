"""③ runtime 平台化：统一评估报告卡——把 reports/ 既有产物聚合成一张 Agent 报告卡。

用法::

    python scripts/eval_report.py [--reports reports] [--out reports/agent-card-<ts>.json]

聚合：各门禁最新报告（judge_sensitivity / injection_gate / conflict_gate / qa_gate /
replay 次数）+ 累计 usage 总账 + （可选）trace 汇总。
判读口径继承 evalmeta：未知/样本不足显式标注，不粉饰。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from scripts.route_a_cost import load_usage, summarize as summarize_usage


def _latest(reports_dir: Path, pattern: str) -> Path | None:
    files = sorted(reports_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def collect(reports_dir: Path, trace_path: str | None = None) -> dict:
    card: dict = {"generated_at": datetime.now().isoformat(timespec="seconds"), "gates": {}}

    j = _latest(reports_dir, "judge_sensitivity_*.json")
    if j:
        data = json.loads(j.read_text(encoding="utf-8"))
        card["gates"]["judge_sensitivity"] = {
            "pack": data.get("pack"),
            "report": j.name,
            "summary": data.get("summary"),
        }
    i = _latest(reports_dir, "injection_gate_*.json")
    if i:
        data = json.loads(i.read_text(encoding="utf-8"))
        card["gates"]["injection_gate"] = {
            "report": i.name,
            "canary_leaks": data.get("canary_leaks"),
            "mechanism_leaks": data.get("mechanism_leaks"),
            "confessions": data.get("confessions"),
            "cases_total": data.get("cases_total"),
            "passed": data.get("passed"),
        }
    c = _latest(reports_dir, "conflict_gate_*.json")
    if c:
        data = json.loads(c.read_text(encoding="utf-8"))
        card["gates"]["conflict_gate"] = {
            "report": c.name,
            "summary": data.get("summary"),
            "note": data.get("note"),
        }
    q = _latest(reports_dir, "qa_gate_*.json")
    if q:
        data = json.loads(q.read_text(encoding="utf-8"))
        card["gates"]["qa_gate"] = {
            "report": q.name,
            "passed_all": data.get("passed_all"),
            "executed": data.get("executed"),
            "skipped": data.get("skipped"),
        }
    card["replay_reports"] = len(list(reports_dir.glob("replay_*.json")))

    # 累计 usage 总账（reports/ 下所有 usage-*.jsonl）
    rows: list[dict] = []
    for f in reports_dir.glob("usage-*.jsonl"):
        rows += load_usage(f)
    summary = summarize_usage(rows)
    card["usage"] = {
        # by_key 的键是 (component, model, purpose) 三元组——JSON 不能序列化，转字符串键
        "by_key": {f"{c}|{m}|{p}": row for (c, m, p), row in summary["by_key"].items()},
        "total": summary["total"],
    }

    if trace_path and Path(trace_path).exists():
        from scripts.trace_report import aggregate, load_trace

        agg = aggregate(load_trace(trace_path))
        card["trace"] = {
            "turns": len(agg["turns"]),
            "failures": agg["failures"],
            "side_calls": agg["side_calls"],
        }
    return card


def render(card: dict) -> str:
    lines = ["===== Agent 报告卡 ====="]
    g = card["gates"]
    if "judge_sensitivity" in g:
        s = g["judge_sensitivity"]
        lines.append(f"[E1 Judge] {s['pack']}（{s['report']}）")
        for cat, row in (s["summary"] or {}).items():
            if cat == "normal":
                lines.append(f"  {cat:<8} 误报率 {row.get('rate')}（n={row.get('n')}，"
                             f"未知 {row.get('unknown')}）")
            else:
                lines.append(f"  {cat:<8} 拦截率 {row.get('rate')}（n={row.get('n')}，"
                             f"未知 {row.get('unknown')}）")
    if "injection_gate" in g:
        i = g["injection_gate"]
        mark = "✓" if i["passed"] else "✗"
        lines.append(f"[注入门禁] {mark} canary 泄露 {i['canary_leaks']}/{i['cases_total']} · "
                     f"机制泄露 {i['mechanism_leaks']} · 身份自认 {i['confessions']}（观察项）")
    if "conflict_gate" in g:
        c = g["conflict_gate"]
        lines.append(f"[冲突判定] {c['report']}（基线期无硬门；行为轴见报告）")
        for label, row in (c["summary"] or {}).items():
            lines.append(f"  {label:<8} {row['correct']}/{row['n']} = {row['accuracy']}")
    if "qa_gate" in g:
        q = g["qa_gate"]
        lines.append(f"[qa_gate] {'✓ 全部通过' if q['passed_all'] else '✗'} "
                     f"（执行 {q['executed']} / 跳过 {q['skipped']}）")
    lines.append(f"[Replay] 报告 {card['replay_reports']} 份")
    total = card["usage"].get("total", {})
    lines.append(f"[成本总账] 调用 {total.get('calls', 0)} · 入 {total.get('prompt', 0):,} · "
                 f"出 {total.get('completion', 0):,} · 约 ¥{total.get('cost', 0):.3f}（累计，空闲口径）")
    if "trace" in card:
        t = card["trace"]
        lines.append(f"[Trace] 回合 {t['turns']} · 熔断 {t['failures']['meltdown']} · "
                     f"工具异常 {len(t['failures']['tool_issues'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_report", description="统一评估报告卡")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--trace", default=None, help="trace jsonl（可选，附回合级观测）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    card = collect(Path(args.reports), args.trace)
    print(render(card))
    if args.out:
        out = Path(args.out)
        out.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告卡已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

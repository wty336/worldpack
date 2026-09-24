"""③ runtime 平台化：trace 查看器——把 trace JSONL 聚合成回合视图（回答"第 N 轮为什么这样"）。

与 usage 账本分工：trace = 过程证据（时延/verdict/重试/熔断），usage = 成本证据。

用法::

    python scripts/trace_report.py --trace <trace.jsonl> [--turn N] [--usage <usage.jsonl>] [--out json]

- 总览：每回合一行（iterations/时延/tokens/工具状态/结局）；
- ``--turn N``：展开该回合完整事件链（turn_begin → call → tool → turn_end）；
- 故障清单：熔断回合 + 工具异常（rejected/bad_json/protocol_error/unknown）；
- ``--usage``：成本分用途表（复用 route_a_cost 同源口径）。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from scripts.route_a_cost import load_usage, summarize as summarize_usage


def load_trace(path: str | Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def aggregate(events: list[dict]) -> dict:
    """事件流 → {turns, side_calls, failures}。"""
    turns: dict[int, dict] = defaultdict(
        lambda: {"calls": [], "tools": [], "begin": None, "end": None}
    )
    side_calls: list[dict] = []
    for e in events:
        seq = e.get("turn_seq")
        if seq is None:
            if e["event"] == "call":
                side_calls.append(e)
            continue
        t = turns[seq]
        if e["event"] == "turn_begin":
            t["begin"] = e
        elif e["event"] == "turn_end":
            t["end"] = e
        elif e["event"] == "call":
            t["calls"].append(e)
        elif e["event"] == "tool":
            t["tools"].append(e)

    rows: list[dict] = []
    for seq in sorted(turns):
        t = turns[seq]
        if t["begin"] is None:
            continue
        end = t["end"]
        rows.append({
            "turn_seq": seq,
            "model": t["begin"].get("model"),
            "iterations": end.get("iterations") if end else len(t["calls"]),
            "outcome": end.get("outcome") if end else "unfinished",
            "latency_ms": sum(c.get("latency_ms") or 0 for c in t["calls"]),
            "prompt_tokens": sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in t["calls"]),
            "completion_tokens": sum((c.get("usage") or {}).get("completion_tokens", 0) for c in t["calls"]),
            "tool_statuses": dict(Counter(x.get("status") for x in t["tools"])),
            "plot_signal": end.get("plot_signal") if end else None,
            "narration_chars": end.get("narration_chars") if end else None,
        })

    side: dict[str, dict] = defaultdict(lambda: {"calls": 0, "latency_ms": 0})
    for c in side_calls:
        s = side[c.get("purpose", "?")]
        s["calls"] += 1
        s["latency_ms"] += c.get("latency_ms") or 0

    bad = ("rejected", "bad_json", "protocol_error", "unknown")
    failures = {
        "meltdown": [r["turn_seq"] for r in rows if r["outcome"] == "meltdown"],
        "tool_issues": [
            {"turn_seq": r["turn_seq"], "statuses": r["tool_statuses"]}
            for r in rows if any(k in r["tool_statuses"] for k in bad)
        ],
    }
    return {
        "turns": rows,
        "side_calls": {k: dict(v) for k, v in side.items()},
        "failures": failures,
    }


def render_turn_chain(events: list[dict], turn_seq: int) -> str:
    lines: list[str] = []
    for e in events:
        if e.get("turn_seq") != turn_seq:
            continue
        if e["event"] == "call":
            lines.append(
                f"  [{e['seq']}] call {e.get('purpose')} {e.get('latency_ms')}ms "
                f"finish={e.get('finish_reason')} usage={e.get('usage')}"
            )
        elif e["event"] == "tool":
            lines.append(
                f"  [{e['seq']}] tool {e.get('name')} status={e.get('status')} "
                f"detail={str(e.get('detail'))[:60]}"
            )
        elif e["event"] == "turn_end":
            lines.append(
                f"  [{e['seq']}] turn_end outcome={e.get('outcome')} "
                f"iterations={e.get('iterations')} plot={e.get('plot_signal')}"
            )
        else:
            lines.append(
                f"  [{e['seq']}] turn_begin model={e.get('model')} messages={e.get('messages')}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trace_report", description="trace 查看器")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--turn", type=int, default=None, help="展开该回合完整事件链")
    parser.add_argument("--usage", default=None, help="usage jsonl（成本分用途表）")
    parser.add_argument("--out", default=None, help="汇总 JSON 输出路径")
    args = parser.parse_args(argv)

    events = load_trace(args.trace)
    agg = aggregate(events)
    print(f"trace 事件 {len(events)} 条 · 回合 {len(agg['turns'])} · "
          f"侧信道用途 {sorted(agg['side_calls'])}")
    for r in agg["turns"]:
        ts = " ".join(f"{k}:{v}" for k, v in r["tool_statuses"].items()) or "-"
        print(f"  #{r['turn_seq']:<4} {r['outcome']:<11} iter={r['iterations']} "
              f"{r['latency_ms']:>6}ms 入{r['prompt_tokens']:>7} 出{r['completion_tokens']:>5} 工具[{ts}]")
    if agg["failures"]["meltdown"]:
        print(f"[!] 熔断回合: {agg['failures']['meltdown']}")
    if agg["failures"]["tool_issues"]:
        print(f"[!] 工具异常回合: {agg['failures']['tool_issues']}")
    if args.turn is not None:
        print(f"\n=== 第 {args.turn} 回合事件链 ===")
        print(render_turn_chain(events, args.turn))
    if args.usage:
        print("\n===== 成本分用途（与 route_a_cost 同源口径）=====")
        summary = summarize_usage(load_usage(Path(args.usage)))
        items = sorted(summary["by_key"].items(), key=lambda kv: -kv[1]["cost"])
        for (component, _model, purpose), row in items:
            print(f"  {component}|{purpose:<16} {_model:<22} 调用 {row['calls']:>4} "
                  f"入 {row['prompt']:>8,} 出 {row['completion']:>7,} 约 ¥{row['cost']:.4f}")
        t = summary["total"]
        print(f"  合计：调用 {t['calls']} · 入 {t['prompt']:,} · 出 {t['completion']:,} "
              f"· 约 ¥{t['cost']:.4f}")
    if args.out:
        Path(args.out).write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n汇总已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

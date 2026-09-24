"""③ runtime 平台化 守卫测试：观测与评估聚合（离线，合成数据）。"""

from __future__ import annotations

import json

from scripts.eval_report import collect, render
from scripts.run_matrix import RunResult, build_table
from scripts.trace_report import aggregate, load_trace, render_turn_chain


def _events() -> list[dict]:
    return [
        {"ts": "t", "seq": 1, "pid": 1, "event": "turn_begin", "turn_seq": 1, "model": "m", "messages": 5},
        {"ts": "t", "seq": 2, "pid": 1, "event": "call", "turn_seq": 1, "purpose": "turn",
         "model": "m", "latency_ms": 100, "finish_reason": "tool_calls",
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"ts": "t", "seq": 3, "pid": 1, "event": "tool", "turn_seq": 1, "name": "change_stat",
         "status": "rejected", "detail": "越界"},
        {"ts": "t", "seq": 4, "pid": 1, "event": "turn_end", "turn_seq": 1, "outcome": "completed",
         "iterations": 1, "narration_chars": 20, "plot_signal": "normal"},
        {"ts": "t", "seq": 5, "pid": 1, "event": "turn_begin", "turn_seq": 2, "model": "m", "messages": 7},
        {"ts": "t", "seq": 6, "pid": 1, "event": "turn_end", "turn_seq": 2, "outcome": "meltdown", "iterations": 3},
        {"ts": "t", "seq": 7, "pid": 1, "event": "call", "purpose": "judge", "model": "m",
         "latency_ms": 50, "usage": None},
    ]


def test_trace_aggregate_turns_and_failures():
    agg = aggregate(_events())
    assert len(agg["turns"]) == 2
    t1 = agg["turns"][0]
    assert t1["outcome"] == "completed" and t1["latency_ms"] == 100
    assert t1["prompt_tokens"] == 10 and t1["tool_statuses"] == {"rejected": 1}
    assert agg["failures"]["meltdown"] == [2]
    assert agg["failures"]["tool_issues"][0]["turn_seq"] == 1
    assert agg["side_calls"]["judge"]["calls"] == 1  # 无 turn_seq 的调用归侧信道


def test_trace_render_turn_chain():
    chain = render_turn_chain(_events(), 1)
    assert "turn_begin" in chain and "change_stat" in chain and "turn_end" in chain


def test_trace_load(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(_events()[0], ensure_ascii=False) + "\n", encoding="utf-8")
    assert load_trace(p)[0]["event"] == "turn_begin"


def test_eval_report_collect(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "judge_sensitivity_20260101-000000.json").write_text(json.dumps({
        "pack": "江湖旧梦",
        "summary": {"ooc": {"rate": 1.0, "n": 20, "unknown": 0},
                    "normal": {"rate": 0.06, "n": 48, "unknown": 0}},
    }), encoding="utf-8")
    (reports / "injection_gate_20260101-000000.json").write_text(json.dumps({
        "canary_leaks": 0, "mechanism_leaks": 0, "confessions": 0,
        "cases_total": 16, "passed": True,
    }), encoding="utf-8")
    (reports / "usage-x.jsonl").write_text(
        '{"ts":"t","model":"m","purpose":"turn","prompt_tokens":100,"completion_tokens":10}\n',
        encoding="utf-8",
    )
    card = collect(reports)
    assert card["gates"]["judge_sensitivity"]["summary"]["ooc"]["rate"] == 1.0
    assert card["gates"]["injection_gate"]["passed"] is True
    assert card["usage"]["total"]["calls"] == 1 and card["usage"]["total"]["prompt"] == 100


def test_eval_report_render():
    card = {
        "generated_at": "t", "gates": {}, "replay_reports": 3,
        "usage": {"total": {"calls": 1, "prompt": 100, "completion": 10, "cost": 0.0002}},
    }
    text = render(card)
    assert "Agent 报告卡" in text and "成本总账" in text and "¥0.000" in text


def test_run_matrix_build_table_math():
    results = [
        RunResult(seed=1, exit_code=0, audit_pass=True, forbidden_pass=True,
                  ending="自由落体", cost=0.1),
        RunResult(seed=2, exit_code=1, audit_pass=False, forbidden_pass=True,
                  ending="未达成", cost=0.2),
    ]
    table = build_table(results)
    s = table["summary"]
    assert s["exit0_rate"] == 0.5 and s["audit_pass_rate"] == 0.5
    assert s["forbidden_pass_rate"] == 1.0
    assert s["mean_cost"] == 0.15 and s["cost_std"] == 0.05
    assert len(table["runs"]) == 2 and s["endings"] == ["未达成", "自由落体"]

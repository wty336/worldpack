"""extract 直接评测（Phase 1 · Step 1）：读冻结评测集，跑真机，按实体召回/去重纪律判分。

用法：
    python scripts/extract_eval.py              # 基线模式：跑完只出报告，退出码 0
    python scripts/extract_eval.py --gate       # 门禁模式：召回 <0.80 或去重违规 >0 → 退出码 1
    python scripts/extract_eval.py --dry-run    # 只打印用例（零 API）
    python scripts/extract_eval.py --limit 5    # 抽样调试

判分口径见 `tests/test_extract_eval.py` 与 `docs/plan-phase1-data.md` §3（B 块）。
评测集是冻结的（`eval-sets/MANIFEST.md`）：本脚本只读不写。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from game_agent.budgets import EXTRACT_MAX_TOKENS, complete_with_empty_retry
from game_agent.config import load_settings
from game_agent.endpoint import fingerprint_for
from game_agent.llm import LLMClient
from game_agent.memory import EXTRACT_SYSTEM, parse_facts
from game_agent.usage import UsageTracker

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_SET = REPO_ROOT / "eval-sets" / "extract" / "direct.yaml"
PROMPT_VERSION = hashlib.sha256(EXTRACT_SYSTEM.encode()).hexdigest()[:16]
FABRICATION_OVERLAP_MIN = 0.30  # 抽出事实与输入的大二元组重合率下限（低于此值计"疑似编造"，仅诊断）


def _bigrams(text: str) -> set[str]:
    """字符二元组（与 game_agent.memory 同款；此处置于本地以免依赖私有符号）。"""
    text = re.sub(r"\s+", "", text)
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _overlap_ratio(fact: str, haystack_bigrams: set[str]) -> float:
    grams = _bigrams(fact)
    if not grams:
        return 1.0
    return len(grams & haystack_bigrams) / len(grams)


def score_case(case: dict, facts: list[tuple[str, float]]) -> dict:
    """单例判分（纯函数）。facts = parse_facts 的解析结果。"""
    result = {
        "id": case["id"],
        "genre": case["genre"],
        "axis_kind": case["axis_kind"],
        "expect_empty": case["expect_empty"],
        "n_facts": len(facts),
        "missing": [],
        "dedup_violations": [],
        "suspected_fabrication": [],
        "importance_ok": True,
        "importance_out_of_range": False,
        "empty_violation": False,
    }

    if case["expect_empty"]:
        result["empty_violation"] = len(facts) > 0
        result["recalled"] = True
        result["dedup_ok"] = True
        result["passed"] = not result["empty_violation"]
        return result

    fact_texts = [text for text, _ in facts]
    joined = "\n".join(fact_texts)
    result["missing"] = [e for e in case["must_recall"] if e not in joined]
    result["recalled"] = not result["missing"]
    # 去重纪律：按**引擎自身的包含语义**判违规（memory.py:181 双向包含 = 判重）。
    # 不用"实体是否出现"——带了新信息的复述（如「听雨剑已伴随玩家三年」）是合法新事实。
    result["dedup_violations"] = [
        e for e in case["must_not_output"]
        if any(e in f or f in e for f in fact_texts)
    ]
    result["dedup_ok"] = not result["dedup_violations"]

    lo, hi = case["importance_range"]
    if case["must_recall"]:
        key = case["must_recall"][0]
        carrier = next((imp for text, imp in facts if key in text), None)
        result["importance_ok"] = carrier is None or lo <= carrier <= hi
        result["importance_out_of_range"] = not result["importance_ok"]

    haystack = _bigrams(case["turn"] + "".join(case["existing"]))
    result["suspected_fabrication"] = [
        text for text, _ in facts if _overlap_ratio(text, haystack) < FABRICATION_OVERLAP_MIN
    ]

    # 重要性是"优先级口径"而非对错：保留为诊断项，不参与通过判定（2026-09-12 首跑后定）
    result["passed"] = result["recalled"] and result["dedup_ok"]
    return result


def summarize(results: list[dict]) -> dict:
    """汇总：召回率只统计正例；去重违规与负例单独计。"""
    positives = [r for r in results if not r["expect_empty"]]
    negatives = [r for r in results if r["expect_empty"]]
    recall_hits = sum(1 for r in positives if r["recalled"])
    dedup_bad = sum(1 for r in results if not r["dedup_ok"])
    passed = sum(1 for r in results if r["passed"])
    return {
        "n": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results), 3) if results else 0.0,
        "recall_hits": recall_hits,
        "recall_total": len(positives),
        "recall_rate": round(recall_hits / len(positives), 3) if positives else 0.0,
        "dedup_violations": dedup_bad,
        "empty_ok": sum(1 for r in negatives if r["passed"]),
        "empty_total": len(negatives),
        "suspected_fabrication": sum(len(r["suspected_fabrication"]) for r in results),
        "importance_out_of_range": sum(1 for r in results if r.get("importance_out_of_range")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="extract 直接评测（冻结评测集）")
    parser.add_argument("--gate", action="store_true", help="门禁模式：召回<0.80 或去重违规>0 → 退出 1")
    parser.add_argument("--dry-run", action="store_true", help="只打印用例，零 API")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 例（调试）")
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="评测侧默认 0（稳定尺子）；生产 _extract_facts 未传 temperature，见报告 meta 注记")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每例重复次数（默认 1；建议 3——非确定性下单跑方差可达 ±13pp）")
    args = parser.parse_args(argv)

    data = yaml.safe_load(EVAL_SET.read_text(encoding="utf-8"))
    cases = data["cases"][: args.limit] if args.limit else data["cases"]
    eval_sha = hashlib.sha256(EVAL_SET.read_bytes()).hexdigest()
    print(f"评测集 {EVAL_SET.relative_to(REPO_ROOT)} · {len(cases)} 例 · sha256={eval_sha[:12]}")
    print(f"prompt_version={PROMPT_VERSION} · 预算 EXTRACT_MAX_TOKENS={EXTRACT_MAX_TOKENS}\n")

    if args.dry_run:
        for c in cases:
            flag = "负例" if c["expect_empty"] else "正例"
            print(f"  [{flag}] {c['id']:<12} {c['genre']:<5} {c['axis_kind']:<6} 期望 {c['must_recall']}")
        return 0

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（.env）")
        return 1
    tracker = UsageTracker("reports/usage-extract-eval.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    model = llm.model_for("extract")

    results: list[dict] = []
    case_pass: dict[str, list[bool]] = {}
    for c in cases:
        existing = "；".join(c["existing"])
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {
                "role": "user",
                "content": f"已有事实：{existing}\n\n<回合内容>\n{c['turn']}\n</回合内容>",
            },
        ]
        marks: list[bool] = []
        for rep in range(args.repeat):
            out = complete_with_empty_retry(
                llm, messages, purpose="extract", max_tokens=EXTRACT_MAX_TOKENS,
                temperature=args.temperature,
            )
            facts = parse_facts(out)
            r = score_case(c, facts)
            r["repeat"] = rep + 1
            r["raw_output"] = out  # 每条 run 自带原始输出：翻转用例才可诊断
            results.append(r)
            marks.append(r["passed"])
        case_pass[c["id"]] = marks
        r = results[-1]
        mark = "✓" if all(marks) else ("~" if any(marks) else "✗")
        detail = ""
        if not r["passed"]:
            if r["empty_violation"]:
                detail = f"应输出「无」却抽出 {r['n_facts']} 条"
            else:
                bits = []
                if r["missing"]:
                    bits.append(f"漏 {r['missing']}")
                if r["dedup_violations"]:
                    bits.append(f"重复已有 {r['dedup_violations']}")
                if not r["importance_ok"]:
                    bits.append("重要性越界")
                detail = " · ".join(bits)
        rep_txt = f"（{sum(marks)}/{len(marks)} 通过）" if args.repeat > 1 else ""
        print(f"  [{mark}] {c['id']:<12} {c['genre']:<5} 抽出 {r['n_facts']} 条 {rep_txt}{detail}")

    s = summarize(results)
    if args.repeat > 1:
        flipped = [cid for cid, v in case_pass.items() if any(v) and not all(v)]
        print(f"\n逐例稳定性：{len(case_pass) - len(flipped)}/{len(case_pass)} 稳定"
              f"（翻转 {len(flipped)} 例：{', '.join(flipped[:6])}{'…' if len(flipped) > 6 else ''}）")
    print(f"\n召回 {s['recall_hits']}/{s['recall_total']} = {s['recall_rate']:.0%}（按 run 计）"
          f" · 去重违规 {s['dedup_violations']}"
          f" · 负例正确 {s['empty_ok']}/{s['empty_total']}"
          f" · 疑似编造(诊断) {s['suspected_fabrication']}"
          f" · 重要性越界(诊断) {s.get('importance_out_of_range', 0)}")
    print(f"综合通过 {s['passed']}/{s['n']} = {s['pass_rate']:.0%}")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "endpoint": fingerprint_for(settings, "extract"),
        "baseline": "zero-shot",
        "temperature": args.temperature,
        "note_temperature": "评测侧钉 0 以稳定尺子；生产 _extract_facts 未传 temperature（供应商默认 → 实测运行间方差 ±13pp）",
        "repeat": args.repeat,
        "prompt_version": PROMPT_VERSION,
        "budget_policy": f"game_agent/budgets.py EXTRACT_MAX_TOKENS={EXTRACT_MAX_TOKENS}",
        "eval_set": str(EVAL_SET.relative_to(REPO_ROOT)),
        "eval_set_sha256": eval_sha,
        "summary": s,
        "cases": results,  # 每条 run 已自带 repeat / raw_output
    }
    out_path = Path("reports") / f"extract-eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {out_path}")
    print("\n" + tracker.cost_report())

    if args.gate:
        ok = s["recall_rate"] >= 0.80 and s["dedup_violations"] == 0
        print("\n[✓] 门禁通过" if ok else "\n[✗] 门禁未通过（召回<0.80 或有去重违规）")
        return 0 if ok else 1
    print("\n（基线模式：仅记录，不作门禁判定；加 --gate 启用门禁）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

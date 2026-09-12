"""决策 13 的「定 X」（Task 9 Step 5 / M4 验收项）：程序部分。

**X 是什么**：轨道 1（规则保全率）与轨道 2（rubric 保真维折算值）在**同一样本**上的
绝对差，单位 **pp**。两条轨道量的是同一件事（摘要有没有丢关键信息），X 就是"两条尺子
允许多大分歧"——超过 X 的样本说明两条轨道判得不一致，需要人看。

**怎么定**：取 `diff_i = |规则保全率_i − 保真维分_i / 2|` 的 **P95（最近秩法）**，
**向上取整到 5pp**（决策 13 原文）。

**依据哪批数据**：调用方给定（本次 = dev 层 compress 出库批 + 同批轨道 2 报告）。

用法：
    python scripts/rubric_x_calibrate.py --samples data/route-a/dev/compress.jsonl \
        --report reports/rubric-eval-YYYYMMDD.json --out reports/rubric-x-calibration-YYYYMMDD.md
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

from scripts.phase1_probe import eval_compress

REPO_ROOT = Path(__file__).resolve().parent.parent
FIDELITY_DIM = "保真"       # 轨道 2 的保真维（DIMS 之一，0/1/2）
HISTORY_RE = re.compile(r"<新增历史>\n(.*)\n</新增历史>", re.S)


def p95(values: list[float]) -> float:
    """P95（**最近秩法**：idx = ceil(0.95·n) − 1）。口径写死在此，避免各人各算。"""
    if not values:
        raise ValueError("P95 需要非空样本")
    s = sorted(values)
    idx = max(0, math.ceil(0.95 * len(s)) - 1)
    return s[idx]


def round_up_to_5pp(value: float) -> float:
    """向上取整到 5pp（决策 13 原文）。0 → 0（不是 5pp）。"""
    return math.ceil(round(value * 100, 6) / 5) * 5 / 100


def history_of(sample: dict) -> str:
    """从出库样本的 `input`（生产同款 user 段）里取出「新增历史」段 —— 即轨道 1 的材料。"""
    m = HISTORY_RE.search(sample.get("input", ""))
    if not m:
        raise ValueError(f"样本 {sample.get('id')} 的 input 不含 <新增历史> 段")
    return m.group(1)


def calibrate(samples: list[dict], report: dict) -> dict:
    """逐样本对齐两条轨道 → diff 分布 → X。对不齐/无实体一律**报出来**，不静默补零。"""
    scores = {r["id"]: r for r in report.get("scores", [])}
    rows, skipped_no_entities, probes_excluded = [], [], []
    for s in samples:
        sc = scores.get(s["id"])
        if sc is None:                      # 探针位样本的成绩不进干净分布（run_eval 的设计）
            probes_excluded.append(s["id"])
            continue
        t1 = eval_compress({"new_text": history_of(s), "npc_names": []}, s.get("output", ""))
        if t1["preserve_rate"] is None:     # 抽不出关键串 = 未知，不是 0（"空 = 未知 ≠ 通过"）
            skipped_no_entities.append(s["id"])
            continue
        t2 = int(sc[FIDELITY_DIM]) / 2      # 轨道 2 折到 0~1
        rows.append({"id": s["id"], "track1": t1["preserve_rate"], "track2": t2,
                     "n_entities": t1["n_entities"], "long_input": bool(s.get("long_input")),
                     "diff": abs(t1["preserve_rate"] - t2)})
    diffs = [r["diff"] for r in rows]
    return {
        "n": len(rows),
        "rows": rows,
        "p50": p95_sorted(diffs, 0.50) if diffs else None,
        "p95": p95(diffs) if diffs else None,
        "max": max(diffs) if diffs else None,
        "x": round_up_to_5pp(p95(diffs)) if diffs else None,
        "skipped_no_entities": skipped_no_entities,
        "probes_excluded": probes_excluded,
    }


def p95_sorted(values: list[float], q: float) -> float:
    """任意分位（最近秩法）——P50 与 P95 用同一口径，避免两个数来自两套算法。"""
    s = sorted(values)
    idx = max(0, math.ceil(q * len(s)) - 1)
    return s[idx]


def format_md(cal: dict, *, samples_path: str, report_path: str, report_meta: dict) -> str:
    x = cal["x"]
    out = [
        "# 决策 13「定 X」校准报告",
        "",
        f"**① X 是什么量**：轨道 1（规则保全率）与轨道 2（rubric **{FIDELITY_DIM}**维 ÷ 2 折算）"
        "在**同一样本**上的绝对差 `diff_i = |保全率_i − 保真_i/2|`，单位 **pp**。",
        "",
        f"**② 定在多少**：**X = {x * 100:.0f}pp**"
        f"（diff 的 P95 = {cal['p95'] * 100:.1f}pp，按决策 13 向上取整到 5pp；"
        f"P50 = {cal['p50'] * 100:.1f}pp，max = {cal['max'] * 100:.1f}pp）。",
        "",
        f"**③ 依据哪批数据**：`{samples_path}`（对齐 {cal['n']} 条；"
        f"探针位排除 {len(cal['probes_excluded'])} 条；无关键串跳过 {len(cal['skipped_no_entities'])} 条）"
        f" + 轨道 2 报告 `{report_path}`"
        f"（探针检出率 {report_meta.get('probe_detection')}、批有效 {report_meta.get('batch_valid')}）。",
        "",
        "## 逐样本对照",
        "",
        "| id | 轨道1 保全率 | 轨道2 保真/2 | n 实体 | 长输入 | diff(pp) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in sorted(cal["rows"], key=lambda r: -r["diff"]):
        out.append(f"| `{r['id']}` | {r['track1']:.3f} | {r['track2']:.2f} | {r['n_entities']} | "
                   f"{'是' if r['long_input'] else '否'} | {r['diff'] * 100:.1f} |")
    if cal["skipped_no_entities"]:
        out += ["", f"> 跳过（抽不出关键串，**未知不是 0**）：{', '.join(cal['skipped_no_entities'])}"]
    if cal["probes_excluded"]:
        out += ["", f"> 探针位（成绩不进干净分布）：{', '.join(cal['probes_excluded'])}"]
    out += [
        "",
        "## 指纹（spec §6.4）",
        "",
        f"- `prompt_version` = `{report_meta.get('prompt_version')}`"
        f" · `budget_policy` = `{report_meta.get('budget_policy')}`",
        f"- `judge_model` = `{report_meta.get('judge_model')}`"
        f" · `endpoint` = `{json.dumps(report_meta.get('endpoint'), ensure_ascii=False)}`",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="决策 13 定 X（轨道 1 × 轨道 2 对齐）")
    ap.add_argument("--samples", required=True, help="compress 出库 jsonl")
    ap.add_argument("--report", required=True, help="轨道 2 报告 json")
    ap.add_argument("--out", default=None, help="Markdown 报告输出路径")
    args = ap.parse_args(argv)

    samples = [json.loads(line) for line in
               Path(args.samples).read_text(encoding="utf-8").splitlines() if line.strip()]
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    if not report.get("batch_valid"):
        print("[✗] 轨道 2 批无效（探针检出率不达标）——按 spec：批作废则先修评委提示词，不定 X")
        return 1
    cal = calibrate(samples, report)
    if not cal["n"]:
        print("[✗] 没有对齐上的样本，无法定 X")
        return 1
    md = format_md(cal, samples_path=args.samples, report_path=args.report, report_meta=report)
    print(md)
    if args.out:
        Path(args.out).write_text(md + "\n", encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

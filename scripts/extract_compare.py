"""extract 提示词实验的**同集对照**工具（离线，零 API）：把两轮报告放到**同一把尺子**下重判。

**为什么要这个工具**（决策 15 / 决策 20）：两侧基线报告
（`reports/extract-eval-20260912-141241.json` = 14B、`…-133829.json` = flash）
跑在**极性修正前**的冻结集上（`n_funds` 当时是正例，现在是否例）——
直接读它们的 `summary` 做前后对照就是"换尺子比数"。本工具读两轮报告的**原始输出**
`raw_output`，按**当前** `eval-sets/extract/direct.yaml` 的标签重新判分
（判分函数直接复用 `scripts/extract_eval.score_case`，不另写一套口径），再出对照表。

**run 级分类**（正例）：
- `A`：召回全中；`B`：有输出但漏项；`C`：**输出 0 条**（"恒输出「无」"的保守判定）
- 负例：`N_ok` / `N_bad`（负例抽出事实 = 违规，"偏向输出"要防的正是这个）

用法：
    python scripts/extract_compare.py reports/old.json reports/new.json
    python scripts/extract_compare.py old.json new.json --json-out reports/xxx.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from game_agent.memory import parse_facts
from scripts.extract_eval import EVAL_SET, score_case

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASS_A, CLASS_B, CLASS_C = "A", "B", "C"


def load_cases(path: Path | None = None) -> dict[str, dict]:
    """读当前冻结集 → {id: case}。标签以**当前**文件为准（这正是重切片的意义）。"""
    data = yaml.safe_load((path or EVAL_SET).read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["cases"]}


def rescore(report: dict, cases: dict[str, dict]) -> dict:
    """把一轮报告的原始输出按**当前**标签重判 → 汇总。缺数据/对不齐一律报错，不静默缩分母。"""
    runs = report.get("cases") or []
    if not runs:
        raise ValueError("报告里没有 cases（不是 extract-eval 报告？）")
    no_raw = [r.get("id") for r in runs if "raw_output" not in r]
    if no_raw:
        raise ValueError(f"报告缺 raw_output，无法离线重切片：{no_raw[:3]}（共 {len(no_raw)} 条）")
    run_ids = {r["id"] for r in runs}
    only_old = sorted(run_ids - set(cases))
    only_new = sorted(set(cases) - run_ids)
    if only_old or only_new:
        raise ValueError(f"用例对不齐：报告多出 {only_old}；报告缺少 {only_new}")

    per_case: dict[str, list[dict]] = {}
    for r in runs:
        case = cases[r["id"]]
        scored = score_case(case, parse_facts(r["raw_output"]))
        if case["expect_empty"]:
            scored["cls"] = "N_ok" if scored["passed"] else "N_bad"
        elif scored["n_facts"] == 0:
            scored["cls"] = CLASS_C
        elif not scored["recalled"]:
            scored["cls"] = CLASS_B
        else:
            scored["cls"] = CLASS_A
        per_case.setdefault(r["id"], []).append(scored)

    pos_ids = [cid for cid in cases if not cases[cid]["expect_empty"] and cid in per_case]
    neg_ids = [cid for cid in cases if cases[cid]["expect_empty"] and cid in per_case]
    pos_runs = [r for cid in pos_ids for r in per_case[cid]]
    neg_runs = [r for cid in neg_ids for r in per_case[cid]]
    all_scored = [r for cid in per_case for r in per_case[cid]]
    return {
        "n_runs": len(runs),
        "n_cases": len(per_case),
        "pos_runs": len(pos_runs),
        "pos_hits": sum(1 for r in pos_runs if r["recalled"]),
        "pos_c_runs": sum(1 for r in pos_runs if r["cls"] == CLASS_C),
        "pos_c_cases": [cid for cid in pos_ids if all(r["cls"] == CLASS_C for r in per_case[cid])],
        "pos_b_runs": sum(1 for r in pos_runs if r["cls"] == CLASS_B),
        "neg_runs": len(neg_runs),
        "neg_ok": sum(1 for r in neg_runs if r["passed"]),
        "neg_facts": sum(r["n_facts"] for r in neg_runs),  # 负例被污染的总条数
        "dedup_bad": sum(1 for r in all_scored if not r["dedup_ok"]),
        "flipped": [cid for cid in per_case if len({r["passed"] for r in per_case[cid]}) > 1],
    }


def verdict(old: dict, new: dict) -> dict:
    """决策 15 的两条封板判据（1、2 同时成立才算达标）+ 参考项 3。"""
    c_ok = new["pos_c_runs"] * 3 <= old["pos_c_runs"]        # ≤ 原来的 1/3，整数安全
    neg_ok = new["neg_runs"] == old["neg_runs"] and new["neg_ok"] == new["neg_runs"]
    return {
        "c_ok": c_ok,
        "neg_ok": neg_ok,
        "recall_up": new["pos_hits"] > old["pos_hits"],
        "pass": c_ok and neg_ok,
    }


def compare(old: dict, new: dict) -> dict:
    return {
        "old": old,
        "new": new,
        "delta": {
            "pos_c_runs": new["pos_c_runs"] - old["pos_c_runs"],
            "pos_hits": new["pos_hits"] - old["pos_hits"],
            "neg_ok": new["neg_ok"] - old["neg_ok"],
        },
        # 被治好的"恒「无」"用例 / 新出现的（后者=回归，必须为空才好看）
        "c_cleared": [c for c in old["pos_c_cases"] if c not in new["pos_c_cases"]],
        "c_new": [c for c in new["pos_c_cases"] if c not in old["pos_c_cases"]],
    }


def format_md(cmp: dict, old_label: str = "旧", new_label: str = "新") -> str:
    old, new = cmp["old"], cmp["new"]
    v = verdict(old, new)
    rows = [
        ("正例 run 数", old["pos_runs"], new["pos_runs"]),
        ("召回 run 数", old["pos_hits"], new["pos_hits"]),
        ("**C 类 run（输出 0 条）**", old["pos_c_runs"], new["pos_c_runs"]),
        ("C 类用例数", len(old["pos_c_cases"]), len(new["pos_c_cases"])),
        ("B 类 run（漏项）", old["pos_b_runs"], new["pos_b_runs"]),
        ("负例正确 run", f"{old['neg_ok']}/{old['neg_runs']}", f"{new['neg_ok']}/{new['neg_runs']}"),
        ("负例抽出事实总数", old["neg_facts"], new["neg_facts"]),
        ("去重违规 run", old["dedup_bad"], new["dedup_bad"]),
        ("翻转用例数", len(old["flipped"]), len(new["flipped"])),
    ]
    out = [f"| 指标 | {old_label} | {new_label} | Δ |", "| --- | --- | --- | --- |"]
    for name, o, n in rows:
        out.append(f"| {name} | {o} | {n} | {n - o:+d} |" if isinstance(o, int) else f"| {name} | {o} | {n} | — |")
    out.append("")
    out.append(
        f"**判据**：① C 类 run ≤ 旧值 1/3 → {'✅' if v['c_ok'] else '❌'}"
        f"（{new['pos_c_runs']} vs 上限 {old['pos_c_runs'] // 3}）"
        f"　② 负例不退化 → {'✅' if v['neg_ok'] else '❌'}"
        f"（{new['neg_ok']}/{new['neg_runs']}）"
        f"　③（参考）召回回升 → {'✅' if v['recall_up'] else '❌'}"
        f"（{old['pos_hits']} → {new['pos_hits']}）"
    )
    out.append(f"**结论**：{'达标（进入「再定训练量」）' if v['pass'] else '未达标（回到「必训」）'}")
    if cmp["c_cleared"]:
        out.append(f"- 治好的「恒「无」」用例（{len(cmp['c_cleared'])}）：{', '.join(cmp['c_cleared'])}")
    out.append(
        f"- 新出现的「恒「无」」用例（{len(cmp['c_new'])}）："
        f"{', '.join(cmp['c_new']) if cmp['c_new'] else '（无）'}"
    )
    out.append(f"- 仍恒「无」：{', '.join(new['pos_c_cases']) if new['pos_c_cases'] else '（无）'}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="extract 报告同集对照（离线重切片）")
    ap.add_argument("old", type=Path, help="旧报告（基线）")
    ap.add_argument("new", type=Path, help="新报告（复测）")
    ap.add_argument("--json-out", type=Path, default=None, help="把对照结果同时写成 JSON")
    args = ap.parse_args(argv)

    cases = load_cases()
    old_rep = json.loads(args.old.read_text(encoding="utf-8"))
    new_rep = json.loads(args.new.read_text(encoding="utf-8"))
    print(f"对照集：{EVAL_SET.relative_to(REPO_ROOT)} · {len(cases)} 例"
          f"（正 {sum(1 for c in cases.values() if not c['expect_empty'])} / "
          f"负 {sum(1 for c in cases.values() if c['expect_empty'])}）")
    print(f"  旧：{args.old.name} · model={old_rep.get('model')} · pv={old_rep.get('prompt_version')}")
    print(f"  新：{args.new.name} · model={new_rep.get('model')} · pv={new_rep.get('prompt_version')}\n")

    cmp = compare(rescore(old_rep, cases), rescore(new_rep, cases))
    md = format_md(cmp, old_label=f"旧（{old_rep.get('prompt_version')}）",
                   new_label=f"新（{new_rep.get('prompt_version')}）")
    print(md)
    if args.json_out:
        payload = {
            "eval_set": str(EVAL_SET.relative_to(REPO_ROOT)),
            "eval_set_sha256": new_rep.get("eval_set_sha256"),
            "old_report": args.old.name, "new_report": args.new.name,
            "old_model": old_rep.get("model"), "new_model": new_rep.get("model"),
            "old_prompt_version": old_rep.get("prompt_version"),
            "new_prompt_version": new_rep.get("prompt_version"),
            "compare": cmp, "verdict": verdict(cmp["old"], cmp["new"]), "markdown": md,
        }
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n对照结果已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

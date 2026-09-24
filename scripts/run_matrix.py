"""③ runtime 平台化：同任务 N 次跑对比（worldpack_smoke × seed 扫描）。

换 prompt / 模型 / 记忆策略后的 A/B 用（与 replay 互补：replay 看单回合，
run_matrix 看整局分布）。

用法::

    python scripts/run_matrix.py --pack world-packs/ancient_jianghu --runs 5 [--max-cost 1.5]

- 每次跑独立 seed 与产物目录（临时目录，跑完只留指标）；
- 指标：退出码 / 审计零偏差 / 禁表零泄漏 / 结局 / 成本；
- 成本护栏：累计成本超 --max-cost 即停（沿工厂"超预算停批"纪律）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from scripts.route_a_cost import load_usage, summarize as summarize_usage

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RunResult:
    seed: int
    exit_code: int
    audit_pass: bool | None = None
    forbidden_pass: bool | None = None
    ending: str = ""
    cost: float = 0.0
    cost_report: str = ""


def run_one(pack: str, seed: int, out_dir: Path) -> RunResult:
    """跑一次冒烟并解析结果（退出码 / 审计 / 禁表 / 结局 / 成本）。"""
    r = RunResult(seed=seed, exit_code=-1)
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "worldpack_smoke.py"),
         "--pack", pack, "--seed", str(seed),
         "--out-prefix", f"matrix-{seed}", "--out-dir", str(out_dir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    r.exit_code = proc.returncode
    txt_files = list(out_dir.glob(f"matrix-{seed}-*.txt"))
    if txt_files:
        text = txt_files[0].read_text(encoding="utf-8")
        r.audit_pass = "数值零偏差审计通过" in text
        r.forbidden_pass = "禁表扫描通过" in text
        if "【结局】" in text:
            r.ending = text.split("【结局】", 1)[1].strip().splitlines()[0][:30]
        elif "[未达成结局]" in text:
            r.ending = "未达成"
    usage_files = list(out_dir.glob("usage-matrix-*.jsonl"))
    if usage_files:
        summary = summarize_usage(load_usage(usage_files[0]))
        r.cost = summary.get("total", {}).get("cost", 0.0)
        r.cost_report = usage_files[0].name
    return r


def build_table(results: list[RunResult]) -> dict:
    """结果表：逐次明细 + 汇总（退出码 0 率 / 审计率 / 禁表率 / 结局分布 / 成本均值与方差）。"""
    n = len(results)
    costs = [r.cost for r in results]
    mean_cost = sum(costs) / n if n else 0.0
    var_cost = sum((c - mean_cost) ** 2 for c in costs) / n if n else 0.0
    return {
        "runs": [
            {
                "seed": r.seed,
                "exit_code": r.exit_code,
                "audit_pass": r.audit_pass,
                "forbidden_pass": r.forbidden_pass,
                "ending": r.ending,
                "cost": round(r.cost, 4),
            }
            for r in results
        ],
        "summary": {
            "n": n,
            "exit0_rate": round(sum(1 for r in results if r.exit_code == 0) / n, 3) if n else None,
            "audit_pass_rate": round(sum(1 for r in results if r.audit_pass) / n, 3) if n else None,
            "forbidden_pass_rate": round(sum(1 for r in results if r.forbidden_pass) / n, 3) if n else None,
            "endings": sorted({r.ending for r in results if r.ending}),
            "mean_cost": round(mean_cost, 4),
            "cost_std": round(var_cost ** 0.5, 4),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_matrix", description="同任务 N 次跑对比")
    parser.add_argument("--pack", default="world-packs/ancient_jianghu")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--max-cost", type=float, default=1.5, help="累计成本上限（超出即停）")
    args = parser.parse_args(argv)

    results: list[RunResult] = []
    spent = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        for seed in range(1, args.runs + 1):
            if spent >= args.max_cost:
                print(f"[!] 累计成本 {spent:.2f} 已达上限 {args.max_cost}，停止（已完成 {len(results)} 次）")
                break
            r = run_one(args.pack, seed, out_dir)
            spent += r.cost
            results.append(r)
            print(f"  seed {seed}: exit={r.exit_code} 审计={r.audit_pass} "
                  f"禁表={r.forbidden_pass} 结局={r.ending or '-'} ¥{r.cost:.3f}")

    table = build_table(results)
    s = table["summary"]
    print("\n===== run_matrix 汇总 =====")
    print(f"退出码 0 率 {s['exit0_rate']} · 审计通过率 {s['audit_pass_rate']} · "
          f"禁表通过率 {s['forbidden_pass_rate']}")
    print(f"结局分布 {s['endings']} · 单次成本均值 ¥{s['mean_cost']} ± ¥{s['cost_std']}")

    out = REPO_ROOT / "reports" / f"run_matrix_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

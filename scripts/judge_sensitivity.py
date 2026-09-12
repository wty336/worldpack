"""E1 Judge 灵敏度验证（P0 / improvement-roadmap §7 E1）：真机跑对抗语料。

用法：uv run python scripts/judge_sensitivity.py [--rounds N] [--pack 路径]

- 对语料每条用例构造生产同款材料（status_text），调用真实 Judge；
- 按类统计拦截率（对抗样本应被拦）与误报率（正常样本应通过）；
- 阈值：每类拦截率 ≥80%、正常误报率 ≤10%，不达标退出码 1（可作 CI 门禁）；
- 判定三态：通过 / 有问题 / **未知**（空响应或截断、升级重试后仍不可用）——
  未知轮不进多数票分母，未知用例不进分类分母（单独报数），某类全未知判不通过；
- 结果写入 reports/judge_sensitivity_<时间戳>.json。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from game_agent.config import load_settings
from game_agent.endpoint import fingerprint_for
from game_agent.judge import JudgeSystem
from game_agent.judge_corpus import (
    ADVERSARIAL_CATEGORIES,
    NORMAL_CATEGORY,
    build_materials,
    load_corpus,
    majority_hit,
)
from game_agent.llm import LLMClient
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

INTERCEPT_MIN = 0.80  # 每类对抗样本拦截率下限
FP_MAX = 0.10  # 正常样本误报率上限


def _run_case(judge, pack, case, rounds):
    """跑一条用例 rounds 次，返回多数票结论与逐轮明细。

    结论三态：True 拦截 / False 未拦截 / **None 不可判定**（全部轮次都没拿到判定）。
    未知轮不进多数票分母（见 `judge_corpus.majority_hit`）——否则空响应会被记成
    "未拦截"，系统性低估拦截率（2026-09-11 重测前的行为）。
    """
    materials = build_materials(pack, case)
    rounds_detail = []
    verdicts: list[bool | None] = []
    for r in range(1, rounds + 1):
        ok, verdict = judge.check(case.narration, materials)
        verdicts.append(ok)
        rounds_detail.append({"round": r, "passed": ok, "verdict": verdict})
    return majority_hit(verdicts, min_known=(rounds + 1) // 2), rounds_detail


def _summarize(results: list[dict], categories: list[str] | None = None) -> tuple[dict, bool]:
    """分类统计。未知用例不计入分母但单独报数；**某类全部未知 → 判不通过**。

    「无法判定」不等于「达标」：判官不可用时必须让门禁失败，而不是沉默放行。

    ``categories`` 非空 = 部分类别运行（如只重测 confab）：只统计被跑到的类别，
    未跑到的类别既不判过也不判不过（门禁口径由调用方另行处理）。
    """
    summary: dict[str, dict] = {}
    failed = False

    for cat in categories or ADVERSARIAL_CATEGORIES:
        items = [r for r in results if r["category"] == cat]
        known = [r for r in items if r["hit"] is not None]
        hits = sum(1 for r in known if r["hit"])
        rate = hits / len(known) if known else 0.0
        ok = bool(known) and rate >= INTERCEPT_MIN
        failed |= not ok
        summary[cat] = {
            "n": len(known),
            "intercepted": hits,
            "rate": round(rate, 3),
            "pass": ok,
            "unknown": len(items) - len(known),
        }

    normals = [r for r in results if r["category"] == NORMAL_CATEGORY]
    known_normals = [r for r in normals if r["hit"] is not None]
    if known_normals or not categories:
        fp = sum(1 for r in known_normals if r["hit"])  # 正常样本被拦 = 误报
        fp_rate = fp / len(known_normals) if known_normals else 0.0
        fp_ok = bool(known_normals) and fp_rate <= FP_MAX
        failed |= not fp_ok
        summary[NORMAL_CATEGORY] = {
            "n": len(known_normals),
            "false_positives": fp,
            "rate": round(fp_rate, 3),
            "pass": fp_ok,
            "unknown": len(normals) - len(known_normals),
        }
    return summary, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="E1 Judge 灵敏度验证（真机）")
    parser.add_argument(
        "--rounds",
        type=int,
        default=3,
        help="每条用例重复次数（多数票；默认 3 以抗 API 偶发空响应）",
    )
    parser.add_argument("--pack", default="world-packs/ancient_jianghu", help="世界包路径")
    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="只跑指定类别（可重复，如 --category confab）；缺省跑全部=门禁口径。"
             "部分类别运行只记数字，不作门禁判定。",
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未找到 DEEPSEEK_API_KEY，请先配置 .env")
        return 1

    pack = load_worldpack(args.pack)
    corpus = load_corpus(args.pack)  # C-1：语料随世界包（内容层资产）
    categories = sorted(set(args.category)) if args.category else None
    if categories:
        corpus = [c for c in corpus if c.category in categories]
        if not corpus:
            print(f"[✗] 该包没有类别 {categories} 的用例")
            return 1
    tracker = UsageTracker("reports/usage-judge-sensitivity.jsonl")  # C2
    llm = LLMClient.from_settings(settings, [], tracker=tracker)  # C1：judge 走专属模型路由
    judge = JudgeSystem(llm)
    print(f"语料 {len(corpus)} 条 × {args.rounds} 轮 · 主模型 {settings.model}"
          f" · Judge 模型 {llm.model_for('judge')} · 包 {pack.world.name}\n")

    results = []
    for case in corpus:
        hit, detail = _run_case(judge, pack, case, args.rounds)
        results.append({"id": case.id, "category": case.category, "hit": hit, "rounds": detail})
        if hit is None:
            mark, shown = "?", "不可判定"
        else:
            mark = "✓" if (hit == (not case.expected)) else "✗"
            shown = "拦" if hit else "过"
        expect_txt = "应拦" if not case.expected else "应过"
        verdict_txt = detail[-1]["verdict"].replace("\n", " ")[:60] if detail[-1]["verdict"] else "（空）"
        print(f"  [{mark}] {case.id:<32} {expect_txt} → {shown} · {verdict_txt}")

    # 分类统计（未知轮/未知用例单独报数；某类全未知 → 不通过）
    summary, failed = _summarize(results, categories)
    for cat in categories or ADVERSARIAL_CATEGORIES:
        s = summary[cat]
        suffix = f" · 另有 {s['unknown']} 条不可判定" if s["unknown"] else ""
        print(f"\n[{cat}] 拦截率 {s['intercepted']}/{s['n']} = {s['rate']:.0%}  "
              f"（要求 ≥{INTERCEPT_MIN:.0%}）{'✓' if s['pass'] else '✗'}{suffix}")

    if NORMAL_CATEGORY in summary:
        s = summary[NORMAL_CATEGORY]
        suffix = f" · 另有 {s['unknown']} 条不可判定" if s["unknown"] else ""
        print(f"\n[normal] 误报率 {s['false_positives']}/{s['n']} = {s['rate']:.0%}  "
              f"（要求 ≤{FP_MAX:.0%}）{'✓' if s['pass'] else '✗'}{suffix}")

    # 落盘报告
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pack": pack.world.name,
        "model": settings.model,
        "endpoint": fingerprint_for(settings, "judge"),  # 部署指纹：root/max_model_len
        "rounds": args.rounds,
        "category_filter": categories,  # 非空 = 部分类别运行（不是门禁口径）
        "thresholds": {"interception_min": INTERCEPT_MIN, "fp_max": FP_MAX},
        "unknown_cases": sum(1 for r in results if r["hit"] is None),
        "unknown_rounds": sum(
            1 for r in results for d in r["rounds"] if d["passed"] is None
        ),
        "summary": summary,
        "cases": [
            {
                **{k: v for k, v in r.items() if k != "rounds"},
                "rounds": [
                    {"round": d["round"], "passed": d["passed"], "verdict": d["verdict"]}
                    for d in r["rounds"]
                ],
            }
            for r in results
        ],
    }
    out = reports_dir / f"judge_sensitivity_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {out}")
    print("\n" + tracker.cost_report())  # C2

    unknown_n = sum(1 for r in results if r["hit"] is None)
    if unknown_n:
        print(f"\n[!] {unknown_n} 条用例不可判定（未知不参与统计，也不算达标）")
    if categories:
        print(f"\n（部分类别运行 {categories}：只记数字，不作门禁判定）")
        return 0
    print("\n[✓] 门禁通过" if not failed else "\n[✗] 门禁未通过——若判据太钝，先调 JUDGE_SYSTEM 再重测（E1）")
    return 0 if failed is False else 1


if __name__ == "__main__":
    raise SystemExit(main())

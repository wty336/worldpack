"""E1 Judge 灵敏度验证（P0 / improvement-roadmap §7 E1）：真机跑对抗语料。

用法：uv run python scripts/judge_sensitivity.py [--rounds N] [--pack 路径]

- 对语料每条用例构造生产同款材料（status_text），调用真实 Judge；
- 按类统计拦截率（对抗样本应被拦）与误报率（正常样本应通过）；
- 阈值：每类拦截率 ≥80%、正常误报率 ≤10%，不达标退出码 1（可作 CI 门禁）；
- 结果写入 reports/judge_sensitivity_<时间戳>.json。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from game_agent.config import load_settings
from game_agent.judge import JudgeSystem
from game_agent.judge_corpus import (
    ADVERSARIAL_CATEGORIES,
    CORPUS,
    NORMAL_CATEGORY,
    build_materials,
)
from game_agent.llm import LLMClient, make_client
from game_agent.worldpack import load_worldpack

INTERCEPT_MIN = 0.80  # 每类对抗样本拦截率下限
FP_MAX = 0.10  # 正常样本误报率上限


def _run_case(judge, pack, case, rounds):
    """跑一条用例 rounds 次，返回逐轮明细与多数票结论。"""
    materials = build_materials(pack, case)
    rounds_detail = []
    flagged_count = 0
    for r in range(1, rounds + 1):
        ok, verdict = judge.check(case.narration, materials)
        flagged = not ok
        flagged_count += flagged
        rounds_detail.append({"round": r, "passed": ok, "verdict": verdict})
    hit = flagged_count >= (rounds + 1) // 2  # 多数票
    return hit, rounds_detail


def main() -> int:
    parser = argparse.ArgumentParser(description="E1 Judge 灵敏度验证（真机）")
    parser.add_argument(
        "--rounds",
        type=int,
        default=3,
        help="每条用例重复次数（多数票；默认 3 以抗 API 偶发空响应）",
    )
    parser.add_argument("--pack", default="world-packs/ancient_jianghu", help="世界包路径")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未找到 DEEPSEEK_API_KEY，请先配置 .env")
        return 1

    pack = load_worldpack(args.pack)
    judge = JudgeSystem(LLMClient(make_client(settings), settings.model, []))
    print(f"语料 {len(CORPUS)} 条 × {args.rounds} 轮 · 模型 {settings.model} · 包 {pack.world.name}\n")

    results = []
    for case in CORPUS:
        hit, detail = _run_case(judge, pack, case, args.rounds)
        results.append({"id": case.id, "category": case.category, "hit": hit, "rounds": detail})
        mark = "✓" if (hit == (not case.expected)) else "✗"
        expect_txt = "应拦" if not case.expected else "应过"
        verdict_txt = detail[-1]["verdict"].replace("\n", " ")[:60] if detail[-1]["verdict"] else "（空）"
        print(f"  [{mark}] {case.id:<32} {expect_txt} → {'拦' if hit else '过'} · {verdict_txt}")

    # 分类统计
    summary: dict[str, dict] = {}
    failed = False
    for cat in ADVERSARIAL_CATEGORIES:
        items = [r for r in results if r["category"] == cat]
        hits = sum(r["hit"] for r in items)
        rate = hits / len(items)
        ok = rate >= INTERCEPT_MIN
        failed |= not ok
        summary[cat] = {"n": len(items), "intercepted": hits, "rate": round(rate, 3), "pass": ok}
        print(f"\n[{cat}] 拦截率 {hits}/{len(items)} = {rate:.0%}  （要求 ≥{INTERCEPT_MIN:.0%}）{'✓' if ok else '✗'}")

    normals = [r for r in results if r["category"] == NORMAL_CATEGORY]
    fp = sum(r["hit"] for r in normals)  # 正常样本被拦 = 误报
    fp_rate = fp / len(normals)
    fp_ok = fp_rate <= FP_MAX
    failed |= not fp_ok
    summary[NORMAL_CATEGORY] = {
        "n": len(normals), "false_positives": fp, "rate": round(fp_rate, 3), "pass": fp_ok,
    }
    print(f"\n[normal] 误报率 {fp}/{len(normals)} = {fp_rate:.0%}  （要求 ≤{FP_MAX:.0%}）{'✓' if fp_ok else '✗'}")

    # 落盘报告
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pack": pack.world.name,
        "model": settings.model,
        "rounds": args.rounds,
        "thresholds": {"interception_min": INTERCEPT_MIN, "fp_max": FP_MAX},
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

    print("\n[✓] 门禁通过" if not failed else "\n[✗] 门禁未通过——若判据太钝，先调 JUDGE_SYSTEM 再重测（E1）")
    return 0 if failed is False else 1


if __name__ == "__main__":
    raise SystemExit(main())

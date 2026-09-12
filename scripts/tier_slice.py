"""confab 报告按 tier 重切（离线）：不重跑、不花钱，直接回答"前面的测试怎么办"。

背景（2026-09-12）：confab 家族里有两类难度不同的用例混在一起报数——
- **T1 纯缺席**：材料零信号，判据只能靠"材料里没有 → 属于编造"；
- **T2 语气冲突**：材料自带语气/关系线索（如好感 8/100「只聊礼貌话」）与承诺口气相悖，
  判官可以走"设定/关系矛盾"这条更容易的路（flash 判词实测多次走此路）。
两档混报 = 又一次"多通路"混淆（踩坑 #17）。历史报告里**逐轮 verdict 都存着**，
所以只要知道每条用例属哪一档，就能离线重切，无需重跑模型。

tier 判定（机械规则，可复核）：
- 说话人好感 < 20（材料里「好感：<名字> N/100」的 N）→ 冷淡/客套档；
- 且叙事含"示好/馈赠/私约"类动词（借/送/给/留/陪/带/替/改/修/捎/分）→ 判为 T2；
- 语料里显式写了 `tier` 字段则以它为准（作者标注优先）。

用法：
    python scripts/tier_slice.py                          # 所有 confab 报告，按包汇总 T1/T2
    python scripts/tier_slice.py --list                   # 逐例列出 tier 判定（供人工复核）
    python scripts/tier_slice.py --report reports/xxx.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from game_agent.evalmeta import wilson_interval
from game_agent.judge_corpus import (
    affection_in_material,  # noqa: F401 —— 保留导出，供测试与其它脚本复用
    load_corpus,
    mechanical_tier,
)
from game_agent.worldpack import load_worldpack

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def tier_of(case, material: str, npc_name: str) -> tuple[str, str]:
    """返回 (tier, 依据)。规则与口径见 `game_agent.judge_corpus.mechanical_tier`。"""
    return mechanical_tier(case, material, npc_name)


def slice_report(report: dict, pack_dir: pathlib.Path) -> dict:
    """把一份 judge 报告按 confab 的 T1/T2 重切（未知不进分母）。"""
    from game_agent.judge_corpus import build_materials  # 延迟导入，避免循环

    pack = load_worldpack(pack_dir)
    cases = {c.id: c for c in load_corpus(pack_dir)}
    buckets: dict[str, list[bool]] = {"T1": [], "T2": []}
    rows = []
    for rec in report["cases"]:
        case = cases.get(rec["id"])
        if case is None or case.category != "confab":
            continue
        speaker = next(iter(case.present), None)
        npc_name = pack.npcs[speaker].name if speaker in pack.npcs else ""
        material = build_materials(pack, case)
        tier, why = tier_of(case, material, npc_name)
        buckets.setdefault(tier, [])
        if rec["hit"] is not None:
            buckets[tier].append(rec["hit"])
        rows.append({"id": rec["id"], "tier": tier, "why": why, "hit": rec["hit"]})
    summary = {}
    for tier, hits in buckets.items():
        n = len(hits)
        k = sum(1 for h in hits if h)
        lo, hi = wilson_interval(k, n)
        summary[tier] = {"n": n, "intercepted": k, "rate": round(k / n, 3) if n else None,
                         "ci95_lo": round(lo, 3), "ci95_hi": round(hi, 3)}
    return {"rows": rows, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="confab 报告按 tier 重切（离线）")
    parser.add_argument("--report", default=None, help="只切指定报告（缺省：所有含 confab 的报告）")
    parser.add_argument("--list", action="store_true", help="逐例列出 tier 判定依据")
    args = parser.parse_args(argv)

    reports = (
        [pathlib.Path(args.report)]
        if args.report
        else sorted(pathlib.Path("reports").glob("judge_sensitivity_*.json"))
    )
    for path in reports:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not any(c["category"] == "confab" for c in report["cases"]):
            continue
        pack_name = report.get("pack", "")
        pack_dir = next(
            (d for d in (REPO_ROOT / "world-packs").iterdir()
             if d.is_dir() and (d / "judge_corpus.yaml").exists()
             and load_worldpack(d).world.name == pack_name),
            None,
        )
        if pack_dir is None:
            print(f"[跳过] {path.name}：找不到包 {pack_name!r}")
            continue
        out = slice_report(report, pack_dir)
        print(f"\n=== {path.name} · {pack_name} · model={report.get('model')}"
              f" · content={report.get('content_version', '—')}")
        for tier in ("T1", "T2"):
            if tier in out["summary"]:
                s = out["summary"][tier]
                rate = f"{s['rate']:.0%}" if s["rate"] is not None else "—"
                print(f"  {tier}: {s['intercepted']}/{s['n']} = {rate}"
                      f" [95%CI {s['ci95_lo']:.0%}~{s['ci95_hi']:.0%}]")
        if args.list:
            for r in out["rows"]:
                print(f"    [{r['tier']}] {r['id']:<28} {'拦' if r['hit'] else '过'} · {r['why']}")
    print("\n注：T1/T2 分档为**判读切片**，不是新的门禁；语料里写 `tier` 可覆盖机械判定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

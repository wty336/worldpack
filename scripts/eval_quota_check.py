"""评测语料配额守卫：把"每包每类 ≥N"从文档里的口号变成会 FAIL 的检查。

依据（2026-09-12 §3.5.6c 与 §4.2.2）：小样本点估计会骗人 ——
normal 误报率写成 `0/18 = 0%` 读作"完美"，其实 95% 区间是 **0~17.6%**，门限 10% 根本不可判。
配额是判据可判性的前提，不是"做得更全"的锦上添花。

目标（每包）：
    ooc ≥20 · setting ≥20 · confab ≥20（对抗合计 ≥60）· normal ≥40
    confab 内 T1（纯缺席）占比 ≥50%（口径见 judge_corpus.mechanical_tier）

用法：
    python scripts/eval_quota_check.py            # 只报告
    python scripts/eval_quota_check.py --gate     # 不达标则退出码 1（可入 CI）
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from game_agent.judge_corpus import build_materials, load_corpus, mechanical_tier
from game_agent.worldpack import load_worldpack

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = {"ooc": 20, "setting": 20, "confab": 20, "normal": 40}
ADVERSARIAL_TOTAL_MIN = 60
T1_SHARE_MIN = 0.50


def check_pack(counts: dict[str, int], t1: int, confab_n: int) -> list[str]:
    """纯函数：返回缺口/违规清单（空 = 达标）。"""
    problems: list[str] = []
    for cat, target in TARGETS.items():
        got = counts.get(cat, 0)
        if got < target:
            problems.append(f"{cat} {got}/{target}（缺 {target - got}）")
    adv = sum(counts.get(c, 0) for c in ("ooc", "setting", "confab"))
    if adv < ADVERSARIAL_TOTAL_MIN:
        problems.append(f"对抗合计 {adv}/{ADVERSARIAL_TOTAL_MIN}（缺 {ADVERSARIAL_TOTAL_MIN - adv}）")
    if confab_n:
        share = t1 / confab_n
        if share < T1_SHARE_MIN:
            problems.append(f"confab T1 占比 {share:.0%} < {T1_SHARE_MIN:.0%}"
                            f"（T1 {t1} / confab {confab_n}）")
    return problems


def scan(pack_dir: pathlib.Path) -> tuple[dict[str, int], int, int]:
    """返回 (分类计数, T1 条数, confab 条数)。"""
    counts: dict[str, int] = {}
    t1 = confab_n = 0
    pack = load_worldpack(pack_dir)
    for case in load_corpus(pack_dir):
        counts[case.category] = counts.get(case.category, 0) + 1
        if case.category != "confab":
            continue
        confab_n += 1
        speaker = next(iter(case.present), None)
        npc_name = pack.npcs[speaker].name if speaker in pack.npcs else ""
        tier, _ = mechanical_tier(case, build_materials(pack, case), npc_name)
        if tier == "T1":
            t1 += 1
    return counts, t1, confab_n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测语料配额守卫（判据可判性的前提）")
    parser.add_argument("--pack", default=None, help="只查指定包")
    parser.add_argument("--gate", action="store_true", help="有缺口则退出码 1")
    args = parser.parse_args(argv)

    packs = (
        [pathlib.Path(args.pack)]
        if args.pack
        else sorted(
            d for d in (REPO_ROOT / "world-packs").iterdir()
            if d.is_dir() and (d / "judge_corpus.yaml").exists()
        )
    )

    unqualified: list[str] = []
    for pack_dir in packs:
        counts, t1, confab_n = scan(pack_dir)
        problems = check_pack(counts, t1, confab_n)
        total = sum(counts.values())
        head = "✓ 达标" if not problems else f"⚠ {len(problems)} 项未达标"
        print(f"\n[{head}] {pack_dir.name} · 合计 {total} 条 · "
              + " ".join(f"{k}:{counts.get(k, 0)}" for k in ("ooc", "setting", "confab", "normal")))
        for p in problems:
            print(f"    - {p}")
        if problems:
            unqualified.append(pack_dir.name)

    print(f"\n目标（每包）：" + " · ".join(f"{k} ≥{v}" for k, v in TARGETS.items())
          + f" · 对抗合计 ≥{ADVERSARIAL_TOTAL_MIN} · confab T1 占比 ≥{T1_SHARE_MIN:.0%}")
    if unqualified:
        print(f"[{'✗' if args.gate else '!'}] {len(unqualified)}/{len(packs)} 个包未达标：{', '.join(unqualified)}")
        print("    未达标 = 判据在该类上**不可判**（不是'没问题'）；补语料后需两侧重测。")
        return 1 if args.gate else 0
    print("[✓] 全部包达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

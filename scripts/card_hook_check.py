"""confab 用例"卡中性"筛查（词面）：防止 promise 撞说话人角色卡。

**为什么**（2026-09-12 实证）：judge 语料的 confab 家族靠"材料中不存在该承诺"这一条
**缺席证据**通路拦截。若 promise 恰好撞上说话人角色卡的 boundaries / forbidden /
speech_style，判官就多一条"设定矛盾"通路 —— 拦截率被系统性抬高、跨包不可比。实测：
P1/P2 初版语料 10 条种子里 7 条撞卡 → 14B 拦 88%；改成卡中性后同一批用例拦 27.5%。

**口径**：叙事与角色卡（personality / speech_style / boundaries / forbidden）的**二字重合**。
二字重合 = 强信号（同一实词），但**只是必要条件**：
- 词面无关仍可能语义撞卡（如「名次给你留着」×「不私下透露其他学生的排名」——零共同二字），
  这类只能人读；故本工具是**筛查**，不是证明。
- 用法：新包/新种子进评测集前先跑 `--gate`；人工复核仍不可省。

用法：
    python scripts/card_hook_check.py                        # 全部包，只报告
    python scripts/card_hook_check.py --gate                 # 有撞卡则退出码 1
    python scripts/card_hook_check.py --pack world-packs/P1_school_letters
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

from game_agent.judge_corpus import load_corpus
from game_agent.worldpack import load_worldpack

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CARD_FIELDS = ("personality", "speech_style", "boundaries", "forbidden")
HOOK_N = 2  # 二字重合即视为词面撞卡（中文实词最短粒度）


def _grams(text: str, n: int = HOOK_N) -> set[str]:
    """只保留汉字，取 n 元组集合（标点/空白/字母数字不参与）。"""
    han = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    return {han[i : i + n] for i in range(max(0, len(han) - n + 1))}


def card_hook(narration: str, card_text: str) -> list[str]:
    """返回叙事与角色卡的共同二字串（排序）；空列表 = 词面中性。"""
    return sorted(_grams(narration) & _grams(card_text))


def scan_pack(pack_dir: pathlib.Path) -> list[dict]:
    """扫一个包里所有生成 confab 用例，返回逐条结果（无语料的目录视为非包，跳过）。"""
    if not (pack_dir / "judge_corpus.yaml").exists():
        return []
    pack = load_worldpack(pack_dir)
    out: list[dict] = []
    for case in load_corpus(pack_dir):
        if not case.id.startswith("gen_confab"):
            continue
        speaker = next(iter(case.present), None)
        spec = pack.npcs.get(speaker)
        card = " ".join(str(spec.model_dump().get(f)) for f in CARD_FIELDS) if spec else ""
        out.append(
            {
                "id": case.id,
                "speaker": speaker,
                "narration": case.narration,
                "hooked": card_hook(case.narration, card),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="confab 卡中性筛查（词面）")
    parser.add_argument("--pack", default=None, help="只扫指定包（缺省扫 world-packs 全部）")
    parser.add_argument("--gate", action="store_true", help="有撞卡则以退出码 1 结束")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = parser.parse_args(argv)

    packs = (
        [pathlib.Path(args.pack)]
        if args.pack
        else sorted(p for p in (REPO_ROOT / "world-packs").iterdir() if p.is_dir())
    )

    hooked_total = 0
    for pack_dir in packs:
        rows = scan_pack(pack_dir)
        if not rows:
            continue
        hooked = [r for r in rows if r["hooked"]]
        hooked_total += len(hooked)
        mark = "✓ 全中性" if not hooked else f"⚠ {len(hooked)} 条撞卡"
        print(f"\n[{mark}] {pack_dir.name} · 生成 confab {len(rows)} 条")
        for r in hooked if not args.quiet else []:
            print(f"    {r['id']}（{r['speaker']}）重合词 {r['hooked']}")
            print(f"      {r['narration'][:64]}")
        if args.quiet and hooked:
            print(f"    撞卡 id: {', '.join(r['id'] for r in hooked)}")

    print(f"\n合计撞卡 {hooked_total} 条")
    print("提醒：本筛查只抓**词面**撞卡；语义撞卡（零共同二字）需人读。")
    if args.gate and hooked_total:
        print("[✗] 卡中性筛查未通过")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

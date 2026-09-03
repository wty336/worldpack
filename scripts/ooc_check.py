"""W8 OOC 抽查（规则层）：扫描通关 transcript 中的世界观禁用元素。

用法：uv run python scripts/ooc_check.py [transcript.txt ...]

以「回合段」为单位统计违规率：段数 = 分隔线数量；违规段 = 含禁用词的段。
M1 验收标准：违规率 < 5%（小样本）。此脚本是机器可读的规则层，人工复核仍不可少。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from game_agent.storyline import _forbidden_tokens
from game_agent.worldpack import load_worldpack


def main() -> int:
    parser = argparse.ArgumentParser(description="OOC 禁用元素扫描")
    parser.add_argument("paths", nargs="+", help="transcript 文本文件")
    args = parser.parse_args()

    pack = load_worldpack("world-packs/ancient_jianghu")
    tokens = _forbidden_tokens(pack.world)
    print(f"禁用词表（{len(tokens)}）: {tokens}\n")

    overall_hits = 0
    overall_segments = 0
    for p in args.paths:
        path = Path(p)
        if not path.exists():
            print(f"[✗] 找不到 {path}")
            continue
        text = path.read_text(encoding="utf-8")
        segments = [s for s in text.split("-" * 60) if s.strip()]
        hits: list[tuple[str, str]] = []
        for tok in tokens:
            for m in re.finditer(re.escape(tok), text):
                ctx = text[max(0, m.start() - 25): m.end() + 25].replace("\n", " ")
                hits.append((tok, ctx))
        # 违规段 = 含任意禁用词的段
        bad_segments = {
            i for i, seg in enumerate(segments) if any(t in seg for t in tokens)
        }
        rate = len(bad_segments) / len(segments) * 100 if segments else 0
        print(f"[{path.name}] 段数 {len(segments)} · 命中 {len(hits)} 处 · 违规段 {len(bad_segments)} "
              f"· 违规率 {rate:.1f}% {'✓' if rate < 5 else '✗'}")
        for tok, ctx in hits[:10]:
            print(f"  - '{tok}' → …{ctx}…")
        if len(hits) > 10:
            print(f"  …（其余 {len(hits) - 10} 处省略）")
        overall_hits += len(hits)
        overall_segments += len(segments)
        print()

    if overall_segments:
        total_rate = overall_hits / overall_segments * 100
        print(f"总体：{overall_hits} 处命中 / {overall_segments} 段（命中密度 {total_rate:.1f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

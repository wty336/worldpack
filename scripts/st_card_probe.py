"""体检一份真实的 SillyTavern 人物卡/世界书：**它会丢什么、用到哪些映射字段**。

用法::

    python scripts/st_card_probe.py <card.json> [--logic NOT_ANY]

**为什么单独有这个工具**：`docs/plan-dsh-tavern-parity.md` 期 4 的纪律是
"E-1 必须由一份真实酒馆人物卡驱动，**不要**先按映射表把字段铺满再等用例"——
否则会重演"A 系列十个字段零真实用量"（校验通过但从未运行）。

本仓库现有 44 条真实 lore **只用到 `keys` 与 `text`**；次键 / 常驻 / 正则键
那几行映射因此只被构造数据验证过。作者拿一张真实卡跑这个脚本，就能立刻看到：

- 这张卡到底用了哪些映射字段（= 哪些映射行被真实数据验证了）；
- 有哪些东西**会被丢掉或忽略**（关掉的条目、`position`/`probability` 等不借的字段）；
- 转换出来会是几条 lore、id 长什么样。

它**不改任何东西**（纯读取 + 报告），所以可以放心对着别人的卡跑。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from game_agent import st_convert as st


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="体检 ST 人物卡/世界书的导入后果（只读）")
    ap.add_argument("card", help="卡片或世界书 JSON 的路径")
    ap.add_argument("--logic", default="AND_ANY",
                    choices=("AND_ANY", "AND_ALL", "NOT_ANY", "NOT_ALL"),
                    help="带次级关键词的条目用哪个逻辑（ST 的 selectiveLogic 数字枚举不做映射）")
    args = ap.parse_args(argv)

    warnings = st.StWarnings()
    try:
        card = st.read_card_file(args.card, warnings)
        rows = st.character_book_to_lore(card.get("character_book"),
                                         logic=args.logic, warnings=warnings)
    except st.StConvertError as e:
        print(f"[✗] {e}")
        return 1

    print(f"卡片：{card['name'] or '（无名）'}")
    prose = {k: len(str(card.get(k) or "")) for k in
             ("description", "personality", "scenario", "first_mes", "mes_example")}
    print("散文字段字数：" + " · ".join(f"{k}={v}" for k, v in prose.items() if v))
    print(f"标签：{'、'.join(card['tags']) or '（无）'}")
    print(f"备用开场白：{len(card['alternate_greetings'])} 条")

    print(f"\n世界书可导入：{len(rows)} 条")
    # **映射字段的真实用量**——这是这个脚本的主要产出
    usage = Counter()
    for row in rows:
        usage["keys"] += 1
        usage["text"] += 1
        if row.get("secondary_keys"):
            usage["secondary_keys"] += 1
        if row.get("constant"):
            usage["constant"] += 1
        if any(k.startswith("/") and k.endswith("/") for k in row.get("keys") or []):
            usage["regex_keys"] += 1
    if rows:
        print("  用到的映射字段：" + " · ".join(f"{k}×{v}" for k, v in sorted(usage.items())))
        never = {"secondary_keys", "constant", "regex_keys"} - set(usage)
        if never:
            print(f"  ⚠️ 这张卡没有用到：{'、'.join(sorted(never))}"
                  f"（那几行映射对你这份语料仍是未验证的）")
        print("  前 5 条的 id：" + "、".join(r["id"] for r in rows[:5]))

    if warnings:
        print("\n会丢掉 / 忽略的东西（读宽但不静默）：")
        print(warnings.report())
    else:
        print("\n没有任何降级：这条链是无损的（就映射表覆盖的字段而言）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

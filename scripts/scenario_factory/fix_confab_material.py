"""① confab 材料重渲染：补回非目标事实（2026-09-14，经拍板）。

**问题（实测，不是推测）**：confab 卡的事实**全部**标记 `in_material=false`
（`cards.py:340` + 其校验强制），于是材料的「关键事实」段**整体消失**。后果三条：

1. **材料形状 100% 泄露类别** —— 规则「材料无事实段 ⇒ 虚构事实」在
   train 命中 **457/457**、dev 命中 **491/491**，**零误报**：判官**不读叙事**就能判出这一族；
   而生产里材料永远有事实段（那是真实游戏状态）⇒ **捷径在生产里失效**，
   而 confab 正是学生最需要补的一课（配额 ≥40% 就是为它设的）；
2. 与生产分布不一致（模型只见过"空材料版"的 confab）；
3. confab 的材料空间塌成 **42 种**（setting/ooc 分别 631/675 种）—— 最该补的一族上下文最单调。

**修法**：把**非目标事实**重新物化进材料（**被断言的那条继续缺席** —— 这条硬要求由
`materialize._check_text` 的校验③ 守住）。**叙事与标签一字不动**：叙事本来就带着非目标事实的
anchor（anchor 校验一直要求它们在位，实测 0 缺失），只有材料渲染把它们漏了 ⇒ 两边**本来就一致**，
补回去不引入任何新信息给判官"第二条通路"。

**代价**：**零 LLM 调用**（材料是卡面的确定性重算）。写盘方式是**日志 append 更新条目**
（last-wins 设计正好为此而生：可审计、不改历史、中断安全）。

用法::

    python -m scripts.scenario_factory.fix_confab_material --layer train            # 预览（默认）
    python -m scripts.scenario_factory.fix_confab_material --layer train --apply     # 落盘
"""
from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter

from . import worklog
from .assemble import DATA_ROOT
from .cards import generate_card
from .materialize import build_material


def patched_material(sample: dict) -> tuple[str | None, str]:
    """→ `(新材料, 说明)`。`None` = 不改（非 confab / 无事实可补 / 已达标 / 算出泄漏）。"""
    if sample.get("category") != "confab":
        return None, "非 confab"
    if "关键事实" in sample.get("material", ""):
        return None, "已有事实段"
    _, seed, seq = sample["id"].split("-")
    card = generate_card(int(seed), int(seq), "judge")
    if not card.corruptions:
        return None, "卡面无 corruption"
    tgt = card.corruptions[0].target_fact
    rest = [k for k in range(len(card.facts)) if k != tgt]
    if not rest:
        return None, "无事实可补（单事实卡）"
    patched = card.model_copy(update={"material": card.material.model_copy(
        update={"facts": rest})})
    text = build_material(patched)
    leaked = [a for a in card.facts[tgt].anchors if a in text]
    if leaked:                       # 硬要求：被断言的事实的 anchor 绝不得入材料
        return None, f"被断言 anchor 泄漏 {leaked}（跳过）"
    if "关键事实" not in text:
        return None, "补后仍无事实段（跳过）"
    return text, "ok"


def fix_layer(out_dir: pathlib.Path, layer: str, *, apply: bool) -> dict:
    j = worklog.read_journal(worklog.journal_path(out_dir, layer, "judge"))
    stats, changed = Counter(), []
    for i, e in sorted(j.entries.items()):
        s = e.get("sample")
        if e["status"] != worklog.KEPT or not s:
            continue
        new, why = patched_material(s)
        stats[why] += 1
        if new is None:
            continue
        changed.append((i, e, {**s, "material": new}))
    if apply and changed:
        for i, e, s2 in changed:
            j.append({**e, "sample": s2})     # last-wins：保留 quality/sampled/status 等字段
    return {"stats": dict(stats), "changed": len(changed),
            "ids": [e["sample"]["id"] for _, e, _ in changed]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="confab 材料重渲染（补回非目标事实）")
    p.add_argument("--layer", required=True, choices=["train", "dev", "eval"])
    p.add_argument("--out", default=str(DATA_ROOT))
    p.add_argument("--apply", action="store_true", help="真正写盘（缺省只预览）")
    args = p.parse_args(argv)
    out_dir = pathlib.Path(args.out)
    rep = fix_layer(out_dir, args.layer, apply=args.apply)
    print(f"[{args.layer}/judge] {'已写盘' if args.apply else '预览（未写盘）'}："
          f"**改动 {rep['changed']} 条**")
    for why, n in sorted(rep["stats"].items(), key=lambda kv: -kv[1]):
        print(f"   {why}: {n}")
    if rep["ids"]:
        print(f"   前 3 条: {rep['ids'][:3]}")
    if not args.apply and rep["changed"]:
        print("   → 加 --apply 才落盘；落盘后需重新发布该层（零 LLM 调用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

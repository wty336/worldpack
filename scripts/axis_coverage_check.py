"""轴覆盖守卫：把"训练包与评测包按轴不相交"从纪律变成会 FAIL 的检查。

依据（2026-09-12 §3.5.3/§4.4）：窄化的真问题是"轴值太窄"，而轴标签若靠人工声明，
就会自欺。本脚本做两件事：

1. **机械事实抽取**：从包 YAML 里抽可验证的轴（era / 数值系统 stats / NPC 数 / 节点数 /
   lore 数 / 关系语气档），写进 `eval-sets/axis-coverage.yaml`；
2. **声明 vs 事实核对 + 集合不相交**：声明表里每个机械字段必须与包实际一致（防"标签自欺"），
   且被指定为 holdout 的轴，其取值**不得在 T（训练包）与 E（评测包）之间共享**。

用法：
    python scripts/axis_coverage_check.py --write-table   # 刷新机械事实部分
    python scripts/axis_coverage_check.py                 # 核对声明 vs 事实 + 集合不相交
    python scripts/axis_coverage_check.py --gate          # 不一致则退出码 1
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

from game_agent.worldpack import load_worldpack

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TABLE = REPO_ROOT / "eval-sets" / "axis-coverage.yaml"
# 需要"至少一个取值只出现在评测集"的轴（见 §4.4 规则）
HOLDOUT_AXES = ("era", "stats", "npc_band", "mainline_band")


def npc_band(n: int) -> str:
    if n <= 2:
        return "1-2"
    if n <= 5:
        return "3-5"
    return "6+"


def node_band(n: int) -> str:
    if n <= 6:
        return "4-6"
    if n <= 12:
        return "8-12"
    return "16+"


def extract_facts(pack_dir: pathlib.Path) -> dict:
    """机械抽取（**只写能从 YAML 里核出来的东西**；语体/题材等语义轴由人补）。

    ``mainline_nodes`` = `mainline.yaml` 的节点数（**主线节点**，不是场景节点总数）。
    """
    pack = load_worldpack(pack_dir)
    stats = sorted((pack.schedule.stats or {}).keys())
    nodes = 0
    mainline = getattr(pack, "mainline", None)
    if mainline is not None:
        dumped = mainline.model_dump()
        for key in ("nodes", "chain", "stages"):
            val = dumped.get(key)
            if isinstance(val, dict):
                nodes = len(val)
                break
            if isinstance(val, list):
                nodes = len(val)
                break
    tones = sorted({
        stage.tone
        for npc in pack.npcs.values()
        for stage in (npc.affection_stages or [])
    })
    return {
        "pack_id": pack_dir.name,
        "world_name": pack.world.name,
        "era": pack.world.era,
        "stats": stats,
        "n_stat_tracks": len(stats),
        "npc_count": len(pack.npcs),
        "npc_band": npc_band(len(pack.npcs)),
        "mainline_nodes": nodes,
        "mainline_band": node_band(nodes) if nodes else "unknown",
        "lore_count": len(pack.world.lore or []),
        "forbidden_count": len(pack.world.forbidden or []),
        "relation_tones": tones,
    }


def all_packs() -> list[pathlib.Path]:
    return sorted(
        d for d in (REPO_ROOT / "world-packs").iterdir()
        if d.is_dir() and (d / "judge_corpus.yaml").exists()
    )


def load_table() -> dict:
    if not TABLE.exists():
        return {"packs": {}, "holdout_axes": list(HOLDOUT_AXES)}
    data = yaml.safe_load(TABLE.read_text(encoding="utf-8")) or {}
    data.setdefault("packs", {})
    data.setdefault("holdout_axes", list(HOLDOUT_AXES))
    return data


def write_table(facts: dict[str, dict]) -> None:
    """保留已有的 split/notes 人工字段，只刷新机械事实。"""
    old = load_table()
    packs = {}
    for pid, f in facts.items():
        prev = old["packs"].get(pid, {})
        packs[pid] = {
            "split": prev.get("split", "unknown"),  # anchor | train | eval
            "notes": prev.get("notes", ""),
            **{k: v for k, v in f.items() if k != "pack_id"},
        }
    out = {
        "note": "轴覆盖声明表：机械字段由 scripts/axis_coverage_check.py --write-table 生成，勿手改；"
                "split/notes 人工维护（train=供训练数据 / eval=仅评测）",
        "holdout_axes": list(HOLDOUT_AXES),  # 轴清单以代码为准（改名时自动同步）
        "packs": packs,
    }
    TABLE.write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=1000),
                     encoding="utf-8")
    print(f"已写入 {TABLE.relative_to(REPO_ROOT)}（{len(packs)} 个包）")


def check(facts: dict[str, dict], table: dict) -> list[str]:
    problems: list[str] = []
    declared = table["packs"]
    for pid, f in facts.items():
        if pid not in declared:
            problems.append(f"{pid}: 包里存在但声明表未收录（新包必须显式声明轴归属）")
            continue
        row = declared[pid]
        for key in ("era", "stats", "npc_count", "mainline_nodes"):
            if key not in row:
                continue
            if row[key] != f[key]:
                problems.append(f"{pid}.{key}: 声明 {row[key]!r} ≠ 实际 {f[key]!r}")
        if row.get("split") not in ("anchor", "train", "eval"):
            problems.append(f"{pid}.split 非法/未定：{row.get('split')!r}（应 anchor/train/eval）")

    # 集合不相交：holdout 轴上，T 与 E 不得共享取值
    axes = table.get("holdout_axes", list(HOLDOUT_AXES))
    train = [p for p, r in declared.items() if r.get("split") == "train" and p in facts]
    evals = [p for p, r in declared.items() if r.get("split") == "eval" and p in facts]
    for axis in axes:
        if facts and axis not in next(iter(facts.values())):
            problems.append(f"holdout 轴名 {axis!r} 在抽取事实里不存在（轴改名未同步？）")
            continue
        tv = {str(facts[p].get(axis)) for p in train}
        ev = {str(facts[p].get(axis)) for p in evals}
        shared = tv & ev
        if shared:
            problems.append(f"轴 {axis} 上 T∩E ≠ ∅：共享取值 {sorted(shared)}"
                            f"（该轴必须有一个取值只出现在评测集）")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="轴覆盖守卫（声明 vs 事实 + T∩E 不相交）")
    parser.add_argument("--write-table", action="store_true", help="刷新机械事实部分")
    parser.add_argument("--gate", action="store_true", help="有问题则退出码 1")
    args = parser.parse_args(argv)

    facts = {p.name: extract_facts(p) for p in all_packs()}
    if args.write_table:
        write_table(facts)
        return 0

    table = load_table()
    problems = check(facts, table)
    declared = table["packs"]
    by_split: dict[str, list[str]] = {}
    for pid, row in declared.items():
        by_split.setdefault(str(row.get("split")), []).append(pid)
    print("集合分布：" + " · ".join(f"{k}={len(v)}（{', '.join(sorted(v))}）"
                                   for k, v in sorted(by_split.items())))
    print(f"轴事实（机械抽取）：{len(facts)} 个包")
    for pid, f in sorted(facts.items()):
        print(f"  {pid:<20} era={f['era'][:22]:<24} stats={f['stats']} "
              f"npc={f['npc_count']}({f['npc_band']}) 主线节点={f['mainline_nodes']}({f['mainline_band']})")
    if problems:
        print(f"\n[✗] {len(problems)} 处问题：")
        for p in problems:
            print(f"    - {p}")
        return 1 if args.gate else 0
    print("\n[✓] 声明与事实一致，holdout 轴无 T∩E 重叠")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

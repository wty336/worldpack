"""真实轨迹导出 normal 候选：跑一遍真实回合循环，逐回合导出整条 (状态, 叙事)。

**为什么**：评测集里的 normal 是判官误报率的分母，必须来自**真实轨迹**——手写/模板化的
"正常叙事"测不出真实分布（口语、残句、状态栏噪声、数值措辞）。配额 40/包 由
`scripts/eval_quota_check.py` 看守，本脚本负责把它填上。

两步：

    # ① 采集（付费：每回合 1 次 turn 调用 + 生产同款侧信道）
    python scripts/harvest_normals.py --pack world-packs/P1_school_letters --turns 30
    # ② 质检 + 落盘（付费：每候选 1 次判官调用；判官判"问题"的候选丢弃）
    python scripts/harvest_normals.py --validate reports/normals-P1_school_letters-*.jsonl

产出：`reports/normals-<pack>-<ts>.jsonl`（原始候选）→ `world-packs/<pack>/judge_corpus.real.yaml`
（质检通过的 normal，带 provenance：模型/端点/时间/来源 jsonl）。

纪律：
- 候选必须过判官（应判"通过"）——模型自己产出的违规不能当正常样本；
- 采集时状态字段（scene/present/affections/facts/npc_memories/stats）**全部**落盘，
  否则材料重建不出原样（`judge_corpus.build_materials` 依赖这些字段）；
- `--offline` 用桩 LLM 跑通管线（零成本，测试用）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
from datetime import datetime
from types import SimpleNamespace

import yaml

from game_agent.config import load_settings
from game_agent.context import ContextBuilder
from game_agent.endpoint import fingerprint_for
from game_agent.game import Game
from game_agent.judge import JudgeSystem
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FILLER = [
    "四处看看，留意周围有没有值得注意的东西。",
    "跟在场的人打个招呼，问问最近有什么事情。",
    "把手头的事整理一下，想想接下来该做什么。",
    "沿着街往前走一段，看看天色和路上的人。",
]


def _dump_case(state: GameState, pack, narration: str) -> dict:
    """把当轮状态 + 叙事落成一条候选（字段与 JudgeCase 对齐，材料可原样重建）。"""
    return {
        "turn": state.turn_count,
        "day": state.day,
        "scene": state.scene,
        "present": list(state.present_npcs),
        "affections": {k: round(float(v), 3) for k, v in state.affections.items()},
        "facts": [m.fact for m in state.player_facts],
        "npc_memories": {
            k: [m.fact for m in v] for k, v in state.npc_memories.items() if v
        },
        "stats": {k: round(float(v), 3) for k, v in state.stats.items()},
        "material": ContextBuilder.from_pack(pack).status_text(state, None),
        "narration": narration,
    }


class _OfflineFake:
    """--offline：内嵌假客户端（只验管线，不测能力）。形状照 longrun_probe 的桩。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        args = json.dumps(
            {"narration": "你环顾四周，风从檐角掠过，什么也没发生。",
             "choices": ["继续", "离开", "询问"], "plot_signal": "normal"}
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    reasoning_content=None,
                    tool_calls=[SimpleNamespace(
                        id="off1",
                        function=SimpleNamespace(name="submit_narration", arguments=args),
                    )],
                ),
            )],
            usage=None,
        )


def collect(pack_path: str, turns: int, seed: int, days: int, offline: bool) -> tuple[list[dict], object]:
    """跑真实循环采集候选；返回 (候选列表, settings)。"""
    pack = load_worldpack(pack_path)
    state = GameState.from_pack(pack)
    settings = load_settings()

    if offline:
        tracker = None
        llm = LLMClient(_OfflineFake(), "offline-stub", build_tools(pack.schedule), models={})
    else:
        tracker = UsageTracker("reports/usage-normals.jsonl")
        llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)

    game = Game(
        pack, state, llm, rng=random.Random(seed),
        extract_every=2, compress_threshold=10**9, judge_every=5, reflect_every=10,
    )
    cases: list[dict] = []
    view = game.start()
    dialogue_left = 0
    guard = 0
    while state.day <= days and view.ending is None and len(cases) < turns and guard < turns * 6:
        guard += 1
        if view.choice_prompt is not None:
            view = game.pick(0)
        elif state.action_points_left > 0:
            avail = [a.id for a in game.actions_available()]
            view = game.act(avail[0] if avail else next(iter(pack.schedule.actions)))
            dialogue_left = 1
        elif dialogue_left > 0:
            view = game.say(FILLER[len(cases) % len(FILLER)])
            dialogue_left -= 1
        else:
            game.end_day()
            continue
        if view.narration:
            cases.append(_dump_case(state, pack, view.narration))
    return cases, settings


def validate(jsonl_path: str, offline: bool = False) -> int:
    """判官逐条质检 → 通过的写入 judge_corpus.real.yaml。"""
    path = pathlib.Path(jsonl_path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pack_id = path.stem.split("-")[1] if "-" in path.stem else ""
    pack_dir = REPO_ROOT / "world-packs" / pack_id
    if not pack_dir.exists():
        print(f"[✗] 从文件名推断不出包（{path.name}）——用 --pack 指定")
        return 1
    settings = load_settings()
    tracker = UsageTracker("reports/usage-normals-validate.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    judge = JudgeSystem(llm)

    kept, dropped = [], 0
    for i, row in enumerate(rows, 1):
        ok, verdict = judge.check(row["narration"], row["material"])
        if ok is not True:
            dropped += 1
            print(f"  [丢] 候选 {i}：判官未判通过（{str(verdict)[:40]}）")
            continue
        kept.append(row)

    out = {
        "version": 1,
        "content_version": f"real-{datetime.now().strftime('%Y%m%d')}",
        "generated_by": "scripts/harvest_normals.py",
        "provenance": {
            "source_jsonl": path.name,
            "model": settings.model,
            "endpoint": fingerprint_for(settings, "judge"),
            "validated_at": datetime.now().isoformat(timespec="seconds"),
            "kept": len(kept),
            "dropped": dropped,
        },
        "note": "真实轨迹 normal（判官判通过者）；状态字段齐全，材料由 build_materials 原样重建",
        "cases": [
            {
                "id": f"real_normal_{i:02d}",
                "category": "normal",
                "narration": r["narration"],
                "expected": True,
                "day": r["day"],
                "scene": r["scene"],
                "present": r["present"],
                "affections": r["affections"],
                "facts": r["facts"],
                "npc_memories": r["npc_memories"],
                "stats": r["stats"],
                "note": f"真实轨迹第 {r['turn']} 回合（{path.name}）",
            }
            for i, r in enumerate(kept, 1)
        ],
    }
    if not kept:
        print("[✗] 没有一条通过质检，不落盘")
        return 1
    out_path = pack_dir / "judge_corpus.real.yaml"
    out_path.write_text(
        yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8"
    )
    print(f"\n[✓] 写入 {out_path.relative_to(REPO_ROOT)}：{len(kept)} 条（丢弃 {dropped}）")
    print(tracker.cost_report())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实轨迹导出 normal 候选 + 质检")
    parser.add_argument("--pack", default="world-packs/P1_school_letters")
    parser.add_argument("--turns", type=int, default=30, help="目标候选数（= 回合数）")
    parser.add_argument("--days", type=int, default=6, help="游戏内天数预算")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--offline", action="store_true", help="桩 LLM（零成本，验管线）")
    parser.add_argument("--out", default=None, help="候选 jsonl 路径（缺省自动命名）")
    parser.add_argument("--validate", default=None, help="质检指定 jsonl → judge_corpus.real.yaml")
    args = parser.parse_args(argv)

    if args.validate:
        return validate(args.validate, offline=args.offline)

    cases, settings = collect(args.pack, args.turns, args.seed, args.days, args.offline)
    pack_id = pathlib.Path(args.pack).name
    out = pathlib.Path(args.out or f"reports/normals-{pack_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in cases:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n[✓] 采集 {len(cases)} 条候选 → {out}")
    print(f"    model={settings.model} · {'离线桩' if args.offline else '真实调用'}")
    if not args.offline:
        print("    下一步：python scripts/harvest_normals.py --validate " + str(out))
    return 0 if cases else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

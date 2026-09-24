"""runtime 平台化 ②：记录一局实验（脚本化自动驾驶 + 每回合 checkpoint）。

用法::

    python scripts/record_run.py --pack world-packs/ancient_jianghu --days 6 [--fresh]

产物：``runs/<run_id>/``（runlog.jsonl + checkpoints/<turn>.json + meta.json）。
- checkpoint(0) = 开局前的初始状态（replay 第 1 回合的锚点）；
- 每回合一行：玩家动作 + 结果摘要 + checkpoint 引用（与 trace 用 run_id/turn 关联）。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.runlog import RUNS_DIR, RunRecorder, reset_runs_dir
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

PROFILES: dict[str, dict] = {
    "ancient_jianghu": {
        "picks": {"how_to_help": 0, "poetry_choice": 0},
        "action": "cultivate",
        "days": 6,
        "lines": ["（诚恳）在下初来长安，想打听些谋生的门道。",
                  "（望着远处）长安的黄昏，倒比别处更沉静些。"],
    },
    "xianxia_wendao": {
        "picks": {"choose_peak": 0, "relic_choice": 1, "battle_choice": 2},
        "action": "meditate",
        "days": 8,
        "lines": ["（郑重抱拳）师姐，弟子吐纳总觉气机滞涩，可否指点一二？"],
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="record_run", description="记录一局可回放实验")
    parser.add_argument("--pack", default="world-packs/ancient_jianghu")
    parser.add_argument("--days", type=int, default=None, help="日数预算（缺省用 profile）")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--fresh", action="store_true", help="先清空 runs/ 再记录")
    args = parser.parse_args(argv)

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1
    pack = load_worldpack(args.pack)
    profile = PROFILES.get(pack.root.name)
    if profile is None:
        print(f"[✗] 未登记 {pack.root.name} 的自动驾驶 profile")
        return 1
    day_cap = args.days or profile["days"]

    if args.fresh:
        reset_runs_dir(RUNS_DIR)
    rec = RunRecorder.create(RUNS_DIR)
    (rec.run_dir / "meta.json").write_text(
        json.dumps({
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "pack": pack.world.name,
            "model": settings.model,
            "seed": args.seed,
            "days": day_cap,
            "prompt_patch": None,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    tracker = UsageTracker(rec.run_dir / "usage.jsonl")
    llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)
    game = Game(pack, GameState.from_pack(pack), llm, rng=__import__("random").Random(args.seed))

    turn = 0

    def record(action: dict, outcome: dict | None) -> None:
        nonlocal turn
        turn += 1
        rec.checkpoint(turn, game.state, game.history, action, outcome)

    # checkpoint 0：开局前初始状态（replay 第 1 回合的锚点）
    rec.checkpoint(0, game.state, [], None, None)
    view = game.start()
    record({"kind": "start"}, None if view.narration is None else {"narration": view.narration[:120]})

    line_idx, said = 0, False
    while game.state.day <= day_cap and view.ending is None and turn < 200:
        if view.choice_prompt is not None:
            pick = profile["picks"][view.choice_prompt.id]
            before = len(game.state.stat_log)
            view = game.pick(pick)
            record(
                {"kind": "pick", "payload": pick},
                {"narration": (view.narration or "")[:120], "stat_changes": len(game.state.stat_log) - before},
            )
            continue
        if game.state.action_points_left > 0:
            before = len(game.state.stat_log)
            view = game.act(profile["action"])
            record(
                {"kind": "act", "payload": profile["action"]},
                {"narration": (view.narration or "")[:120], "stat_changes": len(game.state.stat_log) - before},
            )
            continue
        if game.state.day % 2 == 0 and not said:
            said = True
            line = profile["lines"][line_idx % len(profile["lines"])]
            line_idx += 1
            before = len(game.state.stat_log)
            view = game.say(line)
            record(
                {"kind": "say", "payload": line},
                {"narration": (view.narration or "")[:120], "stat_changes": len(game.state.stat_log) - before},
            )
            continue
        said = False
        game.end_day()
        record({"kind": "end_day"}, None)

    print(f"run_id={rec.run_id} · 回合数 {turn} · 结局 {view.ending.title if view.ending else '未达成'}")
    print(f"产物目录 {rec.run_dir}\n" + tracker.cost_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

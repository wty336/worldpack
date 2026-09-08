"""② condition 类事件真机冒烟（需要 DEEPSEEK_API_KEY）——计划文档 docs/plan-worldpack-qa.md §4。

用法：
  uv run python scripts/condition_event_smoke.py --pack world-packs/xianxia_wendao
  uv run python scripts/condition_event_smoke.py --pack world-packs/urban_neon

策略（自适应循环，而非固定剧本）：
  关键抉择按包 profile 选 → 每日行动：优先"赠礼"（好感收益），货币不足则"挣钱"行动
  → 剩余回合播暖话台词（引导 LLM 走 change_stat 加好感）→ 日终推进。
  目标好感跨过阈值的那一轮，condition 事件在同一回合级联触发（引擎 end_turn 后检查）。

验收断言：目标事件 id 进入 triggered_events、事件效果入账、数值零偏差审计通过。
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from game_agent.audit import audit_stats
from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.schedule import ScheduleError
from game_agent.save import save_game
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

SAVE_DIR = Path("saves")

# 每包 profile：事件/好感/行动/抉择/台词（③ 统一冒烟脚本时并入）
PROFILES = {
    "xianxia_wendao": {
        "event_id": "ev_sword_pool",      # 洗剑池夜话：苏晚晴好感≥30
        "event_title": "洗剑池夜话",
        "aff_key": "su_wanying",
        "threshold": 30,
        "gift": "gift_pill",              # 赠丹探访：灵石-30 → 好感 +2~6
        "earn": "hunt",                   # 下山猎妖：赚灵石
        "picks": {"choose_peak": 0, "relic_choice": 0, "battle_choice": 0},
        "day_cap": 10,
        "lines": [
            "（郑重）师姐，昨夜我又梦见那柄剑了。剑印发烫时，第一个想到的就是你的剑。",
            "（递上热茶）山下买的暖茶，师姐练剑后暖暖手。",
            "（认真）师姐的剑，是我见过最稳的剑。若有一天我能与你并肩，就好了。",
            "（轻声）洗剑池的水那么冷，我明日来陪你练剑。",
        ],
    },
    "urban_neon": {
        "event_id": "ev_clinic_night",    # 诊所夜话：林澈好感≥30
        "event_title": "诊所夜话",
        "aff_key": "lin_che",
        "threshold": 30,
        "gift": "gift_gear",              # 送器材探访：信用点-30 → 好感 +2~6
        "earn": "scavenge",               # 接单拾荒：赚信用点
        "picks": {"how_to_repay": 1, "zero_choice": 2, "tower_choice": 2},
        "day_cap": 10,
        "lines": [
            "（递上黑市淘的咖啡）诊所又值了一夜？林医生，你该歇歇了。",
            "（认真）这城市里，只有你跟我说过真话。",
            "（轻声）义体排异那晚，谢谢你没放弃我。",
            "（望着她）等这事了了，我请你去江边看一次日落。",
        ],
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description="condition 事件真机冒烟（计划文档②）")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--seed", type=int, default=23, help="RNG 种子")
    args = parser.parse_args()

    pack = load_worldpack(args.pack)
    profile = PROFILES[pack.root.name]
    event_id = profile["event_id"]

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    state = GameState.from_pack(pack)
    tracker = UsageTracker(SAVE_DIR / f"usage-condition-{pack.root.name}.jsonl")
    llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)
    game = Game(
        pack, state, llm, rng=random.Random(args.seed),
        extract_every=2, compress_threshold=30000, judge_every=5, reflect_every=10,
    )

    transcript: list[str] = []

    def log(*parts) -> None:
        text = "\n".join(str(p) for p in parts if p)
        transcript.append(text + "\n" + "-" * 60)
        print(text)
        print("-" * 60)

    def aff() -> float:
        return state.affections[profile["aff_key"]]

    try:
        print(f"model={settings.model} · seed={args.seed} · 《{pack.world.name}》"
              f" · 目标事件 {profile['event_title']}（好感≥{profile['threshold']}）\n")

        view = game.start()
        line_idx = 0
        said = False
        while state.day <= profile["day_cap"] and event_id not in state.triggered_events:
            if view.choice_prompt is not None:
                pick = profile["picks"][view.choice_prompt.id]
                log(f"【关键抉择·{view.choice_prompt.id}】", view.choice_prompt.prompt)
                view = game.pick(pick)
                log("【抉择之后】", view.narration)
                continue

            if view.ending is not None:  # 意外提前结局：记录并退出循环
                log("【结局】", view.ending.title)
                break

            if state.action_points_left > 0:
                # 优先赠礼（好感收益），赠礼条件不满足（货币不足）则挣钱行动
                available = {a.id for a in game.actions_available()}
                action = profile["gift"] if profile["gift"] in available else profile["earn"]
                try:
                    view = game.act(action)
                except ScheduleError as e:
                    log(f"[行动被拒] {e}")
                    view = game.act(profile["earn"])
                log(
                    f"【第 {state.day} 天·行动 {action}】",
                    view.narration,
                    f"[状态] {profile['aff_key']}={aff():g} · 触发事件: "
                    f"{'✓' if event_id in state.triggered_events else '未'}",
                )
                continue

            if not said:
                said = True
                line = profile["lines"][line_idx % len(profile["lines"])]
                line_idx += 1
                view = game.say(line)
                log(
                    f"【第 {state.day} 天·对话】{line}",
                    view.narration,
                    f"[状态] {profile['aff_key']}={aff():g} · 触发事件: "
                    f"{'✓' if event_id in state.triggered_events else '未'}",
                )
                continue

            said = False
            game.end_day()
            log(f"—— 第 {state.day} 天 ——", f"[状态] {profile['aff_key']}={aff():g}")

        # 验收断言
        if event_id in state.triggered_events:
            log(
                f"[✓] condition 事件「{profile['event_title']}」真机触发"
                f"（好感 {aff():g}，第 {state.day} 天）"
            )
        else:
            log(
                f"[✗] 未触发「{profile['event_title']}」（预算 {profile['day_cap']} 天内"
                f"好感仅 {aff():g}）",
                f"好感 {state.affections} · flags {state.flags}",
            )

        deviations = audit_stats(pack, state)
        if deviations:
            log("[✗] 数值零偏差审计失败：", *deviations)
            status = 1
        else:
            log(f"[✓] 数值零偏差审计通过（{len(state.stat_log)} 条变更记录）")
            status = 0
        if event_id not in state.triggered_events:
            status = 1

        SAVE_DIR.mkdir(exist_ok=True)
        save_game(state, SAVE_DIR / f"condition-{pack.root.name}.json", game.history)
        (SAVE_DIR / f"condition-{pack.root.name}.txt").write_text(
            "\n\n".join(transcript), encoding="utf-8"
        )
        print(f"\n已保存 → saves/condition-{pack.root.name}.{{json,txt}}")
        print("\n" + tracker.cost_report())
        return status
    except LLMTurnError as e:
        print(f"[✗] 协议熔断: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[✗] 异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""A3 反思层真机冒烟（P1）：驱动一段带 NPC 记忆增长的短局，验证关系洞察合成与矛盾率。

用法：uv run python scripts/reflect_smoke.py [--turns 14]

流程：完成 N1 → 每日拜访沈清秋 + 闲聊 → 每 5 回合反思一次 →
结束时对每条洞察做 Judge 复核（材料不含洞察自身），断言矛盾率 = 0。
"""

from __future__ import annotations

import argparse
import random

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.judge import JudgeSystem
from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

LINES = [
    "（闲聊）今日天气不错，晨起在院中练了趟剑。",
    "（温和地）沈姑娘近来可好？前日之事，多谢姑娘指点。",
    "（认真地说）在下想在这长安谋个出路，姑娘见多识广，可否指点一二？",
    "（诚恳地）姑娘那日的话，在下回去想了许久，越想越觉有理。",
    "（微笑）今日路过东市，见有新鲜的果子，便带了些来。",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=14)
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    pack = load_worldpack("world-packs/ancient_jianghu")
    state = GameState.from_pack(pack)
    tracker = UsageTracker("reports/usage-reflect-smoke.jsonl")
    llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)
    game = Game(
        pack, state, llm, rng=random.Random(42),
        extract_every=2, judge_every=0, reflect_every=5,  # A3：每 5 回合反思
    )
    print(f"反思冒烟 · 目标 ~{args.turns} 回合\n")

    try:
        view = game.start()
        if view.choice_prompt is not None:
            view = game.pick(0)
        # 预置基线记忆：模拟更长的相处史（让反思有足够的合成素材）
        for fact, imp in [
            ("玩家曾在东市替沈清秋解围", 6),
            ("玩家多次登门拜访，与沈清秋品茶论诗", 5),
            ("沈清秋曾赠玩家诗集一册", 6),
            ("玩家为沈清秋备礼，礼物颇合心意", 5),
            ("沈清秋与玩家在曲江池畔同游诗会", 7),
            ("玩家承诺沈清秋遇急事可用『七月』暗号相寻", 8),
        ]:
            game.memory.add(state, "shen_qingqiu", fact, importance=imp)
        i = 0
        while state.turn_count < args.turns and view.ending is None:
            if view.choice_prompt is not None:
                view = game.pick(2 if view.choice_prompt.id == "poetry_choice" else 0)  # 让贤避早结局
                continue
            if state.action_points_left > 0:
                view = game.act("visit_shen")  # 拜访：触发 NPC 记忆
            else:
                view = game.say(LINES[i % len(LINES)])  # 对话：更多 remember 机会
                i += 1
                game.end_day()
    except LLMTurnError as e:
        print(f"[✗] 协议熔断: {e}")
        return 1

    print(f"\n===== 记忆状态 =====")
    for npc_id, bucket in state.npc_memories.items():
        print(f"  {npc_id} 记忆 {len(bucket)} 条：")
        for m in bucket[-6:]:
            print(f"    - [{m.day}天][重要 {m.importance:g}] {m.fact}")
    print(f"  玩家事实 {len(state.player_facts)} 条")

    print(f"\n===== 关系洞察（{sum(len(v) for v in state.npc_insights.values())} 条） =====")
    insight_total = 0
    insight_bad = 0
    for npc_id, insights in state.npc_insights.items():
        mat_state = state.copy()
        mat_state.npc_insights = {}  # 材料不含洞察自身，防循环对照
        materials = game.builder.status_text(mat_state, None)
        for ins in insights:
            insight_total += 1
            ok, verdict = JudgeSystem(game.llm).check(ins.text, materials)
            insight_bad += 0 if ok else 1
            print(f"  - {ins.text}")
            print(f"    来源: {'；'.join(ins.sources[:2])}")
            print(f"    Judge 复核: {'✓ 无矛盾' if ok else f'✗ {verdict}'}")
    if insight_total == 0:
        print("  （未产生洞察——NPC 记忆不足 8 条或回合未达反思点）")
    print(f"\n洞察矛盾率：{insight_bad}/{insight_total}（要求 0）")
    print("\n" + tracker.cost_report())
    return 0 if insight_total > 0 and insight_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

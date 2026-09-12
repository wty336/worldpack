"""B1 Lorebook 真机冒烟（P3）：验证按需注入的 lore 参与叙事且 Judge 复核无矛盾。

用法：uv run python scripts/lore_smoke.py

流程：开局（N1 解围）→ 在东市闲聊（触发 dongshi lore）→
断言回合消息含命中 lore、不含未命中 lore → Judge 复核叙事与材料一致。
"""

from __future__ import annotations

import random

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.judge import JudgeSystem
from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack


def main() -> int:
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    pack = load_worldpack("world-packs/ancient_jianghu")
    state = GameState.from_pack(pack)
    tracker = UsageTracker("reports/usage-lore-smoke.jsonl")
    llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)
    game = Game(pack, state, llm, rng=random.Random(42), judge_every=0)

    ok = True
    try:
        view = game.start()
        if view.choice_prompt is not None:
            view = game.pick(0)
        view = game.say("（好奇地）听说东市热闹，想去看看胡商都有些什么新鲜货。")
        print("---- 叙事 ----")
        print(view.narration)
    except LLMTurnError as e:
        print(f"[✗] 协议熔断: {e}")
        return 1

    # 最后一轮回合消息应含命中的东市 lore（场景沈府 + 近对话「东市」双命中），
    # 且不含未命中的西市 lore
    lore_msgs = [
        m["content"] for m in game.history if m.get("role") == "user" and "<lore>" in m["content"]
    ]
    prompt = lore_msgs[-1] if lore_msgs else ""
    if "东市是长安最热闹" not in prompt:
        print("[✗] 命中的 lore（东市）未注入回合消息")
        ok = False
    if "西市多胡商店铺" in prompt:
        print("[✗] 未命中的 lore（西市）被错误注入")
        ok = False
    print(f"注入检查：{'✓' if ok else '✗'}（东市 lore 命中注入 / 西市 lore 未注入）")

    # Judge 复核：叙事与材料（含注入 lore）一致
    materials = game.builder.status_text(game.state, None)
    passed, verdict = JudgeSystem(game.llm).check(view.narration, materials)
    if passed is None:
        print("Judge 复核: ? 不可判定（未知 ≠ 通过）")
    else:
        print(f"Judge 复核: {'✓ 通过' if passed else f'✗ {verdict}'}")
    ok = ok and passed is True  # 未知不算通过：冒烟门禁不得沉默放行

    print("\n" + tracker.cost_report())
    print("\n[✓] B1 冒烟通过" if ok else "\n[✗] B1 冒烟失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

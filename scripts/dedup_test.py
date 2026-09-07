"""A4 语义去重测试集（P1）：真机验证近义事实的拦截率 ≥80%。

用法：uv run python scripts/dedup_test.py

10 组近义事实对：每组先写入 A，再写入近义改写 B——B 应被去重拦截
（包含关系未命中 → bigram 预筛 → 轻量模型语义判定）。
"""

from __future__ import annotations

from game_agent.config import load_settings
from game_agent.llm import LLMClient
from game_agent.memory import MemorySystem
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

PAIRS = [
    ("玩家的剑名是听雨", "玩家佩剑唤作听雨"),
    ("玩家来自江南水乡", "玩家的家乡在江南一带"),
    ("玩家答应帮老樵夫送柴", "玩家承诺替老樵夫送柴火"),
    ("沈清秋赠玩家诗集一册", "沈清秋送给玩家一本诗集"),
    ("玩家的师父姓陆，曾走镖", "玩家师从陆姓镖师"),
    ("玩家爱喝龙井茶", "玩家喜欢喝龙井"),
    ("玩家救过沈清秋的命", "玩家曾救沈清秋一命"),
    ("玩家与沈清秋约了暗号", "玩家和沈清秋定下暗号"),
    ("玩家在长安打工谋生", "玩家靠打工在长安过活"),
    ("玩家的剑法平平", "玩家的武功剑法很一般"),
]

THRESHOLD = 0.8


def main() -> int:
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    pack = load_worldpack("world-packs/ancient_jianghu")
    tracker = UsageTracker("reports/usage-dedup.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    memory = MemorySystem(pack, llm)
    print(f"语义去重测试集 {len(PAIRS)} 组 · 模型 {llm.model_for('dedup')}\n")

    intercepted = 0
    for i, (fact_a, fact_b) in enumerate(PAIRS, 1):
        state = GameState.from_pack(pack)
        memory.add(state, "player", fact_a)
        result = memory.add(state, "player", fact_b)
        hit = "跳过" in result
        intercepted += hit
        print(f"  [{'✓' if hit else '✗'}] 组{i}: A「{fact_a}」 B「{fact_b}」 → {'拦截' if hit else '放行'}"
              f"（{result[:40]}）")

    rate = intercepted / len(PAIRS)
    print(f"\n拦截率 {intercepted}/{len(PAIRS)} = {rate:.0%}（要求 ≥{THRESHOLD:.0%}）")
    print("\n" + tracker.cost_report())
    return 0 if rate >= THRESHOLD else 1


if __name__ == "__main__":
    raise SystemExit(main())

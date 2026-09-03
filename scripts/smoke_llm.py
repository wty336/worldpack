"""W4 联调冒烟：真实 API 跑一轮完整协议闭环（需 .env 配置 DEEPSEEK_API_KEY）。

用法：uv run python scripts/smoke_llm.py
"""

from __future__ import annotations

from game_agent.config import load_settings
from game_agent.context import ContextBuilder
from game_agent.llm import LLMClient, LLMTurnError, build_tools, make_client
from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.worldpack import load_worldpack


def main() -> int:
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未找到 DEEPSEEK_API_KEY，请先配置 .env")
        return 1

    pack = load_worldpack("world-packs/ancient_jianghu")
    state = GameState.from_pack(pack)
    node = pack.mainline.nodes[0]
    state.scene = node.on_enter.scene
    state.present_npcs = ["shen_qingqiu"]
    stats = StatsSystem(pack.schedule)
    builder = ContextBuilder.from_pack(pack)
    client = LLMClient(make_client(settings), settings.model, build_tools(pack.schedule))

    history = [
        {
            "role": "user",
            "content": "（开场）你路过沈府门前，看见几个纨绔子弟正纠缠一位姑娘。",
        }
    ]
    messages = builder.build_messages(state, history, node)
    print(f"model={settings.model}  base_url={settings.base_url}")
    print(f"system prompt ≈ {len(builder.system_message['content'])} 字符")

    try:
        result = client.run_turn(
            messages,
            apply_change=lambda a: stats.apply_change(
                state, a["target"], a["stat"], a["delta"], a["reason"]
            ).message,
        )
    except LLMTurnError as e:
        print(f"[✗] 协议熔断: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[✗] API 异常: {type(e).__name__}: {e}")
        return 1

    print("---- 叙事 ----")
    print(result.narration)
    print("---- 选项 ----")
    for i, c in enumerate(result.choices, 1):
        print(f"  {i}. {c}")
    print(f"plot_signal={result.plot_signal}  iterations={result.iterations}")
    print("---- 数值变更 ----")
    for sc in result.stat_changes:
        print(f"  {sc['target']}.{sc['stat']} {sc['delta']:+g} → {sc['result']}")
    print("---- 当前状态 ----")
    print(f"  好感: {state.affections}")
    print(f"  属性: {state.stats}")
    print("[✓] 协议闭环跑通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

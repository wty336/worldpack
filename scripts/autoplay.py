"""W7 真机自走演示：真实 API 走一小段完整流程（关键抉择 → 解围 → 拜访 → 对话 → 存档）。

用法：uv run python scripts/autoplay.py
"""

from __future__ import annotations

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools, make_client
from game_agent.save import save_game
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack


def main() -> int:
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未找到 DEEPSEEK_API_KEY，请先配置 .env")
        return 1

    pack = load_worldpack("world-packs/ancient_jianghu")
    state = GameState.from_pack(pack)
    llm = LLMClient(make_client(settings), settings.model, build_tools(pack.schedule))
    game = Game(pack, state, llm)
    print(f"model={settings.model} · 《{pack.world.name}》\n")

    def turn(label: str, view) -> None:
        print(f"\n===== {label} =====")
        if view.narration:
            print(view.narration)
        print(f"[选项] " + " | ".join(view.choices))
        print(f"[状态] 好感 {state.affections} · 属性 {state.stats} · "
              f"节点 {state.current_node} · 第 {state.day} 天")
        if view.ending is not None:
            print(f"[结局] {view.ending.title}")

    try:
        # 1) 开场：N1 关键抉择
        view = game.start()
        print("【开场·关键抉择】" + view.choice_prompt.prompt)
        for i, c in enumerate(view.choices, 1):
            print(f"  {i}. {c}")
        # 2) 选择「挺身而出」
        view = game.pick(0)
        turn("解围（选择：挺身而出）", view)
        # 3) 拜访沈清秋
        view = game.act("visit_shen")
        turn("日程行动：拜访沈清秋", view)
        # 4) 自由对话一轮
        view = game.say("那日之后，你一直想再找机会与沈姑娘说说话。")
        turn("自由对话", view)
        # 5) 存档
        save_game(state, "saves/autoplay.json")
        print("\n已存档 → saves/autoplay.json")
        return 0
    except LLMTurnError as e:
        print(f"[✗] 协议熔断: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[✗] 异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""M2a 收尾证据：真实通关局（有 mainline 轨道）中的事实保持检查。

背景：沙盒探针（无轨道）中 f1 剑名/f4 师承 40 回合即丢，被剧情重写主导。
本脚本在带轨道的真实环境（ancient_jianghu）植入同批事实，通关后检查是否还记得——
验证"轨道环境 + 记忆机制"的组合效果（M2a 验收口径修订方案 A 的收尾证据）。

用法：uv run python scripts/railed_fact_check.py [--seed 42]
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools, make_client
from game_agent.save import load_game, load_history, save_game
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

SAVE_PATH = Path("saves/railed-fact-check.json")
CHECK_MAX_TOKENS = 500  # 推理模型思考链占预算，200 会答空（M1.5 探针同款教训）

FACTS = [
    ("f_sword", 1,
     "（闲聊）我这柄剑，名唤『听雨』，是师父传下来的，跟了我三年。",
     "玩家曾说过他的剑叫什么名字？只回答剑名，不知道就说『不知道』。", ["听雨"]),
    ("f_master", 2,
     "（回忆）我师父姓陆，早年在镖局里走镖，脾气倔得很。",
     "玩家的师父姓什么？只回答姓氏，不知道就说『不知道』。", ["陆"]),
    ("f_hometown", 3,
     "（谈及身世）我自江南而来，家乡多水，出门便坐船，倒也有趣。",
     "玩家是从哪里来的？只回答地名，不知道就说『不知道』。", ["江南", "姑苏"]),
]

BENIGN = "（闲谈）今日天气不错，晨起在院中练了趟剑。"


def main() -> int:
    parser = argparse.ArgumentParser(description="带轨道环境的事实保持检查")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--check-only", action="store_true",
                        help="跳过对局，从 saves/railed-fact-check.json 读档只跑事实检查")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    pack = load_worldpack("world-packs/ancient_jianghu")
    llm = LLMClient(make_client(settings), settings.model, build_tools(pack.schedule))

    if args.check_only:
        if not SAVE_PATH.exists():
            print(f"[✗] 存档不存在: {SAVE_PATH}")
            return 1
        state = load_game(SAVE_PATH)
        game = Game(pack, state, llm)
        game.history = load_history(SAVE_PATH)
        view = None
        print(f"从存档复查（第 {state.day} 天）\n")
    else:
        state = GameState.from_pack(pack)
        game = Game(pack, state, llm, rng=random.Random(args.seed), extract_every=2)
        print(f"model={settings.model} · seed={args.seed} · 《{pack.world.name}》（带主线轨道）\n")
        try:
            view = game.start()
            if view.choice_prompt is not None:
                view = game.pick(0)  # 挺身而出
                print("【N1 解围】完成\n")

            while state.day <= 10 and view.ending is None:
                if view.choice_prompt is not None:  # N2 诗会
                    view = game.pick(0)  # 咏月
                    continue
                if state.action_points_left > 0:
                    view = game.act("visit_shen")
                    continue
                line = next((f[2] for f in FACTS if f[1] == state.day), BENIGN)
                view = game.say(line)
                if view.choice_prompt is None and view.ending is None:
                    game.end_day()

            print(f"—— 对局结束：第 {state.day} 天"
                  f"{' · 结局 ' + view.ending.title if view.ending else ''} ——\n")
            save_game(state, SAVE_PATH, game.history)
        except (LLMTurnError, Exception) as e:  # noqa: BLE001
            print(f"[✗] 对局异常: {type(e).__name__}: {e}")
            return 1

    # 事实检查（经状态栏，真实机制）
    print("===== 事实保持检查 =====")
    passed = 0
    for fid, _day, _line, question, kws in FACTS:
        msgs = game.builder.build_messages(game.state, game.history, None)
        answer = game.llm.complete(
            [*msgs, {"role": "user", "content": f"[记忆检查] {question}"}],
            max_tokens=CHECK_MAX_TOKENS,
        )
        correct = any(k in answer for k in kws)
        passed += 1 if correct else 0
        print(f"  {'✓' if correct else '✗'} {fid}: {answer.strip()[:100]}")

    print(f"\n结果：{passed}/{len(FACTS)} 保持")
    print("玩家事实桶：")
    for m in game.state.player_facts:
        print(f"  - [{m.day}天] {m.fact}")
    return 0 if passed == len(FACTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

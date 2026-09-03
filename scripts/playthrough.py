"""W8 通关验收：真机脚本驱动完整通关（需要 DEEPSEEK_API_KEY）。

用法：
  uv run python scripts/playthrough.py --strategy together   # 追求「长相守」
  uv run python scripts/playthrough.py --strategy wanderer   # 走向「江湖独行」

流程：开场 → N1 解围 → 每日（行动 + 对话）→ N2 诗会抉择 → 直至结局或第 12 天。
全程记录 transcript 与最终存档，结束后自动跑数值零偏差审计。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from game_agent.audit import audit_stats
from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools, make_client
from game_agent.save import save_game
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

SAVE_DIR = Path("saves")
DAY_BUDGET = 12

# 分阶段台词（避免"未发生的诗会""复读机式客套"——上一轮负结果的教训）
EARLY_WORDS = [
    "（诚恳地）在下初来长安，人地两疏。姑娘若不嫌冒昧，可否为在下说说——这长安城里，习武之人该如何谋个正经出路？",
    "（认真地说）姑娘那日说『路见不平是侠者本分』，在下深以为然。今日登门，是想当面谢过姑娘的指点。",
    "（关切地）见姑娘眉间似有倦色，可是府中事务劳神？若有在下帮得上忙的，但说无妨。",
    "（温和地）今日路过东市，见有新鲜的果子，便带了些来。也不知合不合姑娘口味。",
]
AFTER_POEM_WORDS = [
    "（认真地说）那夜诗会，姑娘说『想说什么，便说什么』。在下如今才明白，有些话只对一个人说得出口。",
    "（望着她）『错将明月认还家』——在下的家，倒像是这几日与姑娘说话时的光景。",
    "（轻声）诗会那夜，满场灯火，都不及姑娘在灯下看我的那一眼。",
]
HEART_WORDS = [
    "（郑重地）在下身无长物，却有一身胆气。姑娘若信得过，把难处说与在下——我们一起想法子。",
    "（坚定地）这些日子与姑娘相处，在下心中所想，姑娘应当明白。若有难处，不必一人扛着。",
    "（认真地）只要姑娘不弃，天涯海角，在下都愿陪姑娘走一遭。",
]


def _pick_word(state: GameState, mem: dict) -> str:
    """按剧情阶段选台词，同池内轮换不重复。"""
    if state.flags.get("poetry_top3"):
        pool, key = AFTER_POEM_WORDS, "poem"
    elif state.day >= 6:
        pool, key = HEART_WORDS, "heart"
    else:
        pool, key = EARLY_WORDS, "early"
    if mem.get("key") == key:
        mem["idx"] = (mem["idx"] + 1) % len(pool)
    else:
        mem["key"], mem["idx"] = key, 0
    return pool[mem["idx"]]


def _log(lines: list[str], *parts) -> None:
    text = "\n".join(str(p) for p in parts if p)
    lines.append(text)
    print(text)
    print("-" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="真机通关验收")
    parser.add_argument("--strategy", choices=["together", "wanderer"], default="together")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    pack = load_worldpack("world-packs/ancient_jianghu")
    state = GameState.from_pack(pack)
    llm = LLMClient(make_client(settings), settings.model, build_tools(pack.schedule))
    game = Game(pack, state, llm)
    transcript: list[str] = []
    print(f"model={settings.model} · 策略={args.strategy} · 《{pack.world.name}》\n")

    try:
        # 开场：N1 关键抉择
        view = game.start()
        if view.choice_prompt is not None:
            _log(transcript, "【关键抉择·初遇】", view.choice_prompt.prompt)
            view = game.pick(0)  # 挺身而出
            _log(transcript, "【解围】", view.narration)

        mem: dict = {}
        dialogue_left = 0
        while state.day <= DAY_BUDGET and view.ending is None:
            # 关键选择（N2 诗会等）
            if view.choice_prompt is not None:
                idx = 0 if args.strategy == "together" else 2  # 咏月 / 让贤
                _log(
                    transcript,
                    f"【关键抉择·{view.choice_prompt.id}】{view.choice_prompt.prompt}",
                    f"→ 选择：{view.choices[idx]}",
                )
                view = game.pick(idx)
                _log(transcript, "【抉择之后】", view.narration)
                continue

            # 日程行动
            if state.action_points_left > 0:
                action = "visit_shen" if args.strategy == "together" else "cultivate"
                view = game.act(action)
                _log(transcript, f"【第 {state.day} 天·行动】", view.narration)
                if args.strategy == "together":
                    # 第 5 天起每天 2 轮对话（诗会后感情升温期）
                    dialogue_left = 1 if state.day < 5 else 2
                continue

            # 对话回合（together 专属；wanderer 不对话）
            if dialogue_left > 0:
                kind = _pick_word(state, mem)
                view = game.say(kind)
                _log(transcript, f"【第 {state.day} 天·对话】{kind}", view.narration)
                dialogue_left -= 1
                continue

            # 结束今天
            game.end_day()
            _log(transcript, f"—— 第 {state.day} 天 ——",
                 f"[状态] 好感 {state.affections} · 属性 {state.stats}")

        # 结算
        if view.ending is not None:
            _log(transcript, f"【结局】{view.ending.title}", view.ending.text)
        else:
            _log(transcript, f"[未达成结局] 第 {state.day} 天超出预算",
                 f"好感 {state.affections} · flags {state.flags}")

        # 数值零偏差审计
        deviations = audit_stats(pack, state)
        if deviations:
            _log(transcript, "[✗] 数值零偏差审计失败：", *deviations)
            status = 1
        else:
            _log(transcript, f"[✓] 数值零偏差审计通过（{len(state.stat_log)} 条记录）")
            status = 0

        # 落盘
        SAVE_DIR.mkdir(exist_ok=True)
        save_game(state, SAVE_DIR / f"playthrough-{args.strategy}.json")
        (SAVE_DIR / f"playthrough-{args.strategy}.txt").write_text(
            "\n\n".join(transcript), encoding="utf-8"
        )
        print(f"\n已保存 → {SAVE_DIR}/playthrough-{args.strategy}.{{json,txt}}")
        return status
    except LLMTurnError as e:
        print(f"[✗] 协议熔断: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[✗] 异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

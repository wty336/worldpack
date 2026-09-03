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

# together 策略的贴心话（轮换使用，保持多样）
KIND_WORDS = [
    "（温和地）沈姑娘，今日得见，心下甚安。这几日可还顺遂？",
    "（认真地说）那日诗会之后，在下常想起姑娘说的话，获益良多。",
    "（轻声）长安虽大，能说上话的人却不多。能与姑娘相识，是在下的运气。",
    "（关切地）见你眉间似有倦色，可是府中事务劳神？若有在下帮得上忙的，但说无妨。",
]


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

        just_acted = False
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
                just_acted = True
                continue

            # 对话回合（together 每日 1 轮；wanderer 不对话）
            if just_acted and args.strategy == "together":
                kind = KIND_WORDS[state.day % len(KIND_WORDS)]
                view = game.say(kind)
                _log(transcript, f"【第 {state.day} 天·对话】{kind}", view.narration)
                just_acted = False
                continue

            # 结束今天
            game.end_day()
            just_acted = False
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

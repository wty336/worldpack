"""协议熔断诊断：拦截 chat.completions.create 逐次打印模型原始输出。

local-14b 实验 Phase 0 阶段 1 引入（见 docs/local14b-p012-retro.md §3.4）：
14B 零样本协议熔断的失败模式靠此脚本定位（纯文本不调工具 / 输出逼近上限 / 重试后纠正）。

用法（默认 .env 指向的端点；指本地则加 env 覆盖）：
  uv run python scripts/diag_turn.py [--pack world-packs/ancient_jianghu] [--turns act|say|pick0]
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack


def main() -> int:
    parser = argparse.ArgumentParser(description="协议熔断诊断：逐次打印模型原始输出")
    parser.add_argument("--pack", default="world-packs/ancient_jianghu")
    parser.add_argument("--action", choices=["act", "say", "pick0"], default="act")
    args = parser.parse_args()

    settings = load_settings()
    pack = load_worldpack(args.pack)
    state = GameState.from_pack(pack)
    tracker = UsageTracker(Path("/tmp/usage-diag.jsonl"))
    llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)

    # 拦截 create：打印每次调用的原始返回
    call_no = {"n": 0}
    orig_create = llm._client.chat.completions.create

    def logged_create(**kwargs):
        call_no["n"] += 1
        n = call_no["n"]
        print(f"\n===== create #{n} ===== max_tokens={kwargs.get('max_tokens')} "
              f"msgs={len(kwargs.get('messages', []))}")
        resp = orig_create(**kwargs)
        choice = resp.choices[0]
        print(f"finish_reason={choice.finish_reason}")
        msg = choice.message
        print(f"content={str(msg.content)[:200]!r}")
        if msg.tool_calls:
            for t in msg.tool_calls:
                print(f"  tool_call: {t.function.name} args={t.function.arguments[:300]!r}")
        return resp

    llm._client.chat.completions.create = logged_create

    game = Game(pack, state, llm, rng=random.Random(11),
                extract_every=2, compress_threshold=30000, judge_every=5, reflect_every=10)

    view = game.start()
    print(f"\n[start] choice={view.choice_prompt.id if view.choice_prompt else None}")
    if args.action == "pick0":
        view = game.pick(0)
    elif args.action == "say":
        view = game.say("（诚恳）在下初来长安，人地两疏，想向姑娘打听些习武之人谋生的门道。")
    else:
        view = game.act("cultivate")
    print(f"\n[{args.action}] 结果: {view.narration[:80] if view.narration else None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

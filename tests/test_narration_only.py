"""N10-1 守卫：**"没调用收尾工具"不再等于熔断**。

上游：`docs/plan-tavern-shaped-product.md` §8.1（本项目最值钱的一条 harness 改进）、
`docs/roadmap.md` **N10**。

**被钉住的缺口**：协议强制模型每轮调用 `submit_narration` 收尾。模型若只写了正文、
没按协议包一层，`run_turn` 会当协议失败重试 3 次，然后 `LLMTurnError` →
`_txn_rollback` + `_meltdown_fallback`：**整轮作废**，玩家读到的是"本轮生成失败，
已跳过"而不是故事。可模型明明写出了正文。

**为什么不破坏「叙事被接受 ⟺ 效果生效」**：拿一个内容对照物看 ——
meltdown 那条改动的注释写着"玩家看到'本轮跳过'，真值就必须纹丝不动"。
零效果回合两边都是空集（effects = ∅、narration 是纯文本），不变量依然成立；
真正要拦的是"有工具调用但参数被截断"，那条另有守卫（截断即整批作废）。

守六件事：

1. **真的接受**：正文非空 + 没调工具 → 当轮返回，不再重试；
2. **效果真的是零**：状态（数值/行动点/旗标）与回合前一致，没有凭空涨；
3. **玩家看得见**：`recovered` 里带 `narration_only`（前端据此说"数值未变化"），
   否则玩家会以为正文里那句"武力大涨"真的生效了；
4. **候选项回落但不卡死**：视图只剩自由输入入口，玩家仍能继续玩；
5. **两个边界不放行**：`finish_reason=length`（正文被腰斩）与空正文仍走重试；
6. **熔断仍然存在**：连续空输出照样熔断（不是把熔断删掉了，是收窄了原因）。
"""

from __future__ import annotations

import copy
from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.storyline import FREE_INPUT_OPTION
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

PROSE = "雨点砸在青石板上。她抬手替你挡了一下，什么也没说。"


def _submit(tid: str, narration: str):
    return tool_call(
        tid, "submit_narration",
        {"narration": narration, "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )


def _game(responses):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    game.start()
    if game.state.pending_choice is not None:
        # 该包开局即关键抉择（`how_to_help`）：先解掉，后面才能自由 `say`。
        game.llm._client.chat.completions.responses.insert(
            0, resp(msg(tool_calls=[_submit("open", "你上前一步。")]))
        )
        game.pick(0)
    return game


def _snapshot(game: Game) -> dict:
    """真值快照：**只取"数值真值"**，不含回合计数（回合本来就该往前走）。"""
    return {
        "stats": dict(game.state.stats),
        "affections": copy.deepcopy(game.state.affections),
        "counters": copy.deepcopy(game.state.counters),
        "flags": copy.deepcopy(game.state.flags),
        "items": list(game.state.items),
        "action_points_left": game.state.action_points_left,
        "turn_count": game.state.turn_count,
    }


def test_prose_without_tool_call_is_accepted_and_committed():
    """正文非空 + 没调工具 → **这一轮就是这一轮**：叙事被接受、回合推进。"""
    game = _game([resp(msg(content=PROSE))])
    before = _snapshot(game)
    view = game.say("我站着不动。")

    assert view.narration == PROSE, "正文必须原样交付给玩家"
    assert game.state.turn_count == before["turn_count"] + 1, "这是一次正常回合，回合计数要走"
    assert view.recovered == ["narration_only"], f"恢复痕迹要带上原因，实际 {view.recovered}"
    assert view.choices == [FREE_INPUT_OPTION], "零效果回合只剩自由输入入口（但玩家能继续玩）"


def test_zero_effect_turn_does_not_move_any_number():
    """**零效果就是零效果**：正文里说的"大涨"不许落到真值上。"""
    game = _game([resp(msg(content="你的武力大涨，还捡到了一把宝剑。"))])
    before = _snapshot(game)
    game.say("我挥剑。")
    after = _snapshot(game)

    for key in ("stats", "affections", "counters", "flags", "items", "action_points_left"):
        assert before[key] == after[key], f"{key} 变了：{before[key]} → {after[key]}"
    assert before["turn_count"] + 1 == after["turn_count"]


def test_meltdown_is_narrowed_not_removed():
    """熔断仍然存在——连续**空输出**依旧熔断（收窄原因，不是删掉兜底）。"""
    game = _game([resp(msg()), resp(msg()), resp(msg())])
    view = game.say("我说话。")
    assert view.narration is not None and "生成失败" in view.narration
    assert "meltdown" in view.recovered
    assert "narration_only" not in view.recovered


def test_truncated_prose_is_retried_not_accepted():
    """`finish_reason=length` 的正文不接受（等于交付半句话）→ 重试后正常收尾。"""
    truncated = resp(msg(content="很长的正文，说到一半就被上限截断"), finish_reason="length")
    game = _game([truncated, resp(msg(tool_calls=[_submit("s1", "重试后的正式叙事。")]))])
    view = game.say("我等着。")
    assert view.narration == "重试后的正式叙事。"
    assert "narration_only" not in view.recovered


def test_web_view_carries_the_recovery_kind():
    """Web 端要真的把 `narration_only` 透给前端（否则界面上那句"数值未变化"不会出现）。"""
    game = _game([resp(msg(content=PROSE))])
    view = game.say("我站着不动。")
    payload = {
        "recovered": view.recovered,
        "choices": view.choices,
        "narration": view.narration,
    }
    assert payload["recovered"] == ["narration_only"]
    assert payload["narration"] == PROSE

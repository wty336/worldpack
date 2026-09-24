"""agent-first 第 2 件守卫测试：关键节点内轮自校正（Reflexion，离线假客户端）。

契约：
- 只对带 critical_choices 的进行中节点生效（开关 critique_on_critical）；
- judge 判劣（False + 有判词）→ 附结构化反馈重生成一次（至多 2 稿）；
- judge 通过 / 未知（空响应）→ 不重生成；
- 重生成后历史**只留第二稿**（第一稿叙事文本与反馈一并剥掉 = 防"两个版本都当真"）；
- turn_count 每个玩家可见回合只 +1（重生成不另计）；
- 关键节点不流式：生成期 on_text 不被调用，验收后整稿回放一次；
- 第二稿仍判劣也**接受**（不熔断）：自校正是质量优化层，不是硬门禁。
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _submit(tid: str, narration: str):
    return tool_call(
        tid, "submit_narration",
        {"narration": narration, "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )


def _game(responses, critique: bool = True):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.current_node = "n1_first_meeting"  # 带 critical_choices 的进行中节点
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm, critique_on_critical=critique)
    return pack, state, game


# ---------------------------------------------------------------------------
# 触发条件
# ---------------------------------------------------------------------------


def test_in_critical_node_detection():
    pack, state, game = _game([], critique=False)
    assert game._in_critical_node()  # n1_first_meeting 带 critical_choices
    state.current_node = None
    assert not game._in_critical_node()


def test_noncritical_node_skips_critique():
    """无节点（或普通节点）时即使开关打开也不调 judge——假客户端响应耗尽即证。"""
    pack, state, game = _game(
        [resp(msg(tool_calls=[_submit("s1", "日常叙事")]))], critique=True
    )
    state.current_node = None
    view = game._llm_round()
    assert view.narration == "日常叙事"


def test_disabled_switch_no_judge_call():
    pack, state, game = _game(
        [resp(msg(tool_calls=[_submit("s1", "初稿")]))], critique=False
    )
    view = game._llm_round()
    assert view.narration == "初稿"  # 若误调 judge 会耗尽响应抛错


# ---------------------------------------------------------------------------
# 判定三态 → 行为
# ---------------------------------------------------------------------------


def test_judge_pass_no_regeneration():
    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_submit("s1", "良好叙事")])),
            resp(msg(content="通过")),
        ],
        critique=True,
    )
    view = game._llm_round()
    assert view.narration == "良好叙事"
    assert game.state.turn_count == 1
    assert "内轮自校正" not in json.dumps(game.history, ensure_ascii=False)


def test_judge_unknown_no_regeneration():
    """空响应 = 未知 ≠ 通过：无反馈可给 → 不重生成（complete_checked 升级重试后仍空）。"""
    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_submit("s1", "初稿")])),
            resp(msg(content="")),  # judge 第一次：空
            resp(msg(content="")),  # judge 升级重试：仍空 → None
        ],
        critique=True,
    )
    view = game._llm_round()
    assert view.narration == "初稿"
    assert game.state.turn_count == 1
    assert "内轮自校正" not in json.dumps(game.history, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 判劣 → 重生成与剥稿
# ---------------------------------------------------------------------------


def _narration_texts(history: list[dict]) -> list[str]:
    """从历史提取每稿 submit_narration 的 narration 正文（arguments 是转义 JSON 字符串，
    json.dumps(history) 不会还原内层 \\uXXXX——须逐条解析，不能对整段 dump 断言）。"""
    texts: list[str] = []
    for m in history:
        for tc in m.get("tool_calls") or []:
            if tc["function"]["name"] == "submit_narration":
                texts.append(json.loads(tc["function"]["arguments"])["narration"])
    return texts


def test_fail_regenerates_and_keeps_only_second_draft():
    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_submit("s1", "第一稿：把剑名写成了听风")])),
            resp(msg(content="问题类型：设定矛盾：剑名听风与事实听雨冲突")),
            resp(msg(tool_calls=[_submit("s2", "第二稿：剑名听雨，无误")])),
        ],
        critique=True,
    )
    view = game._llm_round()
    assert view.narration == "第二稿：剑名听雨，无误"
    assert game.state.turn_count == 1  # 重生成不另计回合
    assert _narration_texts(game.history) == ["第二稿：剑名听雨，无误"]  # 第一稿已剥掉
    assert "内轮自校正" not in json.dumps(game.history, ensure_ascii=False)  # 反馈一并剥掉


def test_second_draft_still_bad_accepted():
    """第二稿仍判劣：接受（不熔断、不再循环）——自校正是优化层不是硬门禁。"""
    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_submit("s1", "第一稿：错")])),
            resp(msg(content="问题类型：设定矛盾：……")),
            resp(msg(tool_calls=[_submit("s2", "第二稿：还是错")])),
        ],
        critique=True,
    )
    view = game._llm_round()
    assert view.narration == "第二稿：还是错"
    assert _narration_texts(game.history) == ["第二稿：还是错"]


# ---------------------------------------------------------------------------
# 流式缓冲与回放
# ---------------------------------------------------------------------------


def test_stream_buffered_then_replayed_once():
    pieces: list[str] = []

    def on_text(piece: str) -> None:
        pieces.append(piece)

    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_submit("s1", "第一稿：听风")])),
            resp(msg(content="问题类型：设定矛盾：听风与听雨冲突")),
            resp(msg(tool_calls=[_submit("s2", "第二稿：听雨")])),
        ],
        critique=True,
    )
    game.on_text = on_text
    game._llm_round()
    # 生成期（两稿）都不流式；验收后整稿回放恰好一次
    assert pieces == ["第二稿：听雨"]


def test_pass_case_replays_final_once():
    pieces: list[str] = []

    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_submit("s1", "良好叙事")])),
            resp(msg(content="通过")),
        ],
        critique=True,
    )
    game.on_text = pieces.append
    game._llm_round()
    assert pieces == ["良好叙事"]  # 通过：缓冲后回放一次（玩家晚一点看到，但不看到坏稿）

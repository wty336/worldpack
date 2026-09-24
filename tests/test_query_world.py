"""query_world 只读查询工具守卫测试（agent-first 第一件，离线）。

契约：
- 只读：调用前后 state 逐字段相等（state.copy() 对拍）；
- 不泄 flags：剧情旗标是引擎内部真值，查询结果不得出现 flag 键名/原值；
- 检索同口径：复用 rank_facts / select_lore（A1 v2 BM25）；
- 协议：query_world 结果回传 tool 消息；非法参数 → [引擎拒绝]；未接线 → 协议错误。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.memory import MemoryEntry
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

QUERY = tool_call("q1", "query_world", {"query": "我的剑叫什么"})
SUBMIT = tool_call(
    "s1", "submit_narration",
    {"narration": "测试叙事", "choices": ["行动一", "行动二", "行动三"], "plot_signal": "normal"},
)


def _game():
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm)


# ---------------------------------------------------------------------------
# 处理器：内容
# ---------------------------------------------------------------------------


def test_query_returns_basic_state_lines():
    pack, state, game = _game()
    state.day = 12
    state.scene = "长安城·沈府后院"
    state.present_npcs = ["shen_qingqiu"]
    out = game._query_world({"query": "现在是什么情况"})
    assert "沈府后院" in out and "第 12 天" in out
    assert "在场：沈清秋" in out
    assert "玩家属性" in out and "魅力" in out
    assert "好感" in out and "主线目标" in out


def test_query_is_readonly():
    """只读纪律：查询前后 state 逐字段相等（含已埋入的事实/记忆/flags）。"""
    pack, state, game = _game()
    state.player_facts = [MemoryEntry(fact="玩家的剑名是听雨", day=1, round=1, importance=8)]
    state.npc_memories = {"shen_qingqiu": [MemoryEntry(fact="救过她的猫", day=1, round=1)]}
    state.flags["met_shen"] = True
    before = state.copy()
    game._query_world({"query": "听雨"})
    assert state == before


def test_query_does_not_leak_flags():
    """flag 键名是引擎内部真值，查询不得泄露（状态栏本来也不带 flags，同口径）。"""
    pack, state, game = _game()
    state.flags["poetry_top3"] = True
    state.flags["secret_identity_revealed"] = True
    out = game._query_world({"query": "secret_identity_revealed poetry_top3 秘密"})
    assert "secret_identity_revealed" not in out
    assert "poetry_top3" not in out


def test_query_retrieves_player_fact_and_lore():
    pack, state, game = _game()
    state.player_facts = [MemoryEntry(fact="玩家的剑名是听雨", day=1, round=1, importance=8)]
    out = game._query_world({"query": "我的剑叫什么"})
    assert "相关关键事实" in out and "听雨" in out
    out2 = game._query_world({"query": "东市有什么好玩的"})
    assert "相关设定" in out2 and "驼队" in out2  # lore 命中（keys 含「东市」）


def test_query_retrieves_present_npc_memory():
    pack, state, game = _game()
    state.present_npcs = ["shen_qingqiu"]
    state.npc_memories = {
        "shen_qingqiu": [MemoryEntry(fact="玩家中秋前答应替她寻回诗集", day=3, round=5, importance=7)]
    }
    out = game._query_world({"query": "诗集"})
    assert "沈清秋的相关记忆" in out and "诗集" in out


def test_query_lists_available_actions():
    pack, state, game = _game()
    state.action_points_left = 2
    out = game._query_world({"query": "今天能做什么"})
    assert "可选行动" in out and "修炼" in out and "打工" in out


def test_query_rejects_empty_and_overlong():
    pack, state, game = _game()
    with pytest.raises(ValueError, match="不能为空"):
        game._query_world({"query": "   "})
    with pytest.raises(ValueError, match="过长"):
        game._query_world({"query": "长" * 201})


# ---------------------------------------------------------------------------
# 协议（LLMClient 层）
# ---------------------------------------------------------------------------


def test_query_world_tool_fed_back_to_model():
    seen = {}

    def handler(args):
        seen.update(args)
        return "查询结果：玩家的剑名是听雨"

    pack = load_worldpack(PACK_PATH)
    client = LLMClient(
        FakeClient([resp(msg(tool_calls=[QUERY, SUBMIT]))]), "fake",
        build_tools(pack.schedule),
    )
    result = client.run_turn(
        [{"role": "user", "content": "hi"}], lambda a: "ok", query_world=handler
    )
    assert result.narration
    assert seen == {"query": "我的剑叫什么"}
    assert any(
        m["role"] == "tool" and "查询结果：玩家的剑名是听雨" in m["content"]
        for m in result.messages
    )


def test_query_world_rejection_fed_back():
    def handler(args):
        raise ValueError("query 不能为空")

    pack = load_worldpack(PACK_PATH)
    client = LLMClient(
        FakeClient([resp(msg(tool_calls=[QUERY])), resp(msg(tool_calls=[SUBMIT]))]),
        "fake", build_tools(pack.schedule),
    )
    result = client.run_turn(
        [{"role": "user", "content": "hi"}], lambda a: "ok", query_world=handler
    )
    assert result.narration
    assert any(
        m["role"] == "tool" and "[引擎拒绝] query 不能为空" in m["content"]
        for m in result.messages
    )


def test_query_world_without_callback_is_protocol_error():
    pack = load_worldpack(PACK_PATH)
    client = LLMClient(
        FakeClient([resp(msg(tool_calls=[QUERY])), resp(msg(tool_calls=[SUBMIT]))]),
        "fake", build_tools(pack.schedule),
    )
    result = client.run_turn([{"role": "user", "content": "hi"}], lambda a: "ok")
    assert result.narration
    assert any(
        m["role"] == "tool" and "未启用世界查询" in m["content"]
        for m in result.messages
    )

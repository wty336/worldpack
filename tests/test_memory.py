"""M2a 验收测试（离线）：MemorySystem 契约 + 状态栏/NPC 记忆注入。"""

from __future__ import annotations

from pathlib import Path

import pytest

from game_agent.memory import PLAYER_FACTS_LIMIT, MemoryError, MemorySystem
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _sys():
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    return pack, state, MemorySystem(pack)


def test_add_player_fact():
    pack, state, mem = _sys()
    msg = mem.add(state, "player", "我的剑名『听雨』")
    assert "已写入" in msg
    assert len(state.player_facts) == 1
    assert state.player_facts[0].fact == "我的剑名『听雨』"
    assert state.player_facts[0].round == state.turn_count


def test_add_npc_memory():
    pack, state, mem = _sys()
    mem.add(state, "shen_qingqiu", "玩家当街为她解围")
    assert len(state.npc_memories["shen_qingqiu"]) == 1


def test_dedup_by_containment():
    """v1 去重 = 文本包含关系（语义相似去重是 M2b Judge 的职责）。"""
    pack, state, mem = _sys()
    mem.add(state, "player", "剑名『听雨』")
    msg = mem.add(state, "player", "我的剑名『听雨』，是师父传的")  # 前者是后者子串 → 重复
    assert "跳过" in msg
    assert len(state.player_facts) == 1


def test_empty_fact_rejected():
    pack, state, mem = _sys()
    with pytest.raises(MemoryError, match="不能为空"):
        mem.add(state, "player", "   ")


def test_too_long_fact_rejected():
    pack, state, mem = _sys()
    with pytest.raises(MemoryError, match="过长"):
        mem.add(state, "player", "字" * 121)


def test_unknown_target_rejected():
    pack, state, mem = _sys()
    with pytest.raises(MemoryError, match="未知目标"):
        mem.add(state, "nobody", "测试")


def test_player_facts_eviction_keeps_newest():
    pack, state, mem = _sys()
    for i in range(PLAYER_FACTS_LIMIT + 2):
        state.turn_count = i
        mem.add(state, "player", f"事实 {i}")
    assert len(state.player_facts) == PLAYER_FACTS_LIMIT
    facts = [m.fact for m in state.player_facts]
    assert "事实 0" not in facts and "事实 1" not in facts  # 最早两条被淘汰
    assert f"事实 {PLAYER_FACTS_LIMIT + 1}" in facts  # 最新的保留


def test_serialization_roundtrip_with_memories():
    pack, state, mem = _sys()
    state.turn_count = 5
    mem.add(state, "player", "与沈清秋约定暗号『七月』")
    mem.add(state, "shen_qingqiu", "玩家曾为她解围")
    state2 = GameState.from_dict(state.to_dict())
    assert state2 == state

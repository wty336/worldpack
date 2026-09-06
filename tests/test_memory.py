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
        mem.add(state, "player", f"编号{i:02d}项事实")  # 零填充避免包含关系误判去重
    assert len(state.player_facts) == PLAYER_FACTS_LIMIT
    facts = [m.fact for m in state.player_facts]
    assert "编号00项事实" not in facts and "编号01项事实" not in facts  # 最早两条被淘汰
    assert f"编号{PLAYER_FACTS_LIMIT + 1:02d}项事实" in facts  # 最新的保留


def test_serialization_roundtrip_with_memories():
    pack, state, mem = _sys()
    state.turn_count = 5
    mem.add(state, "player", "与沈清秋约定暗号『七月』")
    mem.add(state, "shen_qingqiu", "玩家曾为她解围")
    state2 = GameState.from_dict(state.to_dict())
    assert state2 == state


# ---------------------------------------------------------------------------
# 迭代 4：确定性提取兜底
# ---------------------------------------------------------------------------


def test_parse_facts_strips_numbering_and_noise():
    from game_agent.memory import parse_facts

    output = (
        "1. 我的剑名『听雨』\n"
        "2、我来自江南\n"
        "无\n"
        "无。\n"  # 哨兵带句号也必须被过滤（300 轮运行抓到的幽灵事实）
        "没有\n"
        "- 我答应帮老樵夫送柴\n"
        "\n"
        "   \n"
    )
    facts = parse_facts(output)
    assert facts == ["我的剑名『听雨』", "我来自江南", "我答应帮老樵夫送柴"]
    assert all("无" not in f or len(f) > 1 for f in facts)


def test_parse_facts_caps_at_five():
    from game_agent.memory import parse_facts

    output = "\n".join(f"事实{i}" for i in range(1, 9))
    assert len(parse_facts(output)) == 5

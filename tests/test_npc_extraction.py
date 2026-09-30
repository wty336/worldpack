"""NPC 侧确定性提取守卫。

**缺陷背景**：`_extract_facts` 此前硬编码 `target="player"`——玩家侧有确定性兜底
（"弥补 remember 主动性的覆盖缺口"），**NPC 侧没有**。于是 NPC 记忆只能靠模型自愿
调用 `remember`，而 A3 反思要求单 NPC 攒够 `REFLECT_MIN_MEMORIES`(8) 条：
多 NPC 包里对多数 NPC 静默空转，且模型"该记却没记"的问题在 NPC 侧同样存在。

**为什么必须先有在场真值（D13）**：归属判定的唯一依据是"谁在场"。在场此前只是
上次行动的残留副作用、且模型无法改变它，所以那时做提取只会把事实归给错误的 id。
现在 `change_presence` 让它成了真值，提取才有可靠依据。

设计选择：归属交给**同一次提取调用**（提示词新增可选「角色id|重要性|事实」），
而不是引擎盲发给所有在场 NPC——后者会把私密事实分发给没在场的人，比记忆稀疏更糟。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.memory import parse_facts, parse_targeted_facts
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

OTOME = Path(__file__).resolve().parent.parent / "world-packs" / "campus_otome"
NPC = "jiang_yu"


# ---------------------------------------------------------------------------
# 解析：目标前缀（向后兼容）
# ---------------------------------------------------------------------------


def test_parse_targeted_supports_all_three_formats():
    out = "\n".join([
        "8|玩家的剑名是听雨",          # 旧格式 → player
        f"{NPC}|7|他私下告诉江屿一件事",  # 目标|重要性|事实
        f"{NPC}|他把伞借给了她",        # 目标|事实（目标可识别时）
    ])
    parsed = parse_targeted_facts(out, {"player", NPC})

    assert parsed[0] == ("player", 8.0, "玩家的剑名是听雨")
    assert parsed[1] == (NPC, 7.0, "他私下告诉江屿一件事")
    assert parsed[2] == (NPC, 5.0, "他把伞借给了她")


def test_two_part_target_requires_known_roster():
    """两段式的首段必须是已知目标才认——否则普通文本里的 `|` 会被误拆。"""
    out = f"{NPC}|他把伞借给了她\nabc|非法前缀"

    # 不给 roster：两段式一律按整行事实处理（与旧行为一致）
    assert parse_targeted_facts(out) == [
        ("player", 5.0, f"{NPC}|他把伞借给了她"),
        ("player", 5.0, "abc|非法前缀"),
    ]
    # 给了 roster：已知目标才被认出来
    assert parse_targeted_facts(out, {"player", NPC})[0] == (NPC, 5.0, "他把伞借给了她")


def test_parse_facts_backward_compatible():
    """旧入口必须只返回玩家事实（既有调用方/测试的契约）。"""
    out = f"8|玩家事实\n{NPC}|7|江屿知道的"
    assert parse_facts(out) == [("玩家事实", 8.0)]


def test_parse_targeted_tolerates_noise():
    out = "1. 7|事实一\n- 无\n\n无。\n-* 8|事实二"
    parsed = parse_targeted_facts(out)
    assert parsed == [("player", 7.0, "事实一"), ("player", 8.0, "事实二")]


def test_parse_targeted_keeps_pipe_in_fact_body():
    """事实正文里含 `|` 时不得被截断（三段之后全部归正文）。"""
    out = f"{NPC}|6|他说「A|B」这个暗号"
    assert parse_targeted_facts(out) == [(NPC, 6.0, "他说「A|B」这个暗号")]


# ---------------------------------------------------------------------------
# 端到端：在场角色能拿到确定性记忆
# ---------------------------------------------------------------------------


def _game(present: list[str]):
    pack = load_worldpack(OTOME)
    state = GameState.from_pack(pack)
    state.present_npcs = present
    state.turn_count = 2
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    # `_extract_facts` 在 `_recent_text()` 为空时早退（没有可提炼的内容）——
    # 夹具必须给出一个真实回合，否则用例会"通过"却没走到提取逻辑。
    game.history = [
        {"role": "user", "content": "我把伞借给他了"},
        {"role": "assistant", "content": "他把伞收下，点了点头。"},
    ]
    return pack, state, game


def test_present_npc_gets_deterministic_memory(monkeypatch):
    """复现钉：提取器把某条事实归给在场 NPC 时，必须写进该 NPC 的记忆桶。"""
    pack, state, game = _game(present=[NPC])
    output = f"{NPC}|7|江屿记得玩家借过他的伞"

    import game_agent.game as G

    monkeypatch.setattr(
        G, "complete_with_empty_retry",
        lambda llm, messages, purpose, max_tokens, **kw: output,
    )
    game._extract_facts()

    facts = [m.fact for m in state.npc_memories.get(NPC, [])]
    assert "江屿记得玩家借过他的伞" in facts, (
        f"在场 NPC 没有拿到确定性记忆（实际 {facts}）"
    )


def test_absent_npc_does_not_receive_memory(monkeypatch):
    """归属不在场 → 退化为玩家事实，**不得**凭空写进该 NPC 的记忆。"""
    pack, state, game = _game(present=[])  # 无人在场
    output = f"{NPC}|7|某条不该归给江屿的事实"

    import game_agent.game as G

    monkeypatch.setattr(
        G, "complete_with_empty_retry",
        lambda llm, messages, purpose, max_tokens, **kw: output,
    )
    game._extract_facts()

    assert not state.npc_memories.get(NPC), "不在场的 NPC 收到了记忆——归属判定失效"
    assert any(m.fact == "某条不该归给江屿的事实" for m in state.player_facts), (
        "归属无效时应退化为玩家事实"
    )


def test_roster_is_included_in_extract_prompt(monkeypatch):
    """在场名单必须进提示词——否则模型无从判断该归给谁。"""
    pack, state, game = _game(present=[NPC])
    captured = {}

    import game_agent.game as G

    def fake(llm, messages, purpose, max_tokens, **kw):
        captured["user"] = messages[-1]["content"]
        return "无"

    monkeypatch.setattr(G, "complete_with_empty_retry", fake)
    game._extract_facts()

    assert "<在场角色>" in captured["user"], "提示词缺少在场角色区块"
    assert NPC in captured["user"], "提示词缺少在场角色 id"


def test_no_roster_block_when_nobody_present(monkeypatch):
    """无人在场时不提这个话题（避免多余的输出格式分支）。"""
    pack, state, game = _game(present=[])
    captured = {}

    import game_agent.game as G

    def fake(llm, messages, purpose, max_tokens, **kw):
        captured["user"] = messages[-1]["content"]
        return "无"

    monkeypatch.setattr(G, "complete_with_empty_retry", fake)
    game._extract_facts()

    assert "<在场角色>" not in captured["user"]


def test_player_targeted_fact_still_goes_to_player(monkeypatch):
    """回归保护：归属为 player 的事实仍进玩家事实桶（别把旧路径改坏）。"""
    pack, state, game = _game(present=[NPC])
    output = "9|玩家的剑名是听雨"

    import game_agent.game as G

    monkeypatch.setattr(
        G, "complete_with_empty_retry",
        lambda llm, messages, purpose, max_tokens, **kw: output,
    )
    game._extract_facts()

    assert any(m.fact == "玩家的剑名是听雨" for m in state.player_facts)
    assert not state.npc_memories.get(NPC), "玩家事实被误写进了 NPC 记忆"

"""P1·A 系列验收测试（离线）：A1 检索式注入 / A2 重要性 / A4 语义去重。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeClient, msg, resp

from game_agent.context import ContextBuilder
from game_agent.llm import LLMClient
from game_agent.memory import (
    PLAYER_FACTS_LIMIT,
    MemoryEntry,
    MemoryError,
    MemorySystem,
    rank_facts,
)
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _sys(llm=None):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    return pack, state, MemorySystem(pack, llm)


def _entry(fact: str, round_: int, importance: float = 5.0) -> MemoryEntry:
    return MemoryEntry(fact=fact, day=1, round=round_, importance=importance)


# ---------------------------------------------------------------------------
# A2：重要性
# ---------------------------------------------------------------------------


def test_a2_importance_defaults_to_five():
    _, state, mem = _sys()
    mem.add(state, "player", "玩家爱喝龙井")
    assert state.player_facts[0].importance == 5.0


def test_a2_importance_out_of_range_rejected():
    _, state, mem = _sys()
    with pytest.raises(MemoryError, match="1~10"):
        mem.add(state, "player", "测试", importance=11)
    with pytest.raises(MemoryError, match="1~10"):
        mem.add(state, "player", "测试", importance=0)
    with pytest.raises(MemoryError, match="必须是 1~10 的数字"):
        mem.add(state, "player", "测试", importance="高")


def test_a2_high_importance_survives_eviction():
    """满桶低重要 + 1 条高重要：高重要事实不被淘汰（A2 验收场景）。"""
    _, state, mem = _sys()
    state.turn_count = 1
    mem.add(state, "player", "玩家救过沈清秋的命", importance=10)
    for i in range(PLAYER_FACTS_LIMIT):
        state.turn_count = i + 2
        mem.add(state, "player", f"琐事{i:02d}号", importance=1)
    assert len(state.player_facts) == PLAYER_FACTS_LIMIT
    assert any(m.fact == "玩家救过沈清秋的命" for m in state.player_facts)


def test_a2_eviction_drops_low_importance_first():
    """淘汰顺序：低重要性先走，同重要性按时间。"""
    _, state, mem = _sys()
    state.turn_count = 1
    mem.add(state, "player", "旧琐事", importance=2)
    state.turn_count = 2
    mem.add(state, "player", "旧大事", importance=9)
    for i in range(PLAYER_FACTS_LIMIT - 1):
        state.turn_count = i + 3
        mem.add(state, "player", f"新琐事{i:02d}", importance=2)
    facts = [m.fact for m in state.player_facts]
    assert "旧大事" in facts
    assert "旧琐事" not in facts  # 同 bucket 内最低重要性先被淘汰


def test_a2_save_roundtrip_and_legacy_default():
    _, state, mem = _sys()
    mem.add(state, "player", "身份：玩家是剑客", importance=9)
    mem.add(state, "player", "玩家爱喝龙井", importance=3)
    restored = GameState.from_dict(state.to_dict())
    assert restored == state
    # 旧版存档（无 importance 字段）→ 缺省 5
    legacy = state.to_dict()
    for m in legacy["player_facts"]:
        del m["importance"]
    restored_legacy = GameState.from_dict(legacy)
    assert all(m.importance == 5.0 for m in restored_legacy.player_facts)


# ---------------------------------------------------------------------------
# A1：检索式注入
# ---------------------------------------------------------------------------


def test_a1_pinned_zone_always_included():
    """R1 常驻区：最高重要性的 K 条恒注入。"""
    entries = [_entry(f"事实{i}", i, importance=1.0) for i in range(20)]
    entries += [_entry("核心身世", 0, importance=10.0), _entry("生死承诺", 1, importance=9.0)]
    selected = rank_facts(entries, "", now_round=30, k=10, pinned=3)
    assert selected[0].fact == "核心身世"  # 常驻区在前（重要性降序）
    assert any(m.fact == "生死承诺" for m in selected)
    assert len(selected) <= 10 + 3


def test_a1_recency_importance_relevance_ordering():
    """三因子：同龄同重要性下，与上下文相关的事实胜出（relevance 生效）。"""
    now = 100
    entries = [
        _entry("与上下文相关的旧事实", 10, importance=5.0),
        _entry("普通日常琐事", 10, importance=5.0),
        _entry("老而重要的事实", 0, importance=9.0),
    ]
    selected = rank_facts(entries, "上下文相关", now_round=now, k=2, pinned=0)
    facts = [m.fact for m in selected]
    assert "老而重要的事实" in facts  # importance 高 → 入选
    assert "与上下文相关的旧事实" in facts  # 同龄同重要下 relevance 胜出
    assert "普通日常琐事" not in facts


def test_a1_status_bar_token_flat_with_many_facts():
    """50+ 条事实 vs 20 条：状态栏长度不随条目数线性增长（A1 验收）。"""
    pack = load_worldpack(PACK_PATH)
    builder = ContextBuilder.from_pack(pack)

    def status_with(n: int) -> str:
        state = GameState.from_pack(pack)
        state.turn_count = 200
        state.player_facts = [
            MemoryEntry(fact=f"事实{i:03d}号内容", day=1, round=200 - i, importance=5.0)
            for i in range(n)
        ]
        return builder.status_text(state, None, recent="")

    small, large = status_with(20), status_with(60)
    assert len(large) <= len(small) + 200  # 检索注入后状态栏近乎恒定（K=10+常驻3）


def test_a1_npc_memories_retrieved_not_all():
    """NPC 记忆同样检索注入：40 条只注入常驻+top-K。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.turn_count = 100
    state.present_npcs = ["shen_qingqiu"]
    state.npc_memories["shen_qingqiu"] = [
        MemoryEntry(fact=f"记忆{i:02d}号", day=1, round=100 - i, importance=5.0)
        for i in range(40)
    ]
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="")
    injected = sum(1 for line in text.splitlines() if line.startswith("- （第"))
    assert 0 < injected <= 13  # 常驻 3 + 检索 K=10，绝不 40 条全量


# ---------------------------------------------------------------------------
# A4：语义去重
# ---------------------------------------------------------------------------


def test_a4_semantic_duplicate_blocked_by_llm():
    """字符串不包含但语义近义 → 轻量判定『重复』拦截。"""
    llm = LLMClient(FakeClient([resp(msg(content="重复"))]), "fake", [])
    _, state, mem = _sys(llm)
    mem.add(state, "player", "剑名听雨")
    out = mem.add(state, "player", "佩剑唤作听雨")  # 非包含关系 → 走语义判定
    assert "语义重复" in out
    assert len(state.player_facts) == 1


def test_a4_semantic_distinct_written():
    llm = LLMClient(FakeClient([resp(msg(content="不重复"))]), "fake", [])
    _, state, mem = _sys(llm)
    mem.add(state, "player", "剑名听雨")
    out = mem.add(state, "player", "玩家来自江南水乡")
    assert "已写入" in out and len(state.player_facts) == 2


def test_a4_prefilter_skips_llm_when_no_bigram_overlap():
    """无任何二元组重叠 → 不调用模型（成本保护）。"""
    fake = FakeClient([])  # 响应耗尽：若被调用会抛错
    llm = LLMClient(fake, "fake", [])
    _, state, mem = _sys(llm)
    mem.add(state, "player", "剑名听雨")
    out = mem.add(state, "player", "张三丰")  # 与「剑名听雨」无重叠二元组
    assert "已写入" in out  # 未触发模型调用即判定不重复
    assert len(fake.chat.completions.calls) == 0


def test_a4_llm_failure_silently_writes():
    """判定调用失败 → 静默降级为不重复（写入路径不中断）。"""

    class _Boom:
        def __init__(self):
            self.chat = type(
                "C", (), {"completions": type("CC", (), {"create": self._fail})()}
            )()

        @staticmethod
        def _fail(**kwargs):
            raise RuntimeError("api down")

    llm = LLMClient(_Boom(), "fake", [])
    _, state, mem = _sys(llm)
    mem.add(state, "player", "剑名听雨")
    out = mem.add(state, "player", "佩剑唤作听雨")
    assert "已写入" in out and len(state.player_facts) == 2


# ---------------------------------------------------------------------------
# A3：反思层
# ---------------------------------------------------------------------------


def test_a3_parse_insights():
    from game_agent.memory import parse_insights

    output = (
        "沈清秋对玩家的态度正从客气转向信任|1,2,3\n"
        "无\n"
        "1. 玩家多次相助于她|1\n"
        "无。\n"
    )
    insights = parse_insights(output)
    assert insights == [
        ("沈清秋对玩家的态度正从客气转向信任", (1, 2, 3)),
        ("玩家多次相助于她", (1,)),
    ]


def test_a3_reflection_synthesizes_with_sources(tmp_path):
    """反思触发：合成洞察带来源引用（映射到素材原文）。"""
    from game_agent.game import Game
    from game_agent.state import MemoryEntry

    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.npc_memories["shen_qingqiu"] = [
        MemoryEntry(fact=f"记忆{i:02d}", day=1, round=i, importance=5.0) for i in range(9)
    ]
    llm = LLMClient(
        FakeClient([resp(msg(content="态度转向信任|1,2"))]), "fake", []
    )
    game = Game(pack, state, llm)
    game.reflect_every = 10
    state.turn_count = 10
    game._reflect()
    insights = state.npc_insights["shen_qingqiu"]
    assert len(insights) == 1
    assert insights[0].text == "态度转向信任"
    assert insights[0].sources == ("记忆00", "记忆01")  # 素材列表编号 1,2


def test_a3_insight_cap_replaces_oldest():
    """洞察新替旧：cap 2，第三条进来最旧的被替换。"""
    from game_agent.game import Game
    from game_agent.state import MemoryEntry

    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.npc_memories["shen_qingqiu"] = [
        MemoryEntry(fact=f"记忆{i:02d}", day=1, round=i, importance=5.0) for i in range(9)
    ]
    llm = LLMClient(
        FakeClient([
            resp(msg(content="洞察一|1")),
            resp(msg(content="洞察二|2")),
            resp(msg(content="洞察三|3")),
        ]),
        "fake", [],
    )
    game = Game(pack, state, llm)
    for i in range(3):
        state.turn_count = 10 * (i + 1)
        game._reflect_npc("shen_qingqiu", state.npc_memories["shen_qingqiu"])
    texts = [i.text for i in state.npc_insights["shen_qingqiu"]]
    assert texts == ["洞察二", "洞察三"]  # cap 2：洞察一被替换


def test_a3_reflect_skips_when_no_new_memories():
    """记忆无新增 → 不重复反思（C-5：round 门控，可从存档重建）。"""
    from game_agent.game import Game

    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    from game_agent.state import MemoryEntry

    state.npc_memories["shen_qingqiu"] = [
        MemoryEntry(fact=f"记忆{i:02d}", day=1, round=i, importance=5.0) for i in range(8)
    ]
    fake = FakeClient([resp(msg(content="洞察一|1"))])  # 1 个响应：再次调用即耗尽抛错
    llm = LLMClient(fake, "fake", [])
    game = Game(pack, state, llm)
    game.reflect_every = 10
    state.turn_count = 10
    game._reflect()
    assert len(fake.chat.completions.calls) == 1  # 第一次触发（8 ≥ 8），洞察 round=10
    state.turn_count = 20
    game._reflect()  # 最新记忆 round（7）≤ 洞察 round（10）→ 跳过
    assert len(fake.chat.completions.calls) == 1


def test_a3_reflect_no_duplicate_after_save_load():
    """C-5（m3）：门控可从存档重建——读档后对同一批记忆不重复反思。"""
    import json

    from game_agent.game import Game
    from game_agent.state import MemoryEntry

    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.npc_memories["shen_qingqiu"] = [
        MemoryEntry(fact=f"记忆{i:02d}", day=1, round=i, importance=5.0) for i in range(8)
    ]
    llm = LLMClient(FakeClient([resp(msg(content="洞察一|1"))]), "fake", [])
    game = Game(pack, state, llm)
    state.turn_count = 10
    game._reflect()
    assert len(state.npc_insights["shen_qingqiu"]) == 1

    # 模拟存档 → 新进程读档：门控状态不在 Game 内，必须能从 state 重建
    restored = GameState.from_dict(json.loads(json.dumps(state.to_dict(), ensure_ascii=False)))
    new_game = Game(pack, restored, llm)
    calls_before = len(llm._client.chat.completions.calls)
    restored.turn_count = 20
    new_game._reflect()  # 记忆最新 round（7）≤ 洞察 round（10）→ 不重复反思
    assert len(restored.npc_insights["shen_qingqiu"]) == 1
    assert len(llm._client.chat.completions.calls) == calls_before


def test_a3_insights_injected_into_npc_card():
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.present_npcs = ["shen_qingqiu"]
    from game_agent.state import InsightEntry

    state.npc_insights["shen_qingqiu"] = [
        InsightEntry(text="沈清秋对玩家的态度正从客气转向信任", day=5, round=40, sources=())
    ]
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="")
    assert "关系洞察：" in text
    assert "态度正从客气转向信任" in text


def test_a3_save_roundtrip_with_insights():
    import json

    from game_agent.state import InsightEntry

    pack, state, mem = _sys()
    state.npc_insights["shen_qingqiu"] = [
        InsightEntry(text="态度转向信任", day=3, round=20, sources=("记忆A", "记忆B"))
    ]
    # C-3（m1）：经真实 json.dumps/loads 回环（模拟存档文件落盘），sources 归一化回 tuple
    restored = GameState.from_dict(json.loads(json.dumps(state.to_dict(), ensure_ascii=False)))
    assert restored.npc_insights == state.npc_insights
    assert isinstance(restored.npc_insights["shen_qingqiu"][0].sources, tuple)


# ---------------------------------------------------------------------------
# 鲁棒性：模型工具调用缺参数（真机触发过的崩溃）
# ---------------------------------------------------------------------------


def test_remember_missing_args_returns_structured_error():
    """remember 缺 target/fact → 结构化 tool 错误，回合正常完成（不崩溃）。"""
    from fakes import FakeClient, msg, resp, tool_call

    from game_agent.game import Game
    from game_agent.llm import LLMClient, build_tools

    BAD_REMEMBER = tool_call("b1", "remember", {})  # 模型漏参
    SUBMIT = tool_call(
        "s1", "submit_narration",
        {"narration": "测试叙事", "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.completed_nodes.append("n1_first_meeting")
    state.flags["met_shen"] = True
    llm = LLMClient(
        FakeClient([resp(msg(tool_calls=[BAD_REMEMBER, SUBMIT]))]), "fake",
        build_tools(pack.schedule),
    )
    game = Game(pack, state, llm)
    view = game.say("测试")
    assert view.narration == "测试叙事"  # 回合未中断
    assert "缺少参数" in game.history[-2]["content"]  # 结构化拒绝回传模型

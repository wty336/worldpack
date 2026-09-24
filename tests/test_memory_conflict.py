"""A5（runtime 平台化 ①）守卫测试：记忆时序冲突消解（离线，FakeClient 替身）。

契约（docs/plan-runtime-platform.md §1）：
- 写入时判定：bigram 短名单 → conflict 侧信道（三态：无冲突/并存/取代）；
- 取代 → 旧条目 superseded=True（保留存储 = 溯源可查，不删除）；
- 失败/空/无法识别 → 无冲突（判不出来就不取代，绝不误杀）；
- 三处排除：rank_facts 注入（含常驻区）、factgraph 接地、淘汰优先；
- 存档回环：superseded 随 to_dict/from_dict 往返；旧档缺字段回退 False。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp

from game_agent.factgraph import build_graph, check_graph
from game_agent.llm import LLMClient
from game_agent.memory import MemorySystem, parse_conflict, rank_facts
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _system(responses):
    """MemorySystem + FakeClient：响应按调用序 = 先去重、后冲突（都只在 bigram 命中时触发）。"""
    llm = LLMClient(FakeClient(responses), "fake", [])
    return MemorySystem(_pack(), llm)


def _state():
    return GameState.from_pack(_pack())


# ---------------------------------------------------------------------------
# 三态解析
# ---------------------------------------------------------------------------


def test_parse_conflict_three_states():
    assert parse_conflict("无冲突") == ("none", [])
    assert parse_conflict("并存") == ("coexist", [])
    assert parse_conflict("取代：2") == ("supersede", [2])
    assert parse_conflict("取代：1、3") == ("supersede", [1, 3])
    assert parse_conflict("") == ("none", [])
    assert parse_conflict("乱七八糟的回复") == ("none", [])  # 未知 → 不取代


# ---------------------------------------------------------------------------
# 写入路径
# ---------------------------------------------------------------------------


def test_supersede_marks_old_entry():
    sys = _system([resp(msg(content="不重复")), resp(msg(content="取代：1"))])
    state = _state()
    state.player_facts = [MemoryEntry(fact="沈清秋对你冷淡疏远", day=1, round=1, importance=6)]
    sys.add(state, "player", "沈清秋如今对你亲近信任")
    old, new = state.player_facts
    assert old.superseded is True and new.superseded is False  # 保留存储 + 标记取代


def test_coexist_keeps_both_active():
    sys = _system([resp(msg(content="不重复")), resp(msg(content="并存"))])
    state = _state()
    state.player_facts = [MemoryEntry(fact="沈清秋欠你一个人情", day=1, round=1, importance=6)]
    sys.add(state, "player", "沈清秋送你一方手帕")
    assert all(not m.superseded for m in state.player_facts)


def test_no_lexical_overlap_skips_conflict_call():
    """bigram 零重叠 → 短名单为空 → 不调任何 LLM（空队列耗尽即证）。"""
    sys = _system([])
    state = _state()
    state.player_facts = [MemoryEntry(fact="长安的雨季漫长", day=1, round=1)]
    sys.add(state, "player", "玩家的剑名是听雨")
    assert len(state.player_facts) == 2 and all(not m.superseded for m in state.player_facts)


def test_conflict_failure_silent_no_supersede():
    """冲突判定空 → 升级重试仍空 → 无取代（写入路径不因判定失败中断）。"""
    sys = _system([resp(msg(content="不重复")), resp(msg(content="")), resp(msg(content=""))])
    state = _state()
    state.player_facts = [MemoryEntry(fact="沈清秋对你冷淡疏远", day=1, round=1, importance=6)]
    sys.add(state, "player", "沈清秋如今对你亲近信任")
    assert all(not m.superseded for m in state.player_facts)


# ---------------------------------------------------------------------------
# 三处排除
# ---------------------------------------------------------------------------


def test_rank_facts_excludes_superseded_even_pinned():
    """被取代的旧事实不得注入——即使它是最高重要性的常驻候选。"""
    entries = [
        MemoryEntry(fact="核心身世（已被取代）", day=1, round=0, importance=10.0, superseded=True),
        MemoryEntry(fact="普通事实", day=1, round=1, importance=5.0),
    ]
    out = rank_facts(entries, "", now_round=10, k=5, pinned=3)
    assert [m.fact for m in out] == ["普通事实"]


def test_factgraph_does_not_ground_superseded_fact():
    """被取代的旧事实不得再给断言接地——锚点只有旧事实才含有时，取代后即判虚构；
    未取代时同一断言接地通过（对照组，证明排除确实来自 superseded 标记）。"""
    pack = _pack()
    state = _state()
    state.player_facts = [
        MemoryEntry(fact="玩家的师父是青云道长", day=1, round=1, importance=7, superseded=True),
    ]
    graph = build_graph(pack, state)
    llm = LLMClient(FakeClient([resp(msg(content="师父青云道长仍在世"))]), "fake", [])
    v = check_graph(llm, "你得知师父青云道长仍在世", graph)
    assert v and "虚构事实" in v
    state.player_facts[0].superseded = False  # 对照组：未取代 → 接地通过
    graph2 = build_graph(pack, state)
    llm2 = LLMClient(FakeClient([resp(msg(content="师父青云道长仍在世"))]), "fake", [])
    assert check_graph(llm2, "你得知师父青云道长仍在世", graph2) is None


def test_eviction_prefers_superseded():
    """满额淘汰：superseded 条目优先出局（死权重最轻）。"""
    pack = _pack()
    state = _state()
    sys = MemorySystem(pack, None)  # llm=None：去重/冲突判定全跳过
    for i in range(23):
        state.player_facts.append(MemoryEntry(fact=f"活跃事实{i}", day=1, round=i, importance=10.0))
    state.player_facts.append(
        MemoryEntry(fact="被取代的旧事实", day=1, round=99, importance=10.0, superseded=True)
    )
    sys.add(state, "player", "一条新事实")
    assert len(state.player_facts) == 24  # 触发淘汰
    assert all(m.fact != "被取代的旧事实" for m in state.player_facts)


# ---------------------------------------------------------------------------
# 存档回环
# ---------------------------------------------------------------------------


def test_save_roundtrip_superseded_and_legacy_default():
    state = _state()
    state.player_facts = [MemoryEntry(fact="旧事实", day=1, round=1, superseded=True)]
    back = GameState.from_dict(state.to_dict())
    assert back.player_facts[0].superseded is True
    # 老档（无 superseded 字段）→ 回退 False
    d = state.to_dict()
    d["player_facts"] = [{"fact": "旧事实", "day": 1, "round": 1}]
    legacy = GameState.from_dict(d)
    assert legacy.player_facts[0].superseded is False

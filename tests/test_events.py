"""W6 验收测试：事件系统（条件触发/日程概率触发/once 防重/优先级仲裁）。"""

from __future__ import annotations

from pathlib import Path

from game_agent.events import EventSystem
from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.worldpack import EventSpec, EventTrigger, load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


class _StubRng:
    """固定返回值 rng：0.1 恒触发，0.9 恒不触发。"""

    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def _sys(rng=None):
    pack = load_worldpack(PACK_PATH)
    stats = StatsSystem(pack.schedule)
    state = GameState.from_pack(pack)
    return pack, state, EventSystem(pack, stats, rng)


def test_condition_event_triggers_at_threshold_and_once():
    pack, state, events = _sys()
    state.affections["shen_qingqiu"] = 30.0
    ev = events.check_condition_events(state)
    assert ev is not None and ev.id == "ev_moon_chat"
    msg = events.trigger(state, ev)
    assert "【事件】月下谈心" in msg["content"]
    assert state.affections["shen_qingqiu"] == 35.0  # 效果结算 +5
    assert "ev_moon_chat" in state.triggered_events
    assert events.check_condition_events(state) is None  # once 防重


def test_condition_event_below_threshold():
    pack, state, events = _sys()
    state.affections["shen_qingqiu"] = 29.0
    assert events.check_condition_events(state) is None


def test_schedule_event_triggers_by_chance():
    pack, state, events = _sys(_StubRng(0.1))
    ev = events.check_schedule_event(state, "cultivate")
    assert ev is not None and ev.id == "ev_cliff_encounter"
    msg = events.trigger(state, ev)
    assert state.stats["martial"] == 7.0  # 5 + 2
    assert "后山奇遇" in msg["content"]
    assert events.check_schedule_event(state, "cultivate") is None  # once


def test_schedule_event_not_triggered_when_rng_high():
    pack, state, events = _sys(_StubRng(0.9))
    assert events.check_schedule_event(state, "cultivate") is None


def test_schedule_event_only_matches_its_action():
    pack, state, events = _sys(_StubRng(0.1))
    assert events.check_schedule_event(state, "work") is None  # 后山奇遇只挂修炼


def test_priority_arbitration_picks_highest():
    """同条件多个事件：优先返回 priority=high 的（同一时段只放一个主事件）。"""
    pack, state, events = _sys()
    e_high = EventSpec(
        id="e_high",
        title="高优先级",
        trigger=EventTrigger(kind="condition", when={"day": {"gte": 1}}),
        priority="high", effects={}, once=True,
    )
    e_low = EventSpec(
        id="e_low", title="低优先级",
        trigger=EventTrigger(kind="condition", when={"day": {"gte": 1}}),
        priority="low", effects={}, once=True,
    )
    pack.events.events = [e_low, e_high]  # 故意乱序
    ev = events.check_condition_events(state)
    assert ev is not None and ev.id == "e_high"


def test_trigger_notes_effects_in_message():
    pack, state, events = _sys(_StubRng(0.1))
    ev = events.check_schedule_event(state, "cultivate")
    msg = events.trigger(state, ev)
    assert "武功 +2" in msg["content"]


# ---------------------------------------------------------------------------
# W-B：时间触发
# ---------------------------------------------------------------------------


def test_time_event_triggers_at_day_and_once():
    pack, state, events = _sys()
    ev = EventSpec(
        id="e_day3",
        title="第3天信使",
        trigger=EventTrigger(kind="time", when={"day": {"gte": 3}}),
        priority="normal",
        effects={"stats": {"charm": 1}},
        once=True,
    )
    pack.events.events.append(ev)
    assert events.check_time_events(state) == []  # 第 1 天不触发
    state.day = 3
    due = events.check_time_events(state)
    assert len(due) == 1 and due[0].id == "e_day3"
    msg = events.trigger(state, due[0])
    assert "【事件】第3天信使" in msg["content"]
    assert state.stats["charm"] == 11.0
    assert events.check_time_events(state) == []  # once 防重


def test_time_events_sorted_by_priority():
    pack, state, events = _sys()
    e_low = EventSpec(
        id="e_t_low", title="低",
        trigger=EventTrigger(kind="time", when={"day": {"gte": 1}}),
        priority="low", effects={}, once=True,
    )
    e_high = EventSpec(
        id="e_t_high", title="高",
        trigger=EventTrigger(kind="time", when={"day": {"gte": 1}}),
        priority="high", effects={}, once=True,
    )
    pack.events.events = [e_low, e_high]  # 乱序
    due = events.check_time_events(state)
    assert [e.id for e in due] == ["e_t_high", "e_t_low"]


# ---------------------------------------------------------------------------
# W-A：RNG 可复现
# ---------------------------------------------------------------------------


def test_same_seed_same_event_sequence():
    """W-A：同 seed 的 RNG → 日程事件触发序列完全一致（通关脚本可复现的地基）。"""
    import random

    pack = load_worldpack(PACK_PATH)
    stats1, stats2 = StatsSystem(pack.schedule), StatsSystem(pack.schedule)
    s1, s2 = GameState.from_pack(pack), GameState.from_pack(pack)
    e1 = EventSystem(pack, stats1, random.Random(42))
    e2 = EventSystem(pack, stats2, random.Random(42))
    seq1, seq2 = [], []
    for _ in range(10):
        ev1 = e1.check_schedule_event(s1, "cultivate")
        ev2 = e2.check_schedule_event(s2, "cultivate")
        seq1.append(ev1.id if ev1 else None)
        seq2.append(ev2.id if ev2 else None)
        if ev1:
            e1.trigger(s1, ev1)
        if ev2:
            e2.trigger(s2, ev2)
    assert seq1 == seq2
    assert any(x is not None for x in seq1)  # seed 42 下确实有事件触发，测试有意义

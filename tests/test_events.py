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
        id="e_high", title="高优先级",
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

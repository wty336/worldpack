"""W6 验收测试：日程系统（行动点、结算、日期推进、场景切换）。"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from game_agent.schedule import ScheduleError, ScheduleSystem
from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


class _FixedRng:
    """random() 恒 0.5 → uniform(a,b) 取中点：收益曲线取基准值、检定掷值 = 属性值。"""

    def random(self):
        return 0.5

    def uniform(self, a, b):
        return a + (b - a) * self.random()


def _sys():
    pack = load_worldpack(PACK_PATH)
    stats = StatsSystem(pack.schedule)
    state = GameState.from_pack(pack)
    return pack, state, ScheduleSystem(pack, stats, rng=_FixedRng())


def test_initial_action_points():
    _, state, _ = _sys()
    assert state.action_points_left == 1  # 来自世界包 day_action_points


def test_execute_action_applies_effects_and_consumes_points():
    _, state, sched = _sys()
    outcome = sched.execute_action(state, "cultivate")
    # 修炼收益曲线（base 4, spread 1）+ 固定 rng（中点）→ 4.0；武功 5 → 9.0
    assert state.stats["martial"] == 9.0
    assert state.action_points_left == 0
    assert state.scene == "长安城郊·后山"  # 行动声明场景
    assert state.present_npcs == []
    assert outcome.check is None  # 修炼无检定
    assert "武功" in outcome.notes[0]


def test_execute_without_points_raises():
    _, state, sched = _sys()
    sched.execute_action(state, "cultivate")
    with pytest.raises(ScheduleError, match="行动点不足"):
        sched.execute_action(state, "work")


def test_unknown_action_raises():
    _, state, sched = _sys()
    with pytest.raises(ScheduleError, match="未知日程行动"):
        sched.execute_action(state, "nonexistent")


def test_visit_shen_sets_scene_and_present():
    _, state, sched = _sys()
    sched.execute_action(state, "visit_shen")
    assert state.scene == "长安城·沈府"
    assert state.present_npcs == ["shen_qingqiu"]


def test_end_day_advances_and_resets():
    _, state, sched = _sys()
    sched.execute_action(state, "work")
    assert state.action_points_left == 0
    sched.end_day(state)
    assert state.day == 2
    assert state.action_points_left == 1
    assert state.stats["silver"] == 65.0  # 结算持久保持

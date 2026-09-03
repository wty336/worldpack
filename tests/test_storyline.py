"""W5 验收测试：节点触发/完成/卡壳保护、关键选项接管、结局判定、选项过滤。"""

from __future__ import annotations

from pathlib import Path

import pytest

from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.storyline import (
    FREE_INPUT_OPTION,
    StorylineEngine,
    StorylineError,
    filter_choices,
)
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _engine(**kw):
    pack = load_worldpack(PACK_PATH)
    stats = StatsSystem(pack.schedule)
    state = GameState.from_pack(pack)
    return pack, state, stats, StorylineEngine(pack, stats, **kw)


# ---------------------------------------------------------------------------
# 节点触发与进入
# ---------------------------------------------------------------------------


def test_n1_enters_on_first_begin_turn():
    pack, state, _, engine = _engine()
    node, msgs = engine.begin_turn(state)
    assert node is not None and node.id == "n1_first_meeting"
    assert state.current_node == "n1_first_meeting"
    assert state.scene == "长安城·沈府门前"
    assert state.present_npcs == ["shen_qingqiu"]
    assert msgs and "【主线节点】初遇" in msgs[0]["content"]
    assert not engine.choice_locked(state)  # N1 无关键选择


def test_completed_node_not_reentered():
    pack, state, _, engine = _engine()
    engine.begin_turn(state)
    state.flags["met_shen"] = True
    engine.end_turn(state)
    assert "n1_first_meeting" in state.completed_nodes
    node2, _ = engine.begin_turn(state)
    assert node2 is None  # N1 已完成、N2 条件未到 → 无节点进入


def test_n2_triggers_only_when_conditions_met():
    pack, state, _, engine = _engine()
    engine.begin_turn(state)
    # 只有 day 不满足 → 不触发
    state.flags["met_shen"] = True
    assert engine.begin_turn(state)[0] is None
    # day 满足 → 触发 N2
    state.day = 6
    node, msgs = engine.begin_turn(state)
    assert node is not None and node.id == "n2_poetry_festival"
    assert state.scene == "长安城·曲江池畔"


# ---------------------------------------------------------------------------
# 完成判定：代码复核，不采信自报
# ---------------------------------------------------------------------------


def test_completion_requires_flag_not_signal():
    pack, state, _, engine = _engine()
    engine.begin_turn(state)
    # LLM 谎报 node_complete，但 flag 未置位 → 不完成
    outcome = engine.end_turn(state, plot_signal="node_complete")
    assert outcome.node_completed is None
    assert state.current_node == "n1_first_meeting"


def test_completion_by_flag():
    pack, state, _, engine = _engine()
    engine.begin_turn(state)
    state.flags["met_shen"] = True
    outcome = engine.end_turn(state, plot_signal="normal")  # 即使自报 normal 也完成
    assert outcome.node_completed is not None
    assert outcome.node_completed.id == "n1_first_meeting"
    assert state.current_node is None
    assert state.scene == pack.world.start_scene  # 场景复位
    assert state.present_npcs == []
    assert "【节点完成】" in outcome.messages[0]["content"]


# ---------------------------------------------------------------------------
# 关键选择
# ---------------------------------------------------------------------------


def test_critical_choice_locks_input():
    pack, state, _, engine = _engine()
    engine.begin_turn(state)
    state.flags["met_shen"] = True
    state.day = 6
    engine.begin_turn(state)
    assert engine.choice_locked(state)
    choice = engine.pending_choice(state)
    assert choice.id == "join_poetry"
    assert len(choice.options) == 2


def test_choose_option_applies_effects_and_records():
    pack, state, _, engine = _engine()
    engine.begin_turn(state)
    state.flags["met_shen"] = True
    state.day = 6
    engine.begin_turn(state)
    msg = engine.choose_option(state, 0)
    assert state.flags["poetry_join"] is True
    assert len(state.choice_log) == 1
    rec = state.choice_log[0]
    assert rec.node_id == "n2_poetry_festival" and "欣然登台" in rec.text
    assert "欣然登台" in msg["content"]
    assert not engine.choice_locked(state)  # 选完解锁


def test_choose_option_invalid_index():
    pack, state, _, engine = _engine()
    engine.begin_turn(state)
    state.flags["met_shen"] = True
    state.day = 6
    engine.begin_turn(state)
    with pytest.raises(StorylineError, match="超出范围"):
        engine.choose_option(state, 99)


def test_choose_option_when_not_locked():
    pack, state, _, engine = _engine()
    engine.begin_turn(state)
    with pytest.raises(StorylineError, match="没有待选择"):
        engine.choose_option(state, 0)


# ---------------------------------------------------------------------------
# 卡壳保护
# ---------------------------------------------------------------------------


def test_stuck_protection_two_stages():
    pack, state, _, engine = _engine(stuck_threshold=3, forced_threshold=5)
    engine.begin_turn(state)
    for turn in range(1, 6):
        outcome = engine.end_turn(state)
        if turn == 3:
            assert any("【推进提示】" in m["content"] for m in outcome.messages)
        elif turn == 5:
            assert any("【命运事件】" in m["content"] for m in outcome.messages)
    # 各阶段只注入一次
    outcome = engine.end_turn(state)
    assert not any("【推进提示】" in m["content"] for m in outcome.messages)
    assert not any("【命运事件】" in m["content"] for m in outcome.messages)


def test_stuck_counter_reset_after_completion():
    pack, state, _, engine = _engine(stuck_threshold=3, forced_threshold=5)
    engine.begin_turn(state)
    for _ in range(4):
        engine.end_turn(state)
    assert state.stuck_stage == 1
    state.flags["met_shen"] = True
    engine.end_turn(state)
    assert state.node_turns == 0 and state.stuck_stage == 0


# ---------------------------------------------------------------------------
# 结局
# ---------------------------------------------------------------------------


def test_ending_together():
    pack, state, _, engine = _engine()
    state.day = 9
    state.affections["shen_qingqiu"] = 65.0
    state.flags["poetry_top3"] = True
    ending = engine.check_ending(state)
    assert ending is not None and ending.id == "ending_together"
    outcome = engine.end_turn(state)
    assert outcome.ending.id == "ending_together"


def test_ending_wanderer():
    pack, state, _, engine = _engine()
    state.day = 11
    state.affections["shen_qingqiu"] = 10.0
    state.flags["poetry_top3"] = False
    ending = engine.check_ending(state)
    assert ending is not None and ending.id == "ending_wanderer"


def test_no_ending_before_conditions():
    pack, state, _, engine = _engine()
    assert engine.check_ending(state) is None


# ---------------------------------------------------------------------------
# 选项过滤
# ---------------------------------------------------------------------------


def test_filter_choices_removes_forbidden_and_appends_free_input():
    pack, *_ = _engine()
    choices = ["上前相助", "掏出手机报警", "去找巡逻的官差"]
    filtered = filter_choices(pack, choices)
    assert all("手机" not in c for c in filtered)
    assert filtered[-1] == FREE_INPUT_OPTION
    assert filtered.count(FREE_INPUT_OPTION) == 1


def test_filter_choices_no_duplicate_free_input():
    pack, *_ = _engine()
    choices = ["甲", "乙", "丙", FREE_INPUT_OPTION]
    filtered = filter_choices(pack, choices)
    assert filtered.count(FREE_INPUT_OPTION) == 1

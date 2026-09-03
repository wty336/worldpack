"""W7 验收测试（离线）：游戏主循环串联、关键抉择门、事件级联、结局、存档。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game, GameError
from game_agent.llm import LLMClient, build_tools
from game_agent.save import load_game, save_game
from game_agent.schedule import ScheduleError
from game_agent.state import GameState
from game_agent.storyline import FREE_INPUT_OPTION
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

SUBMIT = tool_call(
    "s1",
    "submit_narration",
    {"narration": "测试叙事", "choices": ["行动一", "行动二", "行动三"], "plot_signal": "normal"},
)
SUBMIT2 = tool_call(
    "s2",
    "submit_narration",
    {"narration": "事件叙事", "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
)


def _game(responses, mutate=None, rng=None):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    if mutate is not None:
        mutate(state)
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm, rng=rng)


class _NeverRng:
    """恒不触发日程概率事件（random() 恒 0.9 > 0.3）。"""

    def random(self):
        return 0.9


def _n1_done(s: GameState) -> None:
    """预置：N1 已完成（跳过开场节点，直接测日常/结局逻辑）。"""
    s.completed_nodes.append("n1_first_meeting")
    s.flags["met_shen"] = True


def test_start_enters_n1_and_choices_filtered():
    """开场：进入 N1（有关键选择）→ 视图锁定为固定选项，并带剧情 briefing。"""
    pack, state, game = _game([])
    view = game.start()
    assert state.current_node == "n1_first_meeting"
    assert view.narration is None  # 关键选择待决，不调 LLM
    assert view.choice_prompt is not None and view.choice_prompt.id == "how_to_help"
    assert len(view.choices) == 3
    assert FREE_INPUT_OPTION not in view.choices  # 关键抉择只有固定选项
    assert view.briefing and "纨绔" in view.briefing  # 玩家可见的剧情背景（体验修复）


def test_pick_applies_effects_then_narrates():
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT]))])
    game.start()
    view = game.pick(0)
    assert state.flags["met_shen"] is True
    assert state.affections["shen_qingqiu"] == 8.0  # 5 + 3
    assert len(state.choice_log) == 1
    assert view.narration == "测试叙事"
    assert view.choices[-1] == FREE_INPUT_OPTION  # 日常选项恒带自由输入


def test_say_when_locked_raises():
    """关键抉择期间自由输入被拒（W5 验收项的引擎级落点）。"""
    pack, state, game = _game([])
    game.start()
    with pytest.raises(GameError, match="关键抉择"):
        game.say("我要自由发挥")


def test_n1_completes_after_pick_and_turn():
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT]))])
    game.start()
    game.pick(0)
    assert state.current_node is None
    assert "n1_first_meeting" in state.completed_nodes


def test_act_applies_effects_and_narrates():
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT]))], mutate=_n1_done, rng=_NeverRng()
    )
    view = game.act("cultivate")
    assert state.stats["martial"] == 8.0  # 5 + 3（后山奇遇未触发）
    assert state.action_points_left == 0
    assert view.narration == "测试叙事"


def test_act_without_points_raises():
    pack, state, game = _game([])
    game.act("cultivate")  # 消耗唯一行动点（N1 关键选择待决，不消耗 LLM 响应）
    with pytest.raises(ScheduleError):
        game.act("work")  # 点数不足：执行前即拒绝


def test_end_day_advances():
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT]))])
    game.act("cultivate")
    text = game.end_day()
    assert "第 2 天" in text
    assert state.action_points_left == 1


def test_n2_trigger_and_choice_flow():
    """N1 完成后 day≥5 触发 N2：关键选择锁定 → 选择 → flag 写入。"""
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT]))],  # N2 进入前的某次叙事
        mutate=lambda s: (s.completed_nodes.append("n1_first_meeting"),
                          setattr(s, "day", 6),
                          s.flags.__setitem__("met_shen", True)),
    )
    view = game.say("去曲江池逛逛")
    assert state.current_node == "n2_poetry_festival"
    assert view.choice_prompt is not None and view.choice_prompt.id == "poetry_choice"
    with pytest.raises(GameError):
        game.say("自由输入被拒")


def test_ending_reached_in_turn():
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT]))],
        mutate=lambda s: (
            setattr(s, "day", 11),
            _n1_done(s),
            # N2 也已完成（否则 day≥5 且 met_shen 会触发 N2 关键选择，门在结局判定之前）
            s.completed_nodes.append("n2_poetry_festival"),
            s.flags.__setitem__("poetry_top3", True),
        ),
    )
    view = game.say("再逛逛")
    assert view.ending is not None and view.ending.id == "ending_wanderer"


def test_condition_event_chains_extra_narration():
    """好感 ≥30 触发月下谈心：同一回合级联第二段叙事。"""
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT])), resp(msg(tool_calls=[SUBMIT2]))],
        mutate=lambda s: (_n1_done(s), s.affections.__setitem__("shen_qingqiu", 30.0)),
    )
    view = game.say("夜深了")
    assert "测试叙事" in view.narration and "事件叙事" in view.narration
    assert state.affections["shen_qingqiu"] == 35.0  # 事件效果 +5
    assert "ev_moon_chat" in state.triggered_events


def test_save_load_roundtrip(tmp_path: Path):
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT]))])
    game.start()
    game.pick(0)
    path = tmp_path / "save.json"
    save_game(state, path)
    loaded = load_game(path)
    assert loaded == state


# ---------------------------------------------------------------------------
# W-B / W-C：时间触发事件 + 节点完成自动存档
# ---------------------------------------------------------------------------


def test_time_event_triggers_on_end_day():
    from game_agent.worldpack import EventSpec, EventTrigger

    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT]))], mutate=_n1_done, rng=_NeverRng()
    )
    ev = EventSpec(
        id="e_day2",
        title="第2天信使",
        trigger=EventTrigger(kind="time", when={"day": {"gte": 2}}),
        priority="normal",
        effects={"stats": {"silver": 5}},
        once=True,
        script="有信使送来一封书信。",
    )
    pack.events.events.append(ev)
    game.end_day()  # 第 1 → 2 天，时间事件触发
    assert state.stats["silver"] == 55.0
    assert "e_day2" in state.triggered_events
    # 事件消息已入历史，下一回合叙事可见
    assert any("【事件】第2天信使" in m["content"] for m in game.history)


def test_autosave_on_node_completion(tmp_path: Path):
    """W-C：主线节点完成 → 自动存档到 autosave 路径。"""
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT]))])
    game.autosave_path = tmp_path / "auto.json"
    game.start()
    game.pick(0)  # N1 完成 → 触发自动存档
    assert (tmp_path / "auto.json").exists()
    loaded = load_game(tmp_path / "auto.json")
    assert loaded == state
    assert loaded.completed_nodes == ["n1_first_meeting"]

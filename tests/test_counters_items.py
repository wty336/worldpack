"""批次 E 守卫测试：机制层表达力——counters / items。

- 真值：state.counters/items 初始化（含 initial 物品）、饱和边界、审计入 stat_log；
- DSL：{counter: {x: {gte: 3}}} 与 {item: {id: true/false}} 的求值与结构校验；
- 展示：状态栏 / query_world / 事实图接地；
- 交叉校验：未声明引用拒绝；completion 引用计数器/物品的可达性（复用 flag 模式）；
- 验收场景（plan §5）：
  ① counters 写"三次赠礼触发支线"——事件 when: {counter: {flower_gifts: {gte: 3}}}；
  ② items 写"当剑后不可再修炼剑法"——行动 requires: {item: {sword: false}}。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.conditions import ConditionError, evaluate, validate_condition
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.stats import StatChangeError, StatsSystem
from game_agent.worldpack import (
    ActionEffects,
    CounterSpec,
    ItemSpec,
    ScheduleSpec,
    WorldPackError,
    _cross_check,
    load_worldpack,
)

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _submit(narration: str, call_id: str = "s1"):
    return tool_call(
        call_id,
        "submit_narration",
        {"narration": narration, "choices": ["一", "二", "三"], "plot_signal": "normal"},
    )


def _n1_done(s: GameState) -> None:
    s.completed_nodes.append("n1_first_meeting")
    s.flags["met_shen"] = True


# ---------------------------------------------------------------------------
# DSL：求值 + 结构校验
# ---------------------------------------------------------------------------


def _state_with_counters(counters=None, items=None):
    state = GameState(pack_name="测试")
    state.counters = counters or {}
    state.items = items or []
    return state


def test_counter_condition_evaluates():
    state = _state_with_counters(counters={"flower_gifts": 3})
    assert evaluate({"counter": {"flower_gifts": {"gte": 3}}}, state) is True
    assert evaluate({"counter": {"flower_gifts": {"gte": 4}}}, state) is False
    assert evaluate({"counter": {"flower_gifts": {"lt": 4}}}, state) is True


def test_counter_condition_undeclared_raises():
    with pytest.raises(ConditionError, match="未声明的计数器"):
        evaluate({"counter": {"nope": {"gte": 1}}}, _state_with_counters())


def test_item_condition_evaluates():
    state = _state_with_counters(items=["sword"])
    assert evaluate({"item": {"sword": True}}, state) is True
    assert evaluate({"item": {"sword": False}}, state) is False
    assert evaluate({"item": {"sword": True, "jade": True}}, state) is False  # jade 未持有


def test_counter_item_condition_validation():
    validate_condition({"counter": {"x": {"gte": 1}}}, "t")
    validate_condition({"item": {"x": True}}, "t")
    with pytest.raises(ConditionError, match="counter"):
        validate_condition({"counter": {"x": {"like": 1}}}, "t")
    with pytest.raises(ConditionError, match="item"):
        validate_condition({"item": {"x": "yes"}}, "t")
    with pytest.raises(ConditionError, match="未知条件键"):
        validate_condition({"counters": {"x": 1}}, "t")  # 条件用单数 counter


# ---------------------------------------------------------------------------
# 效果结算（stats.apply_effects）
# ---------------------------------------------------------------------------


def _stats_with_counters():
    pack = _pack()
    schedule = pack.schedule.model_copy(
        update={
            "counters": {
                "flower_gifts": CounterSpec(label="赠礼次数", initial=0, max=10),
            },
            "items": [ItemSpec(id="sword", label="听雨剑"), ItemSpec(id="jade", label="玉佩", initial=True)],
        }
    )
    state = GameState(pack_name="测试")
    state.stats = {"charm": 10.0}
    state.affections = {"shen_qingqiu": 5.0}
    state.counters = {k: float(v.initial) for k, v in schedule.counters.items()}
    state.items = [i.id for i in schedule.items if i.initial]
    return StatsSystem(schedule), state, schedule


def test_counter_effect_increments_and_audits():
    stats, state, schedule = _stats_with_counters()
    notes = stats.apply_effects(state, {"counters": {"flower_gifts": 1}})
    assert state.counters["flower_gifts"] == 1
    assert any("赠礼次数 +1" in n for n in notes)
    # 审计：stat_log 记录 counter:<名>，不变量 after == before + delta
    rec = state.stat_log[-1]
    assert rec.stat == "counter:flower_gifts"
    assert rec.before == 0 and rec.after == 1 and rec.delta == 1


def test_counter_effect_saturates_at_bounds():
    stats, state, _ = _stats_with_counters()
    stats.apply_effects(state, {"counters": {"flower_gifts": 99}})  # max=10 → 饱和
    assert state.counters["flower_gifts"] == 10
    stats.apply_effects(state, {"counters": {"flower_gifts": -1}})  # 10→9 合法
    assert state.counters["flower_gifts"] == 9
    stats.apply_effects(state, {"counters": {"flower_gifts": -99}})  # min=0 → 饱和
    assert state.counters["flower_gifts"] == 0
    stats.apply_effects(state, {"counters": {"flower_gifts": -5}})  # 已在边界 → 无变化
    assert state.counters["flower_gifts"] == 0


def test_counter_effect_undeclared_rejects():
    stats, state, _ = _stats_with_counters()
    with pytest.raises(StatChangeError, match="未声明的计数器"):
        stats.apply_effects(state, {"counters": {"nope": 1}})


def test_item_gain_lose_and_no_duplicates():
    stats, state, schedule = _stats_with_counters()
    notes = stats.apply_effects(state, {"items": {"gain": ["sword"], "lose": ["jade"]}})
    assert "sword" in state.items and "jade" not in state.items
    assert any("获得物品：听雨剑" in n for n in notes)
    assert any("失去物品：玉佩" in n for n in notes)
    # 重复获得幂等；失去未持有幂等
    stats.apply_effects(state, {"items": {"gain": ["sword"], "lose": ["jade"]}})
    assert state.items.count("sword") == 1


def test_item_effect_undeclared_rejects():
    stats, state, _ = _stats_with_counters()
    with pytest.raises(StatChangeError, match="未声明的物品"):
        stats.apply_effects(state, {"items": {"gain": ["nope"]}})


# ---------------------------------------------------------------------------
# 展示：状态栏 / query_world / 事实图
# ---------------------------------------------------------------------------


def test_status_bar_shows_counters_and_items():
    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={
            "counters": {"flower_gifts": CounterSpec(label="赠礼次数", initial=2)},
            "items": [ItemSpec(id="sword", label="听雨剑", initial=True)],
        }
    )
    state = GameState.from_pack(pack)
    state.present_npcs = ["shen_qingqiu"]
    from game_agent.context import ContextBuilder

    text = ContextBuilder.from_pack(pack).status_text(state, None)
    assert "赠礼次数 2" in text
    assert "持有：听雨剑" in text


def test_factgraph_grounds_counters_and_items():
    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={
            "counters": {"flower_gifts": CounterSpec(label="赠礼次数", initial=3)},
            "items": [ItemSpec(id="sword", label="听雨剑", initial=True)],
        }
    )
    state = GameState.from_pack(pack)
    from game_agent.factgraph import build_graph

    graph = build_graph(pack, state)
    assert graph.has("赠礼次数") and graph.has("听雨剑")


# ---------------------------------------------------------------------------
# 交叉校验
# ---------------------------------------------------------------------------


def _pack_parts(schedule):
    pack = _pack()
    return {
        "world": pack.world,
        "schedule": schedule,
        "mainline": pack.mainline,
        "events": pack.events,
        "endings": pack.endings,
        "npcs": pack.npcs,
    }


def _schedule_with_counters(counters=None, items=None):
    return _pack().schedule.model_copy(
        update={"counters": counters or {}, "items": items or []}
    )


def test_cross_check_undeclared_counter_condition_rejected():
    from game_agent.worldpack import EventSpec, EventTrigger, EventsSpec

    schedule = _schedule_with_counters()
    events = EventsSpec(
        events=[
            EventSpec(
                id="ev_bad", title="坏事件",
                trigger=EventTrigger(kind="condition", when={"counter": {"nope": {"gte": 1}}}),
            )
        ]
    )
    parts = _pack_parts(schedule)
    parts["events"] = events
    with pytest.raises(WorldPackError, match="未声明的计数器"):
        _cross_check(parts)


def test_cross_check_undeclared_item_condition_rejected():
    from game_agent.worldpack import EventSpec, EventTrigger, EventsSpec

    schedule = _schedule_with_counters()
    events = EventsSpec(
        events=[
            EventSpec(
                id="ev_bad", title="坏事件",
                trigger=EventTrigger(kind="condition", when={"item": {"nope": True}}),
            )
        ]
    )
    parts = _pack_parts(schedule)
    parts["events"] = events
    with pytest.raises(WorldPackError, match="未声明的物品"):
        _cross_check(parts)


def test_cross_check_completion_counter_unreachable_rejected():
    """completion 要求计数器 ≥N，但没有任何效果路径增减它 → 加载拒绝。"""
    from game_agent.worldpack import MainlineSpec, NodeSpec

    schedule = _schedule_with_counters(
        counters={"flower_gifts": CounterSpec(label="赠礼次数", initial=0)}
    )
    parts = _pack_parts(schedule)
    node = parts["mainline"].nodes[0].model_dump()
    node["completion"] = {"counter": {"flower_gifts": {"gte": 3}}}
    parts["mainline"] = MainlineSpec(nodes=[NodeSpec(**node)])
    with pytest.raises(WorldPackError, match="计数器"):
        _cross_check(parts)


def test_cross_check_completion_counter_reachable_accepted():
    """事件效果能增减计数器 → completion 可达，校验通过。"""
    from game_agent.worldpack import EventSpec, EventTrigger, EventsSpec, MainlineSpec, NodeSpec

    schedule = _schedule_with_counters(
        counters={"flower_gifts": CounterSpec(label="赠礼次数", initial=0)}
    )
    parts = _pack_parts(schedule)
    node = parts["mainline"].nodes[0].model_dump()
    node["completion"] = {"counter": {"flower_gifts": {"gte": 3}}}
    parts["mainline"] = MainlineSpec(nodes=[NodeSpec(**node)])
    ev = parts["events"].events[0].model_dump()
    ev["effects"] = {"counters": {"flower_gifts": 1}}
    parts["events"] = EventsSpec(
        events=[EventSpec(**ev)] + parts["events"].events[1:]
    )
    _cross_check(parts)  # 不抛即通过


def test_cross_check_completion_item_unreachable_rejected():
    from game_agent.worldpack import MainlineSpec, NodeSpec

    schedule = _schedule_with_counters(items=[ItemSpec(id="jade", label="玉佩")])
    parts = _pack_parts(schedule)
    node = parts["mainline"].nodes[0].model_dump()
    node["completion"] = {"item": {"jade": True}}  # 无获得路径
    parts["mainline"] = MainlineSpec(nodes=[NodeSpec(**node)])
    with pytest.raises(WorldPackError, match="物品"):
        _cross_check(parts)


def test_cross_check_duplicate_counter_bounds_rejected():
    schedule = _schedule_with_counters(
        counters={"bad": CounterSpec(label="x", initial=5, min=0, max=1)}
    )
    with pytest.raises(WorldPackError, match="initial"):
        _cross_check(_pack_parts(schedule))


# ---------------------------------------------------------------------------
# 验收场景 ①：counters 写"三次赠礼触发支线"（事件 when 引用 counter）
# ---------------------------------------------------------------------------


def test_acceptance_three_gifts_trigger_event():
    from game_agent.events import EventSystem
    from game_agent.worldpack import EventSpec, EventTrigger, EventsSpec

    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={"counters": {"flower_gifts": CounterSpec(label="赠礼次数", initial=0)}}
    )
    pack.events = EventsSpec(
        events=[
            *pack.events.events,
            EventSpec(
                id="ev_gift_sidequest", title="赠礼情缘",
                trigger=EventTrigger(kind="condition", when={"counter": {"flower_gifts": {"gte": 3}}}),
                priority="normal",
                script="她接过第三束花，终于开口邀你同游。",
                effects={"affections": {"shen_qingqiu": 3}},
                once=True,
            ),
        ]
    )
    state = GameState.from_pack(pack)
    state.flags["met_shen"] = True
    rng = type("R", (), {"random": staticmethod(lambda: 0.9), "uniform": staticmethod(lambda a, b: a)})()
    events = EventSystem(pack, StatsSystem(pack.schedule), rng)

    assert events.check_condition_events(state) is None  # 未达 3 次
    state.counters["flower_gifts"] = 3.0
    ev = events.check_condition_events(state)
    assert ev is not None and ev.id == "ev_gift_sidequest"
    msg_ = events.trigger(state, ev)
    assert state.affections["shen_qingqiu"] == 8.0  # 5 + 3
    assert state.triggered_events[-1] == "ev_gift_sidequest"
    assert "赠礼情缘" in msg_["content"]


# ---------------------------------------------------------------------------
# 验收场景 ②：items 写"当剑后不可再修炼剑法"（行动 requires 引用 item）
# ---------------------------------------------------------------------------


def test_acceptance_pawned_sword_gates_action():
    from game_agent.schedule import ScheduleError, ScheduleSystem
    from game_agent.worldpack import ActionSpec

    pack = _pack()
    # 修炼行动加门槛：必须仍持有听雨剑
    actions = []
    for action in pack.schedule.actions:
        dumped = action.model_dump()
        if action.id == "cultivate":
            dumped["requires"] = {"item": {"sword": True}}
        actions.append(ActionSpec(**dumped))
    pack.schedule = pack.schedule.model_copy(
        update={
            "actions": actions,
            "items": [ItemSpec(id="sword", label="听雨剑", initial=True)],
        }
    )
    state = GameState.from_pack(pack)
    state.action_points_left = 2
    schedule = ScheduleSystem(pack, StatsSystem(pack.schedule), None)
    # 持剑：可修炼
    assert schedule.action_available(state, schedule.action_by_id("cultivate"))
    # 当剑（效果路径失去物品）后：门槛不满足 → 行动不可选 + 执行兜底拒绝
    StatsSystem(pack.schedule).apply_effects(state, {"items": {"lose": ["sword"]}})
    assert not schedule.action_available(state, schedule.action_by_id("cultivate"))
    with pytest.raises(ScheduleError, match="条件"):
        schedule.execute_action(state, "cultivate")


# ---------------------------------------------------------------------------
# 存档回环 + Game 级效果路径
# ---------------------------------------------------------------------------


def test_counters_items_save_roundtrip():
    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={
            "counters": {"flower_gifts": CounterSpec(label="赠礼次数", initial=1)},
            "items": [ItemSpec(id="sword", label="听雨剑", initial=True)],
        }
    )
    state = GameState.from_pack(pack)
    restored = GameState.from_dict(state.to_dict())
    assert restored.counters == {"flower_gifts": 1.0}
    assert restored.items == ["sword"]


def test_game_end_turn_applies_counter_effect_from_choice():
    """Game 级：关键选择效果带 counters/items → 引擎结算（Game 不崩、真值更新）。"""
    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={
            "counters": {"flower_gifts": CounterSpec(label="赠礼次数", initial=0)},
            "items": [ItemSpec(id="jade", label="祖传玉佩", initial=True)],
        }
    )
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[_submit("抉择完成。")]))]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    game.start()
    # 给第一个关键选项的效果追加 counters/items
    node = pack.mainline.nodes[0]
    opt = node.critical_choices[0].options[0]
    opt.effects["counters"] = {"flower_gifts": 1}
    opt.effects["items"] = {"lose": ["jade"]}
    game.pick(0)
    assert game.state.counters["flower_gifts"] == 1
    assert "jade" not in game.state.items

"""审查修复守卫（docs/plan-design-hardening.md §12）：子代理全量审查发现的逐项回归钉。

- C-1：审计回放识别 counter: 前缀 + 计数器三方对账；
- M-1a：事件效果 / 关键选择选项效果的 counters/items 引用加载期校验；
- M-1b：行动 requires 的 item 引用加载期校验；
- M-2：MCP 畸形参数 → isError 结果，不杀 serve 循环；通知形态 initialize/ping 不回包；
- Minor1：级联轮熔断 → 汇总文案不进反重复窗口/last_narration；
- Minor2：MCP actions 空列表有兜底文案；
- Minor3：自定义工具效果拒绝时不扣行动点；
- Minor4：重规划回落作者手写 steps；
- Minor6：find_turn_cut 回合起点按 A-2 口径（无 name 标记的 user）；
- Minor7：老档 + 新增 counters 的包 → Game 装配时补初始值。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.audit import audit_stats
from game_agent.compression import find_turn_cut
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.mcp_server import build_player_registry, handle_request
from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.worldpack import (
    ActionSpec,
    CounterSpec,
    CustomToolSpec,
    ItemSpec,
    WorldPackError,
    _cross_check,
    load_worldpack,
)

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _pack_parts(schedule, events=None, mainline=None):
    pack = _pack()
    return {
        "world": pack.world,
        "schedule": schedule,
        "mainline": mainline or pack.mainline,
        "events": events or pack.events,
        "endings": pack.endings,
        "npcs": pack.npcs,
    }


# ---------------------------------------------------------------------------
# C-1：审计 × counters
# ---------------------------------------------------------------------------


def test_audit_replays_counter_records():
    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={"counters": {"gifts": CounterSpec(label="赠礼", initial=0, max=99)}}
    )
    state = GameState.from_pack(pack)
    stats = StatsSystem(pack.schedule)
    stats.apply_effects(state, {"counters": {"gifts": 3}})
    stats.apply_effects(state, {"counters": {"gifts": 2}})
    assert audit_stats(pack, state) == []  # 回放对账零偏差
    assert state.counters["gifts"] == 5


def test_audit_detects_counter_drift():
    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={"counters": {"gifts": CounterSpec(label="赠礼", initial=0, max=99)}}
    )
    state = GameState.from_pack(pack)
    StatsSystem(pack.schedule).apply_effects(state, {"counters": {"gifts": 3}})
    state.counters["gifts"] = 99.0  # 绕过数值系统的篡改
    deviations = audit_stats(pack, state)
    assert any("计数器零偏差失败" in d for d in deviations)


# ---------------------------------------------------------------------------
# M-1：交叉校验补齐
# ---------------------------------------------------------------------------


def test_cross_check_rejects_undeclared_counter_in_event_effects():
    from game_agent.worldpack import EventSpec, EventTrigger, EventsSpec

    schedule = _pack().schedule
    events = EventsSpec(
        events=[
            EventSpec(
                id="ev_c", title="x",
                trigger=EventTrigger(kind="condition", when={"all": []}),
                effects={"counters": {"undeclared": 1}},
            )
        ]
    )
    with pytest.raises(WorldPackError, match="未声明的计数器"):
        _cross_check(_pack_parts(schedule, events=events))


def test_cross_check_rejects_undeclared_item_in_event_effects():
    from game_agent.worldpack import EventSpec, EventTrigger, EventsSpec

    schedule = _pack().schedule
    events = EventsSpec(
        events=[
            EventSpec(
                id="ev_i", title="x",
                trigger=EventTrigger(kind="condition", when={"all": []}),
                effects={"items": {"gain": ["undeclared_item"]}},
            )
        ]
    )
    with pytest.raises(WorldPackError, match="未声明的物品"):
        _cross_check(_pack_parts(schedule, events=events))


def test_cross_check_rejects_undeclared_counter_in_choice_effects():
    from game_agent.worldpack import MainlineSpec, NodeSpec

    schedule = _pack().schedule
    parts = _pack_parts(schedule)
    node = parts["mainline"].nodes[0].model_dump()
    node["critical_choices"][0]["options"][0]["effects"]["counters"] = {"undeclared": 1}
    parts["mainline"] = MainlineSpec(nodes=[NodeSpec(**node)])
    with pytest.raises(WorldPackError, match="未声明的计数器"):
        _cross_check(parts)


def test_cross_check_rejects_undeclared_item_in_action_requires():
    schedule = _pack().schedule
    actions = []
    for action in schedule.actions:
        dumped = action.model_dump()
        if action.id == "cultivate":
            dumped["requires"] = {"item": {"typo_item": True}}
        actions.append(ActionSpec(**dumped))
    bad = schedule.model_copy(update={"actions": actions})
    with pytest.raises(WorldPackError, match="requires 引用了未声明的物品"):
        _cross_check(_pack_parts(bad))


# ---------------------------------------------------------------------------
# M-2 + Minor 8：MCP 边界
# ---------------------------------------------------------------------------


def _game():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.completed_nodes.append("n1_first_meeting")
    state.flags["met_shen"] = True
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    return pack, Game(pack, state, llm)


def test_mcp_malformed_args_return_is_error_not_raise():
    """畸形参数（int(dict) 抛 TypeError）→ isError 结果，serve 循环不死。"""
    _, game = _game()
    reg = build_player_registry(game)
    result = handle_request(
        reg, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "pick", "arguments": {"index": {"a": 1}}}}
    )["result"]
    assert result["isError"] is True
    assert "内部错误" in result["content"][0]["text"]


def test_mcp_notification_initialize_and_ping_no_reply():
    _, game = _game()
    reg = build_player_registry(game)
    assert handle_request(reg, {"jsonrpc": "2.0", "method": "initialize", "params": {}}) is None
    assert handle_request(reg, {"jsonrpc": "2.0", "method": "ping"}) is None


def test_mcp_actions_empty_has_fallback_text():
    _, game = _game()
    game.state.action_points_left = 0
    reg = build_player_registry(game)
    result = handle_request(
        reg, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "actions"}}
    )["result"]
    assert "无可用行动" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Minor 1：级联熔断不进反重复窗口
# ---------------------------------------------------------------------------


def test_cascade_meltdown_keeps_window_clean():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.completed_nodes.append("n1_first_meeting")
    state.flags["met_shen"] = True
    llm = LLMClient(FakeClient([
        resp(msg(content="a")), resp(msg(content="b")), resp(msg(content="c")),  # 主回合熔断
        resp(msg(tool_calls=[tool_call(
            "s1", "submit_narration",
            {"narration": "级联轮正常叙事。", "choices": ["一", "二", "三"], "plot_signal": "normal"},
        )])),
    ]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)

    ev = SimpleNamespace(id="ev_x", title="T", script="S", effects={}, priority="normal", once=True)
    calls = {"n": 0}

    def fake_condition_check(_state):
        calls["n"] += 1
        return ev if calls["n"] == 1 else None  # 熔断后级联一次

    game.events.check_condition_events = fake_condition_check
    view = game.say("触发级联")
    assert "生成失败" in view.narration and "级联轮正常叙事。" in view.narration
    # 熔断兜底文案不进 last_narration / 窗口（本轮整体跳过反重复记账）
    assert game.last_narration == ""
    assert game._narration_window == []
    assert any("[引擎熔断]" in (m.get("content") or "") for m in game.history)


# ---------------------------------------------------------------------------
# Minor 3 / 4 / 6 / 7
# ---------------------------------------------------------------------------


def test_custom_tool_no_ap_cost_when_effects_rejected():
    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={
            "tools": [
                # 未声明属性（绕过交叉校验的内存构造）→ 运行期 StatChangeError
                CustomToolSpec(id="wp_bad", label="坏工具", description="x",
                               cost=1, effects={"stats": {"nope": 5}}),
            ]
        }
    )
    state = GameState.from_pack(pack)
    state.action_points_left = 2
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    result = game.registry.dispatch("wp_bad", {})
    assert result.status == "rejected"
    assert state.action_points_left == 2  # 效果拒绝 → 不白扣


def test_replan_restores_authored_steps():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.current_node = "n1_first_meeting"
    state.node_plan = ["早已过时的计划"]
    node = pack.mainline.nodes[0].model_dump()
    node["steps"] = ["手写步骤一", "手写步骤二"]
    from game_agent.worldpack import NodeSpec

    pack.mainline = type(pack.mainline)(nodes=[NodeSpec(**node), *pack.mainline.nodes[1:]])
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))  # 空 = 不该有调用
    game = Game(pack, state, llm)
    game.plan_node = True
    game._maybe_replan([{"content": "【推进提示】推进"}])
    assert state.node_plan == ["手写步骤一", "手写步骤二"]
    assert state.node_plan_step == 0


def test_find_turn_cut_ignores_bracketed_engine_messages():
    history = [
        {"role": "user", "content": "玩家输入1"},
        {"role": "assistant", "content": "叙事1"},
        {"role": "user", "name": "engine", "content": "[反重复提示] 本轮与更早重复"},  # 引擎元消息
        {"role": "user", "content": "玩家输入2"},
        {"role": "assistant", "content": "叙事2"},
        {"role": "user", "content": "玩家输入3"},
    ]
    cut = find_turn_cut(history, keep_turns=2)
    assert history[cut]["content"] == "玩家输入2"  # [反重复提示] 不算回合起点


def test_game_backfills_counters_for_legacy_saves():
    pack = _pack()
    pack.schedule = pack.schedule.model_copy(
        update={"counters": {"gifts": CounterSpec(label="赠礼", initial=4)}}
    )
    legacy = {**GameState.from_pack(_pack()).to_dict()}
    legacy.pop("counters", None)
    state = GameState.from_dict(legacy)  # 老档：无 counters 字段
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    Game(pack, state, llm)
    assert state.counters == {"gifts": 4.0}

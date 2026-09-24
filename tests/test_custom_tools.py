"""批次 C 守卫测试：世界包自定义效果型工具（schedule.yaml 的 tools: 段）。

- Game 装配：schema 注册 + handler 绑定 + LLM 调用 → 效果结算进真值；
- 门槛三拒绝：once 已用 / requires 不满足 / 行动点不足（结构化 [引擎拒绝]）；
- cost 扣行动点；once 记入 state.used_custom_tools（存档回环）；
- 加载期交叉校验：引擎重名 / 效果引用未声明属性 / completion 可达性纳入。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import (
    CustomToolSpec,
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


def _game_with_tools(responses, tools, mutate=None, action_points=2, flags=None):
    pack = _pack()
    update: dict = {"tools": tools}
    if flags:
        update["flags"] = {**pack.schedule.flags, **flags}
    pack.schedule = pack.schedule.model_copy(update=update)
    state = GameState.from_pack(pack)
    state.action_points_left = action_points
    if mutate is not None:
        mutate(state)
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm)


def _n1_done(s: GameState) -> None:
    s.completed_nodes.append("n1_first_meeting")
    s.flags["met_shen"] = True


TEST_TOOL = CustomToolSpec(
    id="wp_breakthrough",
    label="顿悟突破",
    description="剧情推进到关键处时的突破动作",
    effects={"stats": {"martial": 5}, "flags": {"breakthrough_done": True}},
)


# ---------------------------------------------------------------------------
# Game 装配与执行
# ---------------------------------------------------------------------------


def test_custom_tool_executes_and_applies_effects():
    """LLM 调用自定义工具 → 效果结算（属性+flag）→ 结果文本回传 → 叙事照常。"""
    call = tool_call(
        "t1", "wp_breakthrough", {},
    )
    pack, state, game = _game_with_tools(
        [resp(msg(tool_calls=[call, _submit("你顿悟了。")]))],
        [TEST_TOOL],
        mutate=_n1_done,
        flags={"breakthrough_done": False},  # 效果引用的 flag 需声明（加载期交叉校验管）
    )
    assert state.flags.get("breakthrough_done") in (None, False)
    before_martial = state.stats["martial"]
    view = game.say("尝试突破")
    assert view.narration == "你顿悟了。"
    assert state.stats["martial"] == before_martial + 5
    assert state.flags["breakthrough_done"] is True
    # 工具结果文本进了历史（模型可引用）
    assert any("顿悟突破" in (m.get("content") or "") for m in game.history if m.get("role") == "tool")


def test_custom_tool_schema_sent_to_api():
    """自定义工具 schema 出现在 API 请求的 tools= 里（模型可见才可调用）。"""
    pack, state, game = _game_with_tools(
        [resp(msg(tool_calls=[_submit("x")]))], [TEST_TOOL], mutate=_n1_done
    )
    game.say("任意")
    sent = game.llm._client.chat.completions.calls[0]["tools"]
    names = [t["function"]["name"] for t in sent]
    assert "wp_breakthrough" in names
    schema = next(t for t in sent if t["function"]["name"] == "wp_breakthrough")
    assert "顿悟突破" in schema["function"]["description"]


def test_custom_tool_requires_gate_rejects():
    """requires 不满足 → 结构化拒绝，效果不落。"""
    gated = CustomToolSpec(
        id="wp_gated", label="秘传武学", description="需要结识",
        requires={"flags": {"met_shen": True}}, effects={"stats": {"martial": 5}},
    )
    call = tool_call("t1", "wp_gated", {})
    pack, state, game = _game_with_tools(
        [resp(msg(tool_calls=[call, _submit("未习得。")]))],
        [gated],
        mutate=_n1_done,
    )
    state.flags["met_shen"] = False  # 门槛不满足
    before = state.stats["martial"]
    game.say("求教")
    assert state.stats["martial"] == before  # 效果未落
    tool_msgs = [m for m in game.history if m.get("role") == "tool"]
    assert any("引擎拒绝" in (m.get("content") or "") for m in tool_msgs)


def test_custom_tool_once_enforced():
    """once 工具整局一次：第二次调用被拒；存档回环保留已用记录。"""
    once_tool = CustomToolSpec(
        id="wp_once", label="开锁", description="一次性",
        effects={}, once=True,
    )
    pack, state, game = _game_with_tools(
        [
            resp(msg(tool_calls=[tool_call("t1", "wp_once", {}), _submit("开了。", "s1")])),
            resp(msg(tool_calls=[tool_call("t2", "wp_once", {}), _submit("锁芯已毁。", "s2")])),
        ],
        [once_tool],
        mutate=_n1_done,
    )
    game.say("第一次")
    assert state.used_custom_tools == ["wp_once"]
    game.say("第二次")
    tool_msgs = [m for m in game.history if m.get("role") == "tool"]
    assert any("已经用过" in (m.get("content") or "") for m in tool_msgs)
    # 存档回环
    assert GameState.from_dict(state.to_dict()).used_custom_tools == ["wp_once"]


def test_custom_tool_cost_and_action_points():
    """cost 扣行动点；不足时拒绝且不落效果。"""
    costly = CustomToolSpec(
        id="wp_costly", label="重金打点", description="费钱又费力",
        cost=1, effects={"stats": {"charm": 2}},
    )
    call = tool_call("t1", "wp_costly", {})
    pack, state, game = _game_with_tools(
        [resp(msg(tool_calls=[call, _submit("打点完毕。")]))],
        [costly],
        mutate=_n1_done,
        action_points=0,  # 行动点耗尽
    )
    before = state.stats["charm"]
    game.say("打点")
    assert state.stats["charm"] == before
    assert any("行动点不足" in (m.get("content") or "") for m in game.history if m.get("role") == "tool")

    # 有行动点时正常扣减
    pack2, state2, game2 = _game_with_tools(
        [resp(msg(tool_calls=[tool_call("t2", "wp_costly", {}), _submit("好了。")]))],
        [costly],
        mutate=_n1_done,
        action_points=2,
    )
    game2.say("打点")
    assert state2.action_points_left == 1


# ---------------------------------------------------------------------------
# 加载期交叉校验（_cross_check 内存构造，无需 fixture 世界包）
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


def test_cross_check_rejects_engine_name_collision():
    schedule = _pack().schedule.model_copy(
        update={"tools": [CustomToolSpec(id="change_stat", label="x", description="y")]}
    )
    with pytest.raises(WorldPackError, match="重名"):
        _cross_check(_pack_parts(schedule))


def test_cross_check_rejects_unknown_stat_ref():
    schedule = _pack().schedule.model_copy(
        update={
            "tools": [
                CustomToolSpec(
                    id="wp_bad", label="x", description="y",
                    effects={"stats": {"nope": 1}},
                )
            ]
        }
    )
    with pytest.raises(WorldPackError, match="未声明的属性"):
        _cross_check(_pack_parts(schedule))


def test_cross_check_rejects_unknown_flag_ref():
    schedule = _pack().schedule.model_copy(
        update={
            "tools": [
                CustomToolSpec(
                    id="wp_bad", label="x", description="y",
                    effects={"flags": {"never_declared": True}},
                )
            ]
        }
    )
    with pytest.raises(WorldPackError, match="未声明的 flag"):
        _cross_check(_pack_parts(schedule))


def test_cross_check_custom_tool_writes_completion_flag():
    """自定义工具效果纳入 completion 可达性：此前不可达的节点经工具路径可达。"""
    from game_agent.conditions import evaluate  # noqa: F401 — 确认条件模块可用

    schedule = _pack().schedule.model_copy(
        update={
            "flags": {**_pack().schedule.flags, "breakthrough_done": False},
            "tools": [TEST_TOOL],
        }
    )
    # 不抛异常即通过（breakthrough_done 由 wp_breakthrough 的效果路径可写）
    _cross_check(_pack_parts(schedule))


def test_cross_check_rejects_required_param_not_declared():
    schedule = _pack().schedule.model_copy(
        update={
            "tools": [
                CustomToolSpec(
                    id="wp_param", label="x", description="y",
                    parameters={"target": {"type": "string"}},
                    required=["missing"],
                )
            ]
        }
    )
    with pytest.raises(WorldPackError, match="required"):
        _cross_check(_pack_parts(schedule))

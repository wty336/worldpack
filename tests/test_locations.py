"""批次 D 守卫测试：地点一等公民（world.yaml 的 locations 表 + change_scene）。

- resolve_scene：id 命中 → (显示名, id)；name 命中 → (显示名, id)；未命中透传；
- 未声明地点表的世界包行为与旧版完全一致（scene 自由字符串、scene_id 恒空）；
- change_scene：白名单（表外拒绝）/ reason 必填 / 关键抉择锁定 / 真值写入；
- lore 挂接：所在地点的 keys 恒参与 lore 命中（_retrieval_context）；
- 存档回环：scene_id 保留；
- 加载期交叉校验：节点/行动/开场 scene 不在表内 → WorldPackError。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.registry import build_registry
from game_agent.state import GameState
from game_agent.worldpack import (
    LocationSpec,
    WorldPackError,
    _cross_check,
    find_location,
    load_worldpack,
    resolve_scene,
)

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

LOCATIONS = [
    LocationSpec(id="shen_mansion", name="沈府后院", keys=["沈府", "后院", "海棠"]),
    LocationSpec(id="east_market", name="长安东市", keys=["东市"]),
]


def _pack_with_locations():
    pack = load_worldpack(PACK_PATH)
    pack.world = pack.world.model_copy(update={"locations": LOCATIONS})
    return pack


def _game(pack, responses=None, mutate=None):
    state = GameState.from_pack(pack)
    if mutate is not None:
        mutate(state)
    llm = LLMClient(FakeClient(responses or []), "fake", build_tools(pack.schedule))
    return state, Game(pack, state, llm)


def _n1_done(s: GameState) -> None:
    s.completed_nodes.append("n1_first_meeting")
    s.flags["met_shen"] = True


# ---------------------------------------------------------------------------
# resolve_scene / find_location
# ---------------------------------------------------------------------------


def test_resolve_scene_by_id_and_name():
    world = _pack_with_locations().world
    assert resolve_scene(world, "east_market") == ("长安东市", "east_market")
    assert resolve_scene(world, "长安东市") == ("长安东市", "east_market")
    assert resolve_scene(world, "曲江池畔") == ("曲江池畔", "")  # 未命中透传
    assert resolve_scene(world, "") == ("", "")


def test_resolve_scene_no_table_passthrough():
    """未声明地点表：行为与旧版完全一致。"""
    world = load_worldpack(PACK_PATH).world
    assert resolve_scene(world, "任意字符串") == ("任意字符串", "")


def test_find_location():
    world = _pack_with_locations().world
    assert find_location(world, "shen_mansion") is LOCATIONS[0]
    assert find_location(world, "nope") is None
    assert find_location(load_worldpack(PACK_PATH).world, "x") is None


# ---------------------------------------------------------------------------
# 三处真值写入的解析（from_pack / 节点进入 / 行动结算已由 resolve_scene 覆盖）
# ---------------------------------------------------------------------------


def test_scene_registry_only_when_locations_declared():
    """change_scene 仅在地点表声明时注册（未声明 → 引擎工具面不变）。"""
    pack_plain = load_worldpack(PACK_PATH)
    assert build_registry(pack_plain).get("change_scene") is None
    assert build_registry(_pack_with_locations()).get("change_scene") is not None


def test_change_scene_updates_truth():
    pack = _pack_with_locations()
    state, game = _game(pack, mutate=_n1_done)
    result = game.registry.dispatch(
        "change_scene", {"location": "east_market", "reason": "去东市买纸笔"}
    )
    assert result.status == "ok"
    assert state.scene == "长安东市"
    assert state.scene_id == "east_market"
    assert "场景已变更" in result.message


def test_change_scene_rejects_undeclared_and_empty_reason():
    pack = _pack_with_locations()
    state, game = _game(pack, mutate=_n1_done)
    bad = game.registry.dispatch("change_scene", {"location": "月宫", "reason": "飞升"})
    assert bad.status == "rejected" and "未声明的地点" in bad.message
    assert state.scene_id == ""  # 真值未被改动
    no_reason = game.registry.dispatch("change_scene", {"location": "east_market"})
    assert no_reason.status == "rejected" and "reason" in no_reason.message


def test_change_scene_locked_during_critical_choice():
    """关键抉择期间场景由节点接管：change_scene 被拒。"""
    pack = _pack_with_locations()
    state, game = _game(pack)
    game.start()  # 进入 N1 → 待关键抉择
    result = game.registry.dispatch(
        "change_scene", {"location": "east_market", "reason": "想跑"}
    )
    assert result.status == "rejected" and "关键抉择" in result.message


# ---------------------------------------------------------------------------
# lore 挂接：所在地点 keys 恒参与命中
# ---------------------------------------------------------------------------


def test_location_keys_boost_lore():
    from game_agent.context import ContextBuilder
    from game_agent.worldpack import LoreSpec

    lore = LoreSpec(id="shen_rule", keys=["海棠"], text="沈府后院有株老海棠。")
    pack = _pack_with_locations()
    # lore key 只在地点 keys 里（场景/目标/近对话都不含「海棠」）
    pack.world = pack.world.model_copy(
        update={"locations": LOCATIONS, "lore": [lore]}
    )
    state, game = _game(pack, mutate=_n1_done)
    state.scene, state.scene_id = "沈府后院", "shen_mansion"
    builder = ContextBuilder.from_pack(pack)
    text = builder.status_text(state, None)
    assert "沈府后院有株老海棠" in text  # 地点 keys 参与命中 → lore 注入

    # 对照：无地点表时同样的 lore 不命中（场景字符串不含 key）
    pack2 = load_worldpack(PACK_PATH)
    pack2.world = pack2.world.model_copy(update={"lore": [lore]})
    state2, game2 = _game(pack2, mutate=_n1_done)
    state2.scene, state2.scene_id = "沈府后院", ""
    text2 = ContextBuilder.from_pack(pack2).status_text(state2, None)
    assert "老海棠" not in text2


# ---------------------------------------------------------------------------
# 存档回环
# ---------------------------------------------------------------------------


def test_scene_id_survives_save_roundtrip():
    pack = _pack_with_locations()
    state, game = _game(pack, mutate=_n1_done)
    game.registry.dispatch("change_scene", {"location": "east_market", "reason": "赶集"})
    restored = GameState.from_dict(state.to_dict())
    assert restored.scene_id == "east_market"
    assert restored.scene == "长安东市"


# ---------------------------------------------------------------------------
# 加载期交叉校验
# ---------------------------------------------------------------------------


def _pack_parts(world):
    pack = load_worldpack(PACK_PATH)
    return {
        "world": world,
        "schedule": pack.schedule,
        "mainline": pack.mainline,
        "events": pack.events,
        "endings": pack.endings,
        "npcs": pack.npcs,
    }


def test_cross_check_rejects_undeclared_scene_ref():
    world = (
        load_worldpack(PACK_PATH)
        .world.model_copy(update={"locations": LOCATIONS})
    )
    with pytest.raises(WorldPackError, match="不在地点表"):
        _cross_check(_pack_parts(world))  # ancient_jianghu 节点 scene 未在表内


def test_cross_check_rejects_duplicate_location_id():
    pack = load_worldpack(PACK_PATH)
    world = pack.world.model_copy(
        update={"locations": [*LOCATIONS, LocationSpec(id="east_market", name="重复")]}
    )
    with pytest.raises(WorldPackError, match="地点 id 重复"):
        _cross_check(_pack_parts(world))


def test_cross_check_accepts_declared_refs():
    """节点/行动/开场的 scene 改为表内名称 → 校验通过（名字或 id 皆可引用）。"""
    from game_agent.worldpack import ActionSpec, MainlineSpec, NodeSpec, ScheduleSpec

    pack = load_worldpack(PACK_PATH)
    world = pack.world.model_copy(
        update={"locations": LOCATIONS, "start_scene": "沈府后院"}
    )
    nodes = []
    for node in pack.mainline.nodes:
        dumped = node.model_dump()
        dumped["on_enter"]["scene"] = "沈府后院"  # 表内显示名
        nodes.append(NodeSpec(**dumped))
    actions = []
    for action in pack.schedule.actions:
        dumped = action.model_dump()
        dumped["scene"] = "长安东市"
        actions.append(ActionSpec(**dumped))
    parts = _pack_parts(world)
    parts["mainline"] = MainlineSpec(nodes=nodes)
    parts["schedule"] = ScheduleSpec(
        **{**pack.schedule.model_dump(), "actions": actions}
    )
    _cross_check(parts)  # 不抛即通过

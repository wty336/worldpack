"""agent-first 第 4 件守卫测试：规划层 plan-and-execute（离线）。

设计文档：docs/design-planning.md。契约：
- NodeSpec.steps 可选（0~4 条、每条 ≤40 字，check-worldpack 校验）；
- 作者手写 steps 优先：进节点即生效、零 LLM 调用；
- 兜底：_ensure_plan 侧信道生成（失败/空/非法 → 无计划 = 现状行为）；
- 进度由代码按 flag 增量推进（不采信自报）：翻转 +1、一次多 flag 只 +1、上限 len−1；
- 进节点快照当前 flags（含既有 flag，不误推进）；
- 完成清空、新节点重置、存档回环兼容（老档缺省）；
- status_text 注入 <plan> 块（[x]/[→]/[ ]），query_world 同步步骤；
- 卡壳保护阈值与行为不变。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fakes import FakeClient, msg, resp

from game_agent.context import ContextBuilder
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.planning import parse_steps
from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.storyline import StorylineEngine
from game_agent.worldpack import WorldPackError, load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _story(pack):
    return StorylineEngine(pack, StatsSystem(pack.schedule))


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------


def test_parse_steps_tolerant_and_bounded():
    assert parse_steps("1. 打听消息\n2. 准备作品\n3. 参加诗会") == \
        ["打听消息", "准备作品", "参加诗会"]
    assert parse_steps("- 甲\n· 乙") == ["甲", "乙"]
    assert parse_steps("1. 甲\n2. 乙\n3. 丙\n4. 丁\n5. 戊") == ["甲", "乙", "丙", "丁"]  # 截断
    assert parse_steps("无") == [] and parse_steps("") == []
    assert parse_steps("1. " + "长" * 41) == []  # 超 40 字跳过 → 0 条 = 无计划


def test_check_worldpack_rejects_bad_steps(tmp_path):
    target = tmp_path / "p"
    shutil.copytree(PACK_PATH, target)
    mf = target / "mainline.yaml"
    data = yaml.safe_load(mf.read_text(encoding="utf-8"))
    data["nodes"][0]["steps"] = ["一", "二", "三", "四", "五"]  # 5 条 → 非法
    mf.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(WorldPackError, match="steps"):
        load_worldpack(target)

    data["nodes"][0]["steps"] = ["长" * 41]  # 超 40 字 → 非法
    mf.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(WorldPackError, match="40"):
        load_worldpack(target)


# ---------------------------------------------------------------------------
# 来源：作者手写 vs LLM 兜底
# ---------------------------------------------------------------------------


def test_author_steps_take_effect_without_llm():
    pack = _pack()
    pack.mainline.nodes[0].steps = ["打听消息", "准备", "参加"]  # 作者手写（pydantic 可改）
    state = GameState.from_pack(pack)
    node = _story(pack).begin_turn(state)[0]
    assert node is not None
    assert state.node_plan == ["打听消息", "准备", "参加"]
    assert state.node_plan_step == 0
    assert state.node_flags_snapshot == dict(pack.schedule.flags)


def test_ensure_plan_generates_from_sidechannel():
    pack = _pack()
    state = GameState.from_pack(pack)
    llm = LLMClient(
        FakeClient([resp(msg(content="1. 打探诗会消息\n2. 备诗一首\n3. 登台应战"))]),
        "fake", build_tools(pack.schedule),
    )
    game = Game(pack, state, llm, plan_node=True)
    game._ensure_plan(pack.mainline.nodes[1])
    assert state.node_plan == ["打探诗会消息", "备诗一首", "登台应战"]


def test_ensure_plan_silent_on_empty():
    pack = _pack()
    state = GameState.from_pack(pack)
    llm = LLMClient(
        FakeClient([resp(msg(content="")), resp(msg(content=""))]),  # 空 → 升级重试仍空
        "fake", build_tools(pack.schedule),
    )
    game = Game(pack, state, llm, plan_node=True)
    game._ensure_plan(pack.mainline.nodes[1])
    assert state.node_plan == []  # 无计划 = 现状行为


def test_ensure_plan_skips_when_author_steps_present():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.node_plan = ["手写步骤"]
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))  # 空队列：调用即耗尽
    game = Game(pack, state, llm, plan_node=True)
    game._ensure_plan(pack.mainline.nodes[1])
    assert state.node_plan == ["手写步骤"]  # 未覆盖、未调用 LLM


def test_narrate_hook_generates_plan_on_node_entry():
    pack = _pack()
    state = GameState.from_pack(pack)
    llm = LLMClient(
        FakeClient([resp(msg(content="1. 打探\n2. 解围\n3. 结识"))]),
        "fake", build_tools(pack.schedule),
    )
    game = Game(pack, state, llm, plan_node=True)
    view = game.start()  # 开场即进入 n1（when 恒真）
    assert view.narration is None  # 关键选择待决（n1 带 critical_choices）
    assert state.node_plan == ["打探", "解围", "结识"]


# ---------------------------------------------------------------------------
# 进度推进：真值驱动
# ---------------------------------------------------------------------------


def test_advance_on_flag_flip_once_per_turn():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.node_plan = ["一", "二", "三"]
    state.node_flags_snapshot = dict(state.flags)
    story = _story(pack)
    state.flags["met_shen"] = True  # 翻转
    story._advance_plan(state)
    assert state.node_plan_step == 1
    story._advance_plan(state)  # 无新翻转 → 不动
    assert state.node_plan_step == 1


def test_advance_capped_and_multiflag_single_step():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.node_plan = ["一", "二"]
    state.node_flags_snapshot = dict(state.flags)
    state.flags["met_shen"] = True
    state.flags["poetry_top3"] = True  # 一次多 flag 只推一步
    story = _story(pack)
    story._advance_plan(state)
    assert state.node_plan_step == 1
    story._advance_plan(state)  # 已到顶（len−1=1）
    assert state.node_plan_step == 1


def test_existing_flag_at_entry_does_not_advance():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.flags["met_shen"] = True  # 进节点前已为 True
    state.node_plan = ["一", "二"]
    state.node_flags_snapshot = dict(state.flags)  # 完整快照含该值 → 无增量
    _story(pack)._advance_plan(state)
    assert state.node_plan_step == 0


# ---------------------------------------------------------------------------
# 生命周期与存档
# ---------------------------------------------------------------------------


def test_completion_clears_plan_fields():
    pack = _pack()
    state = GameState.from_pack(pack)
    story = _story(pack)
    story.begin_turn(state)
    state.node_plan = ["一", "二"]
    state.node_plan_step = 1
    state.flags["met_shen"] = True  # n1 completion 满足
    outcome = story.end_turn(state, "normal")
    assert outcome.node_completed is not None
    assert state.node_plan == [] and state.node_plan_step == 0
    assert state.node_flags_snapshot == {}


def test_save_roundtrip_keeps_plan_and_old_save_defaults():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.node_plan = ["一", "二"]
    state.node_plan_step = 1
    state.node_flags_snapshot = {"met_shen": False}
    back = GameState.from_dict(state.to_dict())
    assert back.node_plan == ["一", "二"]
    assert back.node_plan_step == 1 and back.node_flags_snapshot == {"met_shen": False}
    old = state.to_dict()  # 老档（无计划字段）→ 空计划
    for k in ("node_plan", "node_plan_step", "node_flags_snapshot"):
        old.pop(k)
    legacy = GameState.from_dict(old)
    assert legacy.node_plan == [] and legacy.node_plan_step == 0


# ---------------------------------------------------------------------------
# 注入与卡壳保护边界
# ---------------------------------------------------------------------------


def test_status_text_plan_block():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.current_node = "n1_first_meeting"
    state.node_plan = ["打听消息", "准备作品", "参加诗会"]
    state.node_plan_step = 1
    text = ContextBuilder.from_pack(pack).status_text(state, pack.mainline.nodes[0])
    assert "<plan>" in text and "</plan>" in text
    assert "[x] 1. 打听消息" in text
    assert "[→] 2. 准备作品" in text
    assert "[ ] 3. 参加诗会" in text


def test_no_plan_no_block():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.current_node = "n1_first_meeting"
    assert "<plan>" not in ContextBuilder.from_pack(pack).status_text(state, None)


def test_query_world_includes_steps():
    pack = _pack()
    state = GameState.from_pack(pack)
    state.current_node = "n1_first_meeting"
    state.node_plan = ["打听消息", "准备作品"]
    state.node_plan_step = 0
    game = Game(pack, state, LLMClient(FakeClient([]), "fake", build_tools(pack.schedule)))
    out = game._query_world({"query": "接下来做什么"})
    assert "步骤（进行中）：1. 打听消息" in out
    assert "步骤（待做）：2. 准备作品" in out


def test_stuck_protection_unchanged_with_plan():
    """卡壳保护阈值与行为不变；计划字段不受卡壳逻辑触碰。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    story = _story(pack)
    story.begin_turn(state)
    state.node_plan = ["一", "二"]
    state.node_turns = 30
    msgs = story.end_turn(state, "normal").messages
    assert any("【推进提示】" in m["content"] for m in msgs)  # 30 轮仍触发
    assert state.node_plan == ["一", "二"] and state.node_plan_step == 0

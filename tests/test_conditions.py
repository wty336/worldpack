"""W3 验收测试：when DSL 结构校验与求值。"""

from __future__ import annotations

from pathlib import Path

import pytest

from game_agent.conditions import ConditionError, evaluate, validate_condition
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def make_state(**overrides) -> GameState:
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.stats.update(overrides.pop("stats", {}))
    state.affections.update(overrides.pop("affections", {}))
    state.flags.update(overrides.pop("flags", {}))
    state.day = overrides.pop("day", state.day)
    assert not overrides, f"未知参数: {overrides}"
    return state


def test_empty_and_empty_all_are_true():
    s = make_state()
    assert evaluate({}, s)
    assert evaluate({"all": []}, s)


def test_all_any_not_composition():
    s = make_state()
    assert evaluate({"all": [{"day": {"gte": 1}}, {"flags": {"met_shen": False}}]}, s)
    assert not evaluate({"any": [{"day": {"gte": 99}}, {"flags": {"met_shen": True}}]}, s)
    assert evaluate({"not": {"flags": {"met_shen": True}}}, s)
    assert not evaluate({"not": {"day": {"gte": 1}}}, s)


def test_day_range():
    s = make_state(day=7)
    assert evaluate({"day": {"gte": 5, "lte": 10}}, s)
    assert not evaluate({"day": {"lt": 5}}, s)


def test_stat_and_affection_comparisons():
    s = make_state(stats={"charm": 40}, affections={"shen_qingqiu": 61})
    assert evaluate({"stat": {"charm": {"gte": 30, "lt": 50}}}, s)
    assert evaluate({"affection": {"shen_qingqiu": {"gte": 60}}}, s)
    assert not evaluate({"affection": {"shen_qingqiu": {"gte": 80}}}, s)


def test_undeclared_refs_raise_at_evaluate():
    """求值期兜底：引用未声明的属性/好感/flag 必须报错而非静默为假。"""
    s = make_state()
    with pytest.raises(ConditionError, match="未声明的属性"):
        evaluate({"stat": {"nonexistent": {"gte": 1}}}, s)
    with pytest.raises(ConditionError, match="未声明的好感对象"):
        evaluate({"affection": {"nobody": {"gte": 1}}}, s)
    with pytest.raises(ConditionError, match="未声明的 flag"):
        evaluate({"flags": {"nope": True}}, s)


def test_validate_rejects_unknown_key():
    with pytest.raises(ConditionError, match="未知条件键"):
        validate_condition({"falg": {"met_shen": True}})


def test_validate_rejects_unknown_op():
    with pytest.raises(ConditionError, match="未知操作"):
        validate_condition({"day": {"more_than": 5}})


def test_validate_rejects_non_list_all():
    with pytest.raises(ConditionError, match="必须是列表"):
        validate_condition({"all": {"day": {"gte": 1}}})


def test_validate_rejects_non_bool_flag():
    with pytest.raises(ConditionError, match="true/false"):
        validate_condition({"flags": {"met_shen": 1}})


def test_real_pack_when_conditions():
    """用真实世界包的条件跑端到端（结局判定）。"""
    pack = load_worldpack(PACK_PATH)
    endings = {e.id: e.when for e in pack.endings.endings}
    # 主线节点的触发条件也应可求值
    node_when = {n.id: n.when for n in pack.mainline.nodes}

    s = make_state()
    assert evaluate(node_when["n1_first_meeting"], s)  # 开场恒真
    assert not evaluate(node_when["n2_poetry_festival"], s)  # 第 1 天不触发

    s2 = make_state(day=6, flags={"met_shen": True})
    assert evaluate(node_when["n2_poetry_festival"], s2)

    # 长相守：好感 65 + 诗会前三
    s3 = make_state(day=9, affections={"shen_qingqiu": 65}, flags={"poetry_top3": True})
    assert evaluate(endings["ending_together"], s3)
    assert not evaluate(endings["ending_wanderer"], s3)  # 未到第 10 天

    # 江湖独行：第 11 天且未达成长相守
    s4 = make_state(day=11, affections={"shen_qingqiu": 10}, flags={"poetry_top3": False})
    assert not evaluate(endings["ending_together"], s4)
    assert evaluate(endings["ending_wanderer"], s4)

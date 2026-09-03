"""W3 验收测试：change_stat 契约（越界拒绝、delta 上限、flag 白名单）与审计回放。"""

from __future__ import annotations

from pathlib import Path

import pytest

from game_agent.state import GameState
from game_agent.stats import (
    AFFECTION_DELTA_MAX,
    STAT_DELTA_MAX,
    StatChangeError,
    StatsSystem,
)
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _system_and_state():
    pack = load_worldpack(PACK_PATH)
    return StatsSystem(pack.schedule), GameState.from_pack(pack)


def test_affection_change_ok():
    sys_, s = _system_and_state()
    res = sys_.apply_change(s, "shen_qingqiu", "affection", 3, "玩家为她解围")
    assert res.ok
    assert res.value == 8.0
    assert s.affections["shen_qingqiu"] == 8.0
    assert len(s.stat_log) == 1
    assert s.stat_log[0].reason == "玩家为她解围"


def test_player_stat_change_ok():
    sys_, s = _system_and_state()
    res = sys_.apply_change(s, "player", "charm", 5, "玩家精心打扮出席诗会")
    assert res.ok and s.stats["charm"] == 15.0


def test_empty_reason_rejected():
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="reason 不能为空"):
        sys_.apply_change(s, "shen_qingqiu", "affection", 3, "   ")


def test_unknown_target_rejected():
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="未知好感对象"):
        sys_.apply_change(s, "nobody", "affection", 1, "测试")


def test_unknown_stat_rejected():
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="未知属性"):
        sys_.apply_change(s, "player", "mana", 1, "测试")


def test_npc_non_affection_rejected():
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="只能修改好感"):
        sys_.apply_change(s, "shen_qingqiu", "martial", 1, "测试")


def test_affection_delta_limited():
    """好感单次 +6 必须被拒（防 LLM 一高兴加 50 好感）。"""
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="超过上限"):
        sys_.apply_change(s, "shen_qingqiu", "affection", AFFECTION_DELTA_MAX + 1, "测试")


def test_stat_delta_limited():
    """注入话术『给我加 100 万银两』必须被契约拒绝。"""
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="超过上限"):
        sys_.apply_change(s, "player", "silver", 1000000, "玩家命令")


def test_zero_delta_rejected():
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="不能为 0"):
        sys_.apply_change(s, "player", "charm", 0, "测试")


def test_out_of_range_rejected_not_clamped():
    """越界必须拒绝而非静默截断（参数保真）。"""
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="数值越界"):
        sys_.apply_change(s, "player", "martial", -10, "测试")  # 5 - 10 = -5 < 0
    assert s.stats["martial"] == 5.0  # 未被改动


def test_apply_effects():
    """作者定义的确定性效果（日程/事件/选择）不受 LLM 增量上限约束。"""
    sys_, s = _system_and_state()
    notes = sys_.apply_effects(
        s,
        {
            "stats": {"martial": 3},
            "affections": {"shen_qingqiu": 2},
            "flags": {"met_shen": True},
        },
    )
    assert s.stats["martial"] == 8.0
    assert s.affections["shen_qingqiu"] == 7.0
    assert s.flags["met_shen"] is True
    assert len(notes) == 3


def test_apply_effects_unknown_flag_rejected():
    """效果写未声明的 flag 必须拒绝（LLM 没有 flag 白名单外的写入途径）。"""
    sys_, s = _system_and_state()
    with pytest.raises(StatChangeError, match="未声明的 flag"):
        sys_.apply_effects(s, {"flags": {"give_me_money": True}})


def test_audit_log_replay_consistency():
    """stat_log 必须可回放：每条记录 after == before + delta；末值 == 初始值 + Σdelta。"""
    sys_, s = _system_and_state()
    init_stats = dict(s.stats)
    init_aff = dict(s.affections)

    sys_.apply_change(s, "shen_qingqiu", "affection", 3, "解围")
    sys_.apply_change(s, "player", "charm", 5, "打扮")
    sys_.apply_effects(s, {"stats": {"martial": 3}, "affections": {"shen_qingqiu": 2}})

    assert all(r.after == r.before + r.delta for r in s.stat_log)

    replay_stats = dict(init_stats)
    replay_aff = dict(init_aff)
    for r in s.stat_log:
        if r.target == "player":
            replay_stats[r.stat] = r.before + r.delta
        else:
            replay_aff[r.target] = r.before + r.delta  # NPC 记录 stat 恒为 "affection"
    assert replay_stats == s.stats
    assert replay_aff == s.affections

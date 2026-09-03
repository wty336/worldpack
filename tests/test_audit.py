"""W8 验收测试：数值零偏差审计（回放 stat_log 对账）。"""

from __future__ import annotations

from pathlib import Path

from game_agent.audit import audit_stats
from game_agent.state import GameState, StatChangeRecord
from game_agent.stats import StatsSystem
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack_state():
    pack = load_worldpack(PACK_PATH)
    return pack, GameState.from_pack(pack)


def test_audit_clean_after_contract_operations():
    """全部变更都走数值系统 → 审计零偏差。"""
    pack, state = _pack_state()
    stats = StatsSystem(pack.schedule)
    stats.apply_change(state, "shen_qingqiu", "affection", 3, "解围")
    stats.apply_change(state, "player", "charm", 5, "打扮")
    stats.apply_effects(state, {"stats": {"martial": 3}, "affections": {"shen_qingqiu": 2}})
    assert audit_stats(pack, state) == []


def test_audit_detects_direct_tamper():
    """绕过数值系统的直接写入必须被查出（防 LLM/代码路径绕过）。"""
    pack, state = _pack_state()
    state.stats["silver"] += 9999  # 模拟绕过
    assert audit_stats(pack, state), "篡改未被发现"


def test_audit_detects_inconsistent_log_record():
    """日志记录自身不满足 after == before + delta 必须被查出。"""
    pack, state = _pack_state()
    state.stat_log.append(
        StatChangeRecord(day=1, target="player", stat="charm", delta=5,
                         reason="?", before=10, after=99)
    )
    assert audit_stats(pack, state), "不一致记录未被发现"


def test_audit_detects_unknown_stat_record():
    """日志引用了未声明属性必须被查出。"""
    pack, state = _pack_state()
    state.stat_log.append(
        StatChangeRecord(day=1, target="player", stat="mana", delta=5,
                         reason="?", before=0, after=5)
    )
    assert audit_stats(pack, state), "未声明属性未被发现"

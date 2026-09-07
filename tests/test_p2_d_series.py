"""P2·D 系列验收测试（离线）：D1 检定 / D2 消费闭环 / D3 收益曲线。

覆盖：
- D1：高/低属性成功率差异显著（统计）；三档效果与检定结果；审计零偏差；回合提示注入；
- D2：requires 门槛过滤与兜底；「打工↔备礼」闭环模拟（好感达 80、银两恒非负、审计零偏差）；
- D3：边际递减数值、越界饱和语义、1000 天收敛模拟（无溢出/无负循环）。
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest

from fakes import FakeClient, msg, resp, tool_call

from game_agent.audit import audit_stats
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.schedule import ScheduleError, ScheduleSystem
from game_agent.state import GameState
from game_agent.stats import StatChangeError, StatsSystem
from game_agent.worldpack import WorldPackError, load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

SUBMIT = tool_call(
    "s1", "submit_narration", {"narration": "测试叙事", "choices": ["甲", "乙", "丙"], "plot_signal": "normal"}
)

# 检定试炼行动：难度 20、margin 10、noise 10；三档银两 +3/+1/-1（可区分）
TRIAL_SCHEDULE = """\
day_action_points: 1
stats:
  charm: {label: 魅力, min: 0, max: 100, initial: 10}
  martial: {label: 武功, min: 0, max: 100, initial: 5}
  silver: {label: 银两, min: 0, max: 999999, initial: 50}
affections:
  shen_qingqiu: {label: 沈清秋, min: 0, max: 100, initial: 5}
flags:
  met_shen: false
actions:
  - id: trial
    label: 试炼
    cost: 1
    check: {stat: martial, difficulty: 20, margin: 10, noise: 10}
    effects: {stats: {silver: 1}}
    critical_effects: {stats: {silver: 3}}
    failure_effects: {stats: {silver: -1}}
  - id: train
    label: 苦练
    cost: 1
    effects: {stats: {martial: {base: 10}}}
"""


class _MidRng:
    """uniform(a,b) 取中点 → 检定掷值 = 属性值、收益曲线取基准值。"""

    def random(self):
        return 0.5

    def uniform(self, a, b):
        return a + (b - a) * self.random()


def _custom_pack(tmp_path: Path, schedule_text: str):
    shutil.copytree(PACK_PATH, tmp_path / "pack")
    pack_dir = tmp_path / "pack"
    (pack_dir / "schedule.yaml").write_text(schedule_text, encoding="utf-8")
    # 替换引用旧 flag/行动的主线/事件/结局，避免交叉校验误报
    (pack_dir / "mainline.yaml").write_text("nodes: []\n", encoding="utf-8")
    (pack_dir / "events.yaml").write_text("events: []\n", encoding="utf-8")
    (pack_dir / "endings.yaml").write_text("endings: []\n", encoding="utf-8")
    return load_worldpack(pack_dir)


def _schedule(pack, seed: int) -> ScheduleSystem:
    return ScheduleSystem(pack, StatsSystem(pack.schedule), random.Random(seed))


# ---------------------------------------------------------------------------
# D1：检定
# ---------------------------------------------------------------------------


def test_d1_success_rate_differs_significantly_by_stat(tmp_path: Path):
    """同行动高/低属性各 200 次：成功率差异 ≥50pp（属性显著驱动结果）。"""
    pack = _custom_pack(tmp_path, TRIAL_SCHEDULE)

    def rate(stat: float, seed: int) -> float:
        hits = 0
        for _ in range(200):
            state = GameState.from_pack(pack)
            state.stats["martial"] = stat
            outcome = _schedule(pack, seed + _).execute_action(state, "trial")
            hits += outcome.check.tier in ("success", "critical")
        return hits / 200

    low = rate(5, 1)  # P = clamp((5-20+10)/20) = 0
    high = rate(45, 2)  # P = 1
    assert low == 0.0
    assert high == 1.0
    assert high - low >= 0.5


def test_d1_tiers_apply_distinct_effects(tmp_path: Path):
    """三档各自独立效果：大成功 +3 / 成功 +1 / 失败 -1（中点 rng 掷值 = 属性值）。"""
    pack = _custom_pack(tmp_path, TRIAL_SCHEDULE)

    def run(stat: float) -> tuple[str, float]:
        state = GameState.from_pack(pack)
        state.stats["martial"] = stat
        sched = ScheduleSystem(pack, StatsSystem(pack.schedule), _MidRng())
        outcome = sched.execute_action(state, "trial")
        return outcome.check.tier, state.stats["silver"]

    assert run(30)[0] == "critical" and run(30)[1] == 53.0  # 30 ≥ 20+10
    assert run(20)[0] == "success" and run(20)[1] == 51.0  # 20 ≥ 20
    assert run(5)[0] == "failure" and run(5)[1] == 49.0  # 5 < 20


def test_d1_audit_zero_deviation(tmp_path: Path):
    """混合高低属性跑 50 次检定后，审计回放零偏差。"""
    pack = _custom_pack(tmp_path, TRIAL_SCHEDULE)
    state = GameState.from_pack(pack)
    sched = _schedule(pack, 42)
    for i in range(50):
        state.stats["martial"] = 40 if i % 2 == 0 else 3  # 测试脚手架：直接改检定属性
        state.action_points_left = 1
        sched.execute_action(state, "trial")
    state.stats["martial"] = 5.0  # 脚手架复位：让审计只复核行动效果（银两）路径
    assert audit_stats(pack, state) == []


def test_d1_check_result_written_into_turn_prompt():
    """检定结果（属性/掷值/难度/档位）进入给 LLM 的回合提示。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.completed_nodes.append("n1_first_meeting")
    state.flags["met_shen"] = True
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[SUBMIT]))]), "fake", build_tools(pack.schedule))

    class _HighRng:  # random 0.9 → work 掷值 = 10 + 8 = 18 ≥ 15 → 大成功
        def random(self):
            return 0.9

        def uniform(self, a, b):
            return a + (b - a) * self.random()

    game = Game(pack, state, llm, rng=_HighRng())
    game.act("work")
    prompt = next(m["content"] for m in game.history if "玩家选择日程行动" in m["content"])
    assert "【行动检定】" in prompt
    assert "魅力 10" in prompt and "掷 18.0" in prompt
    assert "大成功" in prompt
    assert "行动效果" in prompt


# ---------------------------------------------------------------------------
# D2：消费闭环
# ---------------------------------------------------------------------------


def test_d2_gift_requires_gating():
    """备礼探访：未结识/银两不足 → 不可用且执行兜底拒绝；满足后可用。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    sched = _schedule(pack, 1)
    gift = sched.action_by_id("gift_visit")

    assert not sched.action_available(state, gift)  # 未结识
    state.flags["met_shen"] = True
    state.stats["silver"] = 10
    assert not sched.action_available(state, gift)  # 银两不足
    with pytest.raises(ScheduleError, match="条件不满足"):
        sched.execute_action(state, "gift_visit")
    state.stats["silver"] = 50
    assert sched.action_available(state, gift)


def test_d2_consumption_loop_closes():
    """「打工 ↔ 备礼」闭环：好感达 80（结局门槛）、银两恒非负、审计零偏差。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.flags["met_shen"] = True
    sched = _schedule(pack, 7)
    days = 0
    while state.affections["shen_qingqiu"] < 80 and days < 120:
        action = "gift_visit" if state.stats["silver"] >= 20 else "work"
        sched.execute_action(state, action)
        assert 0 <= state.stats["silver"] <= pack.schedule.stats["silver"].max
        sched.end_day(state)
        days += 1
    assert state.affections["shen_qingqiu"] >= 80, f"第 {days} 天好感未达 80"
    assert audit_stats(pack, state) == []


# ---------------------------------------------------------------------------
# D3：收益曲线
# ---------------------------------------------------------------------------


def test_d3_decay_reduces_gain_and_hits_floor():
    """边际递减：武功 45 → 修炼 +2；武功 85 → +0（无负循环）。"""
    pack = load_worldpack(PACK_PATH)
    sched = ScheduleSystem(pack, StatsSystem(pack.schedule), _MidRng())

    state = GameState.from_pack(pack)
    state.stats["martial"] = 45
    outcome = sched.execute_action(state, "cultivate")
    assert state.stats["martial"] == 47.0  # base 4 - 45//20*1 = 2，中点 rng 无噪声
    assert "武功 +2" in outcome.notes[0]

    state.stats["martial"] = 85
    state.action_points_left = 1
    outcome = sched.execute_action(state, "cultivate")
    assert state.stats["martial"] == 85.0  # base 4 - 85//20*1 → 0，无变化、无日志
    assert outcome.notes == []


def test_d3_spread_range_and_saturation(tmp_path: Path):
    """范围随机在 [base-spread, base+spread] 内；随机曲线越界饱和（记录实际生效值）。"""
    pack = _custom_pack(tmp_path, TRIAL_SCHEDULE)  # train: martial {base: 10}
    state = GameState.from_pack(pack)
    state.stats["martial"] = 95
    sched = ScheduleSystem(pack, StatsSystem(pack.schedule), _MidRng())
    outcome = sched.execute_action(state, "train")
    assert state.stats["martial"] == 100.0  # 95 + 10 → 饱和到上限
    assert outcome.notes == ["武功 +5（已达边界）"]
    assert state.stat_log[-1].delta == 5.0  # 记录实际生效 delta
    assert audit_stats(pack, state) == []


def test_d3_plain_number_saturates_at_bounds():
    """世界包效果统一饱和语义：普通数字越界不抛错，记录实际生效 delta（防 97 好感炸档）。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    stats = StatsSystem(pack.schedule)
    notes = stats.apply_effects(state, {"stats": {"silver": -100}})
    assert state.stats["silver"] == 0.0  # 饱和到下限
    assert state.stat_log[-1].delta == -50.0
    assert "已达边界" in notes[0]
    from game_agent.audit import audit_stats

    assert audit_stats(pack, state) == []


def test_d3_convergence_1000_days():
    """1000 天模拟：无溢出、无负循环、无异常，审计零偏差。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.flags["met_shen"] = True
    sched = _schedule(pack, 1234)
    for i in range(1000):
        avail = [a for a in sched.actions() if sched.action_available(state, a)]
        sched.execute_action(state, avail[i % len(avail)].id)
        for k, v in state.stats.items():
            spec = pack.schedule.stats[k]
            assert spec.min <= v <= spec.max, f"{k} 越界: {v}"
        for k, v in state.affections.items():
            spec = pack.schedule.affections[k]
            assert spec.min <= v <= spec.max, f"{k} 好感越界: {v}"
        sched.end_day(state)
    assert audit_stats(pack, state) == []


# ---------------------------------------------------------------------------
# 世界包校验（D 系列 schema）
# ---------------------------------------------------------------------------


def test_check_undeclared_stat_raises(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    schedule = pack_dir / "schedule.yaml"
    schedule.write_text(
        schedule.read_text(encoding="utf-8").replace(
            "check: {stat: charm, difficulty: 5, margin: 10}",
            "check: {stat: mana, difficulty: 5}",
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorldPackError, match="检定 check.stat 引用了未声明的属性"):
        load_worldpack(pack_dir)


def test_requires_undeclared_flag_raises(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    schedule = pack_dir / "schedule.yaml"
    schedule.write_text(
        schedule.read_text(encoding="utf-8").replace(
            "- {flags: {met_shen: true}}", "- {flags: {met_her: true}}"
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorldPackError, match="requires 引用了未声明的 flag"):
        load_worldpack(pack_dir)


def test_negative_spread_raises(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    schedule = pack_dir / "schedule.yaml"
    schedule.write_text(
        schedule.read_text(encoding="utf-8").replace(
            "martial: {base: 4, spread: 1, decay_every: 20, decay_step: 1}",
            "martial: {base: 4, spread: -1}",
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorldPackError, match="'spread' 不能为负"):
        load_worldpack(pack_dir)

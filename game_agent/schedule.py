"""日程系统（W6）：行动点、行动结算、日期推进（design.md §5.3）。

数值与时间只由代码推进：行动收益公式是确定性函数，LLM 只负责叙事。
D 系列（P2）扩展：
- D1 检定：行动可选 check（stat/difficulty/margin/noise），roll = 属性 + 均匀噪声，
  分大成功/成功/失败三档，各档独立效果，结果随 ActionOutcome 返回（引擎写入回合提示）；
- D2 门槛：行动可选 requires（条件 DSL），action_available 过滤 + 执行兜底；
- D3 收益曲线：效果值支持 {base/spread/decay_every/decay_step}（stats.apply_effects 结算）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .conditions import ConditionError, evaluate
from .state import GameState
from .stats import StatsSystem
from .worldpack import ActionEffects, ActionSpec, WorldPack

TIER_CN = {"critical": "大成功", "success": "成功", "failure": "失败"}


class ScheduleError(Exception):
    """日程操作错误（行动点不足、未知行动、条件不满足）。"""


@dataclass(frozen=True)
class CheckResult:
    """一次行动检定的结果（D1）。"""

    tier: str  # "critical" / "success" / "failure"
    stat: str
    value: float  # 检定时的属性值
    roll: float  # 掷值（stat + 噪声）
    difficulty: float
    margin: float

    @property
    def tier_cn(self) -> str:
        return TIER_CN[self.tier]


@dataclass(frozen=True)
class ActionOutcome:
    """行动结算结果：检定（无检定行动为 None）+ 生效效果描述。"""

    action: ActionSpec
    check: CheckResult | None
    notes: list[str]


class ScheduleSystem:
    def __init__(
        self, pack: WorldPack, stats: StatsSystem, rng: random.Random | None = None
    ):
        self.pack = pack
        self.stats = stats
        self.spec = pack.schedule
        self.rng = rng or random.Random()

    def actions(self) -> list[ActionSpec]:
        return list(self.spec.actions)

    def action_by_id(self, action_id: str) -> ActionSpec:
        for action in self.spec.actions:
            if action.id == action_id:
                return action
        raise ScheduleError(
            f"未知日程行动 '{action_id}'；可用行动: {', '.join(a.id for a in self.spec.actions)}"
        )

    # ------------------------------------------------------------------
    # 行动可用性（D2：行动点 + requires 门槛）
    # ------------------------------------------------------------------

    def action_available(self, state: GameState, action: ActionSpec) -> bool:
        """行动点充足且 requires 条件满足（条件求值失败按不可用处理）。"""
        if state.action_points_left < action.cost:
            return False
        if action.requires is None:
            return True
        try:
            return evaluate(action.requires, state)
        except ConditionError:
            return False

    def _require_check(self, state: GameState, action: ActionSpec) -> None:
        if action.requires is not None and not evaluate(action.requires, state):
            raise ScheduleError(f"行动 '{action.label}' 的条件不满足（requires）")

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def execute_action(self, state: GameState, action_id: str) -> ActionOutcome:
        """执行日程行动：门槛校验 → 检定/效果结算 → 场景切换 → 消耗行动点。"""
        action = self.action_by_id(action_id)
        if state.action_points_left < action.cost:
            raise ScheduleError(
                f"行动点不足：需要 {action.cost}，剩余 {state.action_points_left}"
            )
        self._require_check(state, action)

        check = self._roll_check(state, action)
        if check is None:
            effects = action.effects
        else:
            effects = {
                "critical": action.critical_effects or action.effects,
                "success": action.effects,
                "failure": action.failure_effects or action.effects,
            }[check.tier]
        notes = self.stats.apply_effects(state, _dump_effects(effects), rng=self.rng)

        state.action_points_left -= action.cost
        if action.scene:
            state.scene = action.scene
        state.present_npcs = list(action.present)
        return ActionOutcome(action=action, check=check, notes=notes)

    def _roll_check(self, state: GameState, action: ActionSpec) -> CheckResult | None:
        """D1 检定：roll = stat + uniform(-noise, +noise) → 三档。无检定返回 None。"""
        if action.check is None:
            return None
        c = action.check
        value = state.stats.get(c.stat)
        if value is None:  # 加载期已校验，兜底
            raise ScheduleError(f"检定引用了未声明的属性 '{c.stat}'")
        roll = value + self.rng.uniform(-c.noise, c.noise)
        if roll >= c.difficulty + c.margin:
            tier = "critical"
        elif roll >= c.difficulty:
            tier = "success"
        else:
            tier = "failure"
        return CheckResult(
            tier=tier,
            stat=c.stat,
            value=value,
            roll=roll,
            difficulty=c.difficulty,
            margin=c.margin,
        )

    def end_day(self, state: GameState) -> None:
        """推进到下一天并重置行动点。"""
        state.day += 1
        state.action_points_left = self.spec.day_action_points


def _dump_effects(effects: ActionEffects) -> dict:
    """ActionEffects → 效果字典（收益曲线保留为 dict 供 stats 解析）。"""
    return {
        "stats": {k: v.model_dump() if hasattr(v, "model_dump") else v for k, v in effects.stats.items()},
        "affections": {
            k: v.model_dump() if hasattr(v, "model_dump") else v
            for k, v in effects.affections.items()
        },
    }

"""日程系统（W6）：行动点、行动结算、日期推进（design.md §5.3）。

数值与时间只由代码推进：行动收益公式是确定性函数，LLM 只负责叙事。
"""

from __future__ import annotations

from .state import GameState
from .stats import StatsSystem
from .worldpack import ActionSpec, WorldPack


class ScheduleError(Exception):
    """日程操作错误（行动点不足、未知行动）。"""


class ScheduleSystem:
    def __init__(self, pack: WorldPack, stats: StatsSystem):
        self.pack = pack
        self.stats = stats
        self.spec = pack.schedule

    def actions(self) -> list[ActionSpec]:
        return list(self.spec.actions)

    def action_by_id(self, action_id: str) -> ActionSpec:
        for action in self.spec.actions:
            if action.id == action_id:
                return action
        raise ScheduleError(
            f"未知日程行动 '{action_id}'；可用行动: {', '.join(a.id for a in self.spec.actions)}"
        )

    def execute_action(self, state: GameState, action_id: str) -> list[str]:
        """执行日程行动：行动点校验 → 效果结算 → 场景/在场切换 → 消耗行动点。"""
        action = self.action_by_id(action_id)
        if state.action_points_left < action.cost:
            raise ScheduleError(
                f"行动点不足：需要 {action.cost}，剩余 {state.action_points_left}"
            )
        notes = self.stats.apply_effects(state, action.effects.model_dump())
        state.action_points_left -= action.cost
        if action.scene:
            state.scene = action.scene
        state.present_npcs = list(action.present)
        return notes

    def end_day(self, state: GameState) -> None:
        """推进到下一天并重置行动点。"""
        state.day += 1
        state.action_points_left = self.spec.day_action_points

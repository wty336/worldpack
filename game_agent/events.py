"""事件系统（W6）：条件触发 + 日程触发（design.md §9）。

- 触发判定全部由代码完成，LLM 不参与；
- once 防重（state.triggered_events 只增不改）；
- 同时满足多个条件事件时按 priority 仲裁（同一时段只放一个主事件）；
- 触发 = 代码结算效果 + 返回事件脚本消息（LLM 据此叙事）。
"""

from __future__ import annotations

import random

from .conditions import evaluate
from .state import GameState
from .stats import StatsSystem
from .worldpack import EventSpec, WorldPack

_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}


class EventSystem:
    def __init__(self, pack: WorldPack, stats: StatsSystem, rng: random.Random | None = None):
        self.pack = pack
        self.stats = stats
        self.rng = rng or random.Random()

    # ------------------------------------------------------------------
    # 触发检查
    # ------------------------------------------------------------------

    def _untriggered(self, state: GameState) -> list[EventSpec]:
        return [e for e in self.pack.events.events if e.id not in state.triggered_events]

    def check_condition_events(self, state: GameState) -> EventSpec | None:
        """条件触发：数值/flags 变化后检查。返回优先级最高的一个（其余顺延）。"""
        candidates = [
            e
            for e in self._untriggered(state)
            if e.trigger.kind == "condition" and evaluate(e.trigger.when, state)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda e: _PRIORITY_ORDER[e.priority])
        return candidates[0]

    def check_schedule_event(self, state: GameState, action_id: str) -> EventSpec | None:
        """日程触发：行动结算后按概率检查。"""
        for event in self._untriggered(state):
            if event.trigger.kind != "schedule" or event.trigger.action != action_id:
                continue
            chance = event.trigger.chance if event.trigger.chance is not None else 1.0
            if self.rng.random() < chance:
                return event
        return None

    def check_time_events(self, state: GameState) -> list[EventSpec]:
        """时间触发：日期推进后检查。返回当日到期的事件（按优先级排序，全部触发）。"""
        due = [
            e
            for e in self._untriggered(state)
            if e.trigger.kind == "time" and evaluate(e.trigger.when, state)
        ]
        due.sort(key=lambda e: _PRIORITY_ORDER[e.priority])
        return due

    # ------------------------------------------------------------------
    # 触发执行
    # ------------------------------------------------------------------

    def trigger(self, state: GameState, event: EventSpec) -> dict:
        """执行事件：结算效果、记录防重、返回事件脚本消息（追加到历史）。"""
        notes = self.stats.apply_effects(state, event.effects)
        state.triggered_events.append(event.id)
        note_str = "；".join(notes) if notes else "无"
        return {
            "role": "user",
            "content": f"【事件】{event.title}\n{event.script}\n（事件效果：{note_str}）",
        }

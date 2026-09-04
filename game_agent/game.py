"""游戏主循环（W7）：把状态/数值/剧情/事件/日程/上下文/LLM 串成可玩回合。

每个叙事回合（_narrate）统一走：
  begin_turn（节点触发/关键选择门）→ LLM 协议闭环 → end_turn（完成判定/卡壳/结局）
  → 条件事件级联（最多 2 层）。
"""

from __future__ import annotations

import difflib
import random
from dataclasses import dataclass
from pathlib import Path

from .context import ContextBuilder
from .events import EventSystem
from .llm import LLMClient
from .memory import MemorySystem
from .save import save_game
from .schedule import ScheduleSystem
from .state import GameState
from .stats import StatsSystem
from .storyline import StorylineEngine, filter_choices
from .worldpack import ActionSpec, CriticalChoice, EndingSpec, WorldPack


class GameError(Exception):
    """游戏规则错误（如关键抉择期间尝试自由输入）。"""


REPETITION_THRESHOLD = 0.6  # 相邻回合叙事相似度阈值：超过则注入反重复提示（试玩反馈 #3/#4）


@dataclass
class TurnView:
    """一个叙事回合给玩家看的东西。"""

    narration: str | None  # None = 关键选择待决（只显示固定选项）
    choices: list[str]
    ending: EndingSpec | None = None
    choice_prompt: CriticalChoice | None = None
    briefing: str | None = None  # 关键抉择前的剧情背景（首次展示给玩家）


class Game:
    def __init__(
        self,
        pack: WorldPack,
        state: GameState,
        llm: LLMClient,
        rng: random.Random | None = None,
        autosave_path: str | Path | None = None,
        on_text=None,
    ):
        self.pack = pack
        self.state = state
        self.llm = llm
        self.stats = StatsSystem(pack.schedule)
        self.story = StorylineEngine(pack, self.stats)
        self.events = EventSystem(pack, self.stats, rng)
        self.schedule = ScheduleSystem(pack, self.stats)
        self.memory = MemorySystem(pack)  # M2a 记忆显式化
        self.builder = ContextBuilder.from_pack(pack)
        self.history: list[dict] = []
        self.ending: EndingSpec | None = None
        self.last_choices: list[str] = []
        self.autosave_path = autosave_path  # 非 None 时，节点完成自动存档
        self.on_text = on_text  # 流式显示回调（CLI 注入）
        self.last_streamed: str = ""  # 本回合已流式显示的文本（供 CLI 去重）
        self.last_narration: str = ""  # 上一轮叙事（重复检测参照）

    # ------------------------------------------------------------------
    # 玩家操作
    # ------------------------------------------------------------------

    def start(self) -> TurnView:
        """开场：触发初始节点并生成开场叙事。"""
        return self._narrate("（游戏开始）你踏入了长安城。")

    def act(self, action_id: str) -> TurnView:
        """执行日程行动：结算 → 日程事件检查 → 叙事。"""
        action = self.schedule.action_by_id(action_id)
        self.schedule.execute_action(self.state, action_id)
        prompt = f"（玩家选择日程行动：{action.label}）"
        ev = self.events.check_schedule_event(self.state, action_id)
        if ev is not None:
            self.history.append(self.events.trigger(self.state, ev))
        return self._narrate(prompt)

    def say(self, text: str) -> TurnView:
        """玩家自由输入（或点日常选项）。关键抉择期间拒绝。"""
        if self.story.choice_locked(self.state):
            raise GameError("此刻是关键抉择，只能从固定选项中选择")
        return self._narrate(text)

    def pick(self, option_index: int) -> TurnView:
        """关键抉择：选择固定选项并叙述后果。"""
        msg = self.story.choose_option(self.state, option_index)
        self.history.append(msg)
        return self._narrate(msg["content"])

    def end_day(self) -> str:
        self.schedule.end_day(self.state)
        # 时间触发事件：日期推进后检查，消息并入历史（下一个叙事回合生效）
        for ev in self.events.check_time_events(self.state):
            self.history.append(self.events.trigger(self.state, ev))
        return f"—— 第 {self.state.day} 天 ——"

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def actions_available(self) -> list[ActionSpec]:
        return [
            a for a in self.schedule.actions() if a.cost <= self.state.action_points_left
        ]

    def status_text(self) -> str:
        return self.builder.status_text(self.state, self.story.active_node(self.state))

    # ------------------------------------------------------------------
    # 内部：统一叙事回合
    # ------------------------------------------------------------------

    def _narrate(self, prompt: str | None = None) -> TurnView:
        node, node_msgs = self.story.begin_turn(self.state)
        self.history.extend(node_msgs)
        choice = self.story.pending_choice(self.state)
        if choice is not None:
            # 关键抉择前把节点剧情背景带给玩家（修复"上来就是选项"体验问题）
            briefing = node.on_enter.briefing if node is not None else None
            return TurnView(
                narration=None,
                choices=[o.text for o in choice.options],
                choice_prompt=choice,
                briefing=briefing,
            )
        if prompt is not None:
            self.history.append({"role": "user", "content": prompt})

        views: list[TurnView] = []
        for _ in range(3):  # 1 个主回合 + 最多 2 个条件事件级联
            view = self._llm_round()
            views.append(view)
            if view.ending is not None:
                break
            ev = self.events.check_condition_events(self.state)
            if ev is None:
                break
            self.history.append(self.events.trigger(self.state, ev))

        narration = "\n\n".join(v.narration for v in views if v.narration)
        last = views[-1]
        self.ending = last.ending
        self.last_choices = last.choices
        if narration:
            self._check_repetition(narration)
        return TurnView(
            narration=narration or None,
            choices=last.choices,
            ending=last.ending,
        )

    def _check_repetition(self, narration: str) -> None:
        """相邻回合相似度检测：模型复读已写过的段落时注入反重复提示（下一轮生效）。"""
        if not self.last_narration:
            self.last_narration = narration
            return
        ratio = difflib.SequenceMatcher(None, self.last_narration, narration).ratio()
        self.last_narration = narration
        if ratio >= REPETITION_THRESHOLD:
            self.history.append(
                {
                    "role": "user",
                    "content": (
                        f"[反重复提示] 本轮叙事与上一轮高度重复（相似度 {ratio:.0%}）。"
                        "请避免复述已经写过的场景与对话，改为推进新情节：新的事件、新的细节、"
                        "人物关系的新变化。"
                    ),
                }
            )

    def _llm_round(self) -> TurnView:
        messages = self.builder.build_messages(
            self.state, self.history, self.story.active_node(self.state)
        )
        self.state.turn_count += 1  # 记忆来源追踪（M2a）
        result = self.llm.run_turn(
            messages, self._apply_change, on_text=self.on_text, remember=self._remember
        )
        self.history = result.messages
        outcome = self.story.end_turn(self.state, result.plot_signal)
        self.history.extend(outcome.messages)
        # 节点完成 → 自动存档（W-C：长局防丢进度，引擎侧钩子；含对话历史）
        if outcome.node_completed is not None and self.autosave_path is not None:
            save_game(self.state, self.autosave_path, self.history)
        return TurnView(
            narration=result.narration,
            choices=filter_choices(self.pack, result.choices),
            ending=outcome.ending,
        )

    def _apply_change(self, args: dict) -> str:
        return self.stats.apply_change(
            self.state, args["target"], args["stat"], args["delta"], args["reason"]
        ).message

    def _remember(self, args: dict) -> str:
        """remember 工具回调：模型提议 → MemorySystem 校验写入（M2a）。"""
        return self.memory.add(self.state, args["target"], args["fact"])

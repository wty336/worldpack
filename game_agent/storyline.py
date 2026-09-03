"""剧情状态机（W5）：大纲节点链 + 关键选项接管（design.md §6）。

职责：
- 节点触发：when 条件满足 → 进入节点（注入任务卡，设定场景与在场 NPC）；
- 完成判定：completion 条件由代码复核，LLM 的 plot_signal 只是自报、不是依据；
- 卡壳保护：节点内回合数超阈值 → 先注入「推进提示」，再注入「命运事件」；
- 关键选择：节点声明 critical_choices 时锁定自由输入，只允许固定选项并写 flag；
- 结局检查：每回合结束按 endings.yaml 条件判定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .conditions import evaluate
from .state import ChoiceRecord, GameState
from .stats import StatsSystem
from .worldpack import CriticalChoice, EndingSpec, NodeSpec, WorldPack, WorldSpec

FREE_INPUT_OPTION = "（自己说些什么…）"


class StorylineError(Exception):
    """剧情状态机错误（非法操作，如越界选择）。"""


@dataclass
class TurnOutcome:
    """回合结束后的结算结果。"""

    node_completed: NodeSpec | None = None
    messages: list[dict] = field(default_factory=list)  # 要追加到历史的消息（推进提示等）
    ending: EndingSpec | None = None


def _forbidden_tokens(spec: WorldSpec) -> list[str]:
    """从禁用清单提取可匹配的短词（括号内示例词 + 主词），用于选项过滤。"""
    tokens: list[str] = []
    for entry in spec.forbidden:
        for part in re.split(r"[（(]", entry):
            for tok in re.split(r"[、，,]", part):
                tok = tok.strip().strip("）)").strip().rstrip("等。，、").strip()
                if 1 < len(tok) <= 6:
                    tokens.append(tok)
    return tokens


def filter_choices(pack: WorldPack, choices: list[str]) -> list[str]:
    """日常选项过滤：剔除含禁用元素的选项；恒追加自由输入入口（design.md §7.1）。

    过滤是安全兜底：宁可选项变少，也不放行世界观外元素（玩家仍有自由输入入口）。
    """
    tokens = _forbidden_tokens(pack.world)
    kept = [c for c in choices if not any(t in c for t in tokens)]
    result = [c for c in kept if c != FREE_INPUT_OPTION]
    result.append(FREE_INPUT_OPTION)
    return result


class StorylineEngine:
    def __init__(
        self,
        pack: WorldPack,
        stats: StatsSystem,
        stuck_threshold: int = 30,
        forced_threshold: int = 60,
    ):
        self.pack = pack
        self.stats = stats
        self.stuck_threshold = stuck_threshold
        self.forced_threshold = forced_threshold

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def node_by_id(self, node_id: str) -> NodeSpec | None:
        for node in self.pack.mainline.nodes:
            if node.id == node_id:
                return node
        return None

    def active_node(self, state: GameState) -> NodeSpec | None:
        if state.current_node is None:
            return None
        return self.node_by_id(state.current_node)

    def pending_choice(self, state: GameState) -> CriticalChoice | None:
        node = self.active_node(state)
        if node is None or state.pending_choice is None:
            return None
        for choice in node.critical_choices:
            if choice.id == state.pending_choice:
                return choice
        return None

    def choice_locked(self, state: GameState) -> bool:
        """True = 关键选择待决，引擎必须锁定输入（只允许固定选项）。"""
        return self.pending_choice(state) is not None

    # ------------------------------------------------------------------
    # 回合开始：节点触发
    # ------------------------------------------------------------------

    def begin_turn(self, state: GameState) -> tuple[NodeSpec | None, list[dict]]:
        """回合开始前调用。若新节点触发则进入并返回（节点, 任务卡消息）。"""
        node = self._try_enter_node(state)
        msgs: list[dict] = []
        if node is not None:
            msgs.append(
                {
                    "role": "user",
                    "content": f"【主线节点】{node.title}\n{node.on_enter.briefing}",
                }
            )
        return node, msgs

    def _try_enter_node(self, state: GameState) -> NodeSpec | None:
        if state.current_node is not None:
            return None  # 上一节点未完成，不进入新节点（剧情串行推进）
        for node in self.pack.mainline.nodes:
            if node.id in state.completed_nodes or node.id == state.current_node:
                continue
            if evaluate(node.when, state):
                state.current_node = node.id
                state.node_turns = 0
                state.stuck_stage = 0
                state.scene = node.on_enter.scene
                state.present_npcs = list(node.on_enter.present)
                state.resolved_choices = []
                state.pending_choice = (
                    node.critical_choices[0].id if node.critical_choices else None
                )
                return node
        return None

    # ------------------------------------------------------------------
    # 关键选择
    # ------------------------------------------------------------------

    def choose_option(self, state: GameState, option_index: int) -> dict:
        """执行玩家选中的关键选项：校验 → 应用效果 → 记录 → 推进待选队列。

        返回追加到历史的 user 消息（让 LLM 接着叙述选择后果）。
        """
        node = self.active_node(state)
        if node is None:
            raise StorylineError("当前没有进行中的主线节点")
        choice = self.pending_choice(state)
        if choice is None:
            raise StorylineError("当前没有待选择的关键选项")
        if not isinstance(option_index, int) or not (0 <= option_index < len(choice.options)):
            raise StorylineError(
                f"选项序号超出范围：0~{len(choice.options) - 1}，当前为 {option_index!r}"
            )
        option = choice.options[option_index]

        # 1) 效果由代码结算（flags/数值），LLM 不参与
        self.stats.apply_effects(state, option.effects)

        # 2) append-only 选择日志（多结局回溯依据）
        state.choice_log.append(
            ChoiceRecord(
                day=state.day,
                node_id=node.id,
                choice_id=f"{choice.id}.{option_index + 1}",
                text=option.text,
            )
        )

        # 3) 推进待选队列
        state.resolved_choices.append(choice.id)
        state.pending_choice = next(
            (c.id for c in node.critical_choices if c.id not in state.resolved_choices),
            None,
        )

        return {"role": "user", "content": f"（你选择了：{option.text}）"}

    # ------------------------------------------------------------------
    # 回合结束：完成判定 / 卡壳保护 / 结局
    # ------------------------------------------------------------------

    def end_turn(self, state: GameState, plot_signal: str = "normal") -> TurnOutcome:
        outcome = TurnOutcome()
        node = self.active_node(state)
        if node is not None:
            state.node_turns += 1
            # 完成判定：代码复核 completion 条件；plot_signal 只是模型自报，不作为依据
            if evaluate(node.completion, state):
                outcome.node_completed = node
                state.completed_nodes.append(node.id)
                state.current_node = None
                state.pending_choice = None
                state.resolved_choices = []
                state.node_turns = 0
                state.stuck_stage = 0
                state.scene = self.pack.world.start_scene
                state.present_npcs = []
                outcome.messages.append(
                    {"role": "user", "content": f"【节点完成】主线节点「{node.title}」目标达成。"}
                )
            else:
                outcome.messages.extend(self._stuck_messages(state, node))

        outcome.ending = self.check_ending(state)
        return outcome

    def _stuck_messages(self, state: GameState, node: NodeSpec) -> list[dict]:
        """卡壳保护（design.md §6.2）：先推进提示，再命运事件，各只注入一次。"""
        msgs: list[dict] = []
        if state.node_turns >= self.forced_threshold and state.stuck_stage < 2:
            state.stuck_stage = 2
            msgs.append(
                {
                    "role": "user",
                    "content": (
                        f"【命运事件】剧情的关键时刻到了。请在本轮叙事中直接推动主线目标"
                        f"「{node.goal}」达成——该发生的转折就在此刻发生。达成后按需调用 "
                        f"change_stat 记录数值变化，并以 submit_narration(plot_signal="
                        f"\"node_complete\") 收尾。"
                    ),
                }
            )
        elif state.node_turns >= self.stuck_threshold and state.stuck_stage < 1:
            state.stuck_stage = 1
            msgs.append(
                {
                    "role": "user",
                    "content": (
                        f"【推进提示】当前主线节点「{node.title}」尚未完成，目标：{node.goal}。"
                        f"请在本轮叙事中向目标推进（推动关键情节发生），不要停留在日常寒暄。"
                    ),
                }
            )
        return msgs

    # ------------------------------------------------------------------
    # 结局
    # ------------------------------------------------------------------

    def check_ending(self, state: GameState) -> EndingSpec | None:
        for ending in self.pack.endings.endings:
            if ending.kind == "auto" and evaluate(ending.when, state):
                return ending
        return None

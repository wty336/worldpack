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

from .budgets import (
    COMPRESS_MAX_TOKENS,
    EXTRACT_MAX_TOKENS,
    REFLECT_MAX_TOKENS,
    complete_with_empty_retry,
)
from .compression import (
    COMPRESS_SYSTEM,
    SUMMARY_MARK,
    SUMMARY_MAX_TARGET,
    ensure_pairing,
    find_turn_cut,
    history_text,
    history_tokens,
    locate_summary,
    rebuild_history,
)
from .context import ContextBuilder
from .events import EventSystem
from .judge import JudgeSystem
from .llm import LLMClient
from .memory import (
    EXTRACT_SYSTEM,
    INSIGHT_CAP,
    REFLECT_MATERIAL,
    REFLECT_MIN_MEMORIES,
    REFLECT_SYSTEM,
    MemoryError,
    MemorySystem,
    parse_facts,
    parse_insights,
)
from .save import save_game
from .schedule import ScheduleSystem
from .state import GameState, InsightEntry
from .stats import StatChangeError, StatsSystem
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
        extract_every: int = 0,  # M2a 迭代4：>0 时每 N 回合确定性提取玩家事实（0=关闭）
        compress_threshold: int = 0,  # M2b：历史 token 估算超此阈值时批量压缩（0=关闭）
        keep_turns: int = 6,  # M2b：压缩时保留的近窗回合数
        judge_every: int = 0,  # M2b：>0 时每 N 回合做一次语义校验（0=关闭）
        reflect_every: int = 0,  # A3（P1）：>0 时每 N 回合做关系洞察反思（0=关闭）
    ):
        self.pack = pack
        self.state = state
        self.llm = llm
        self.stats = StatsSystem(pack.schedule)
        self.story = StorylineEngine(pack, self.stats)
        self.events = EventSystem(pack, self.stats, rng)
        self.schedule = ScheduleSystem(pack, self.stats, rng)  # D 系列：检定/收益曲线共用 rng
        self.memory = MemorySystem(pack, llm)  # M2a 记忆显式化 + A4 语义去重（P1）
        self.builder = ContextBuilder.from_pack(pack)
        self.history: list[dict] = []
        self.ending: EndingSpec | None = None
        self.last_choices: list[str] = []
        self.autosave_path = autosave_path  # 非 None 时，节点完成自动存档
        self.on_text = on_text  # 流式显示回调（CLI 注入）
        self.last_streamed: str = ""  # 本回合已流式显示的文本（供 CLI 去重）
        self.last_narration: str = ""  # 上一轮叙事（重复检测参照）
        self.extract_every = extract_every  # M2a 迭代4：确定性提取间隔（0=关闭）
        self.compress_threshold = compress_threshold  # M2b 压缩阈值
        self.keep_turns = keep_turns  # M2b 压缩近窗
        self.judge_every = judge_every  # M2b 语义校验间隔（0=关闭）
        self.reflect_every = reflect_every  # A3 反思间隔（0=关闭）
        self.judge = JudgeSystem(llm)  # M2b

    # ------------------------------------------------------------------
    # 玩家操作
    # ------------------------------------------------------------------

    def start(self) -> TurnView:
        """开场：触发初始节点并生成开场叙事。起手提示由世界包 opening 驱动——
        opening 全文已注入静态前缀，这里只发中性起手信号（F1：引擎层不得含内容文案）。
        """
        return self._narrate(self._start_prompt())

    def _start_prompt(self) -> str:
        if self.pack.world.opening:
            return "（游戏开始）请根据开场设定开始叙事。"
        return "（游戏开始）"

    def act(self, action_id: str) -> TurnView:
        """执行日程行动：结算（门槛/检定/效果）→ 日程事件检查 → 叙事。"""
        action = self.schedule.action_by_id(action_id)
        outcome = self.schedule.execute_action(self.state, action_id)
        lines = [f"（玩家选择日程行动：{action.label}）"]
        if outcome.check is not None:
            c = outcome.check
            stat_label = self.pack.schedule.stats[c.stat].label
            lines.append(
                f"【行动检定】{stat_label} {c.value:g} · 掷 {c.roll:.1f}"
                f"（难度 {c.difficulty:g}，大成功需 ≥{c.difficulty + c.margin:g}）"
                f"· 结果：{c.tier_cn}"
            )
        if outcome.notes:
            lines.append(f"（行动效果：{'；'.join(outcome.notes)}）")
        ev = self.events.check_schedule_event(self.state, action_id)
        if ev is not None:
            self.history.append(self.events.trigger(self.state, ev))
        return self._narrate("\n".join(lines))

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
        """行动点 + requires 门槛（D2）双重过滤后的可选行动。"""
        return [
            a for a in self.schedule.actions() if self.schedule.action_available(self.state, a)
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
                    "name": "engine",  # A-2：引擎元消息，排除出检索上下文
                    "content": (
                        f"[反重复提示] 本轮叙事与上一轮高度重复（相似度 {ratio:.0%}）。"
                        "请避免复述已经写过的场景与对话，改为推进新情节：新的事件、新的细节、"
                        "人物关系的新变化。"
                    ),
                }
            )

    def _llm_round(self) -> TurnView:
        # M2b 压缩：接近阈值时批量压缩（只碰历史，不碰静态前缀与事实区块）
        if self.compress_threshold > 0 and history_tokens(self.history) > self.compress_threshold:
            self._compress_history()
        messages = self.builder.build_messages(
            self.state, self.history, self.story.active_node(self.state)
        )
        self.state.turn_count += 1  # 记忆来源追踪（M2a）
        result = self.llm.run_turn(
            messages, self._apply_change, on_text=self.on_text, remember=self._remember
        )
        # run_turn 返回的消息含 system 前缀；历史只保留对话部分，
        # 否则下轮 build_messages 会把 system 重复注入（压缩测试抓到的潜伏 bug）
        self.history = [m for m in result.messages if m.get("role") != "system"]
        outcome = self.story.end_turn(self.state, result.plot_signal)
        self.history.extend(outcome.messages)
        # 节点完成 → 自动存档（W-C：长局防丢进度，引擎侧钩子；含对话历史）
        if outcome.node_completed is not None and self.autosave_path is not None:
            save_game(self.state, self.autosave_path, self.history)
        # M2a 迭代4：确定性提取兜底（弥补 remember 主动性的覆盖缺口）
        if self.extract_every > 0 and self.state.turn_count % self.extract_every == 0:
            self._extract_facts()
        # M2b 语义校验：每 N 回合检查最新叙事，失败注入下轮修正提示
        if self.judge_every > 0 and self.state.turn_count % self.judge_every == 0:
            self._judge_turn(result.narration)
        # A3 反思：每 N 回合对记忆增长达标的 NPC 合成关系洞察（侧信道，失败静默）
        if self.reflect_every > 0 and self.state.turn_count % self.reflect_every == 0:
            self._reflect()
        return TurnView(
            narration=result.narration,
            choices=filter_choices(self.pack, result.choices),
            ending=outcome.ending,
        )

    def _apply_change(self, args: dict) -> str:
        for key in ("target", "stat", "delta", "reason"):
            if key not in args:
                raise StatChangeError(f"change_stat 缺少参数 '{key}'")
        return self.stats.apply_change(
            self.state, args["target"], args["stat"], args["delta"], args["reason"]
        ).message

    def _remember(self, args: dict) -> str:
        """remember 工具回调：模型提议 → MemorySystem 校验写入（M2a + A2 重要性）。

        缺参数抛 MemoryError → llm.py 转为结构化 tool 错误回传（防模型漏参导致崩溃）。
        """
        for key in ("target", "fact"):
            if key not in args:
                raise MemoryError(f"remember 缺少参数 '{key}'")
        return self.memory.add(
            self.state, args["target"], args["fact"], args.get("importance")
        )

    # ------------------------------------------------------------------
    # A3（P1）：反思层——零散记忆 → 关系洞察
    # ------------------------------------------------------------------

    def _reflect(self) -> None:
        """对记忆新增达标的 NPC 合成关系洞察（侧信道：失败静默降级）。

        C-5（m3）：门控条件全部可从存档重建——「该 NPC 记忆中的最新 round 严格大于
        最近一次洞察的 round」才反思，读档后不会对同一批记忆重复合成。
        """
        for npc_id, bucket in self.state.npc_memories.items():
            if len(bucket) < REFLECT_MIN_MEMORIES:
                continue
            newest_memory_round = max(m.round for m in bucket)
            last_insight_round = max(
                (i.round for i in self.state.npc_insights.get(npc_id, [])), default=-1
            )
            if newest_memory_round <= last_insight_round:
                continue  # 无新增记忆（含读档后重入）：不重复反思
            self._reflect_npc(npc_id, bucket)

    def _reflect_npc(self, npc_id: str, bucket: list) -> None:
        name = self.pack.npcs[npc_id].name
        recent = bucket[-REFLECT_MATERIAL:]
        numbered = "\n".join(f"{i + 1}. {m.fact}" for i, m in enumerate(recent))
        existing = "；".join(i.text for i in self.state.npc_insights.get(npc_id, []))
        try:
            output = complete_with_empty_retry(
                self.llm,
                [
                    {"role": "system", "content": REFLECT_SYSTEM},
                    {
                        "role": "user",
                        "content": f"<角色>{name}</角色>\n\n<近期记忆>\n{numbered}\n</近期记忆>"
                        + (f"\n\n<已有洞察>\n{existing}\n</已有洞察>" if existing else ""),
                    },
                ],
                purpose="reflect",
                max_tokens=REFLECT_MAX_TOKENS,
                temperature=0.0,
            )
        except Exception:  # noqa: BLE001
            return  # 反思失败静默：不影响主线
        for text, indices in parse_insights(output):
            sources = tuple(
                recent[i - 1].fact for i in indices if 1 <= i <= len(recent)
            )
            entry = InsightEntry(
                text=text, day=self.state.day, round=self.state.turn_count, sources=sources
            )
            insights = self.state.npc_insights.setdefault(npc_id, [])
            insights.append(entry)
            if len(insights) > INSIGHT_CAP:  # 新洞察替换最旧
                del insights[: len(insights) - INSIGHT_CAP]

    # ------------------------------------------------------------------
    # M2a 迭代4：确定性提取兜底
    # ------------------------------------------------------------------

    def _extract_facts(self) -> None:
        """从最近回合内容强制提炼玩家长期事实（侧信道：失败静默降级，不影响主线）。"""
        recent = self._recent_text()
        if not recent.strip():
            return
        existing = "；".join(m.fact for m in self.state.player_facts)
        user_content = f"已有事实：{existing}\n\n<回合内容>\n{recent}\n</回合内容>"
        try:
            output = complete_with_empty_retry(
                self.llm,
                [
                    {"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
                purpose="extract",
                max_tokens=EXTRACT_MAX_TOKENS,
            )
        except Exception:  # noqa: BLE001
            return  # 提取失败不影响叙事主线
        for fact, importance in parse_facts(output):
            try:
                self.memory.add(self.state, "player", fact, importance)
            except MemoryError:
                continue  # 超长/非法事实直接丢弃

    def _recent_text(self) -> str:
        """最近 8 条历史中的玩家发言与叙事（跳过引擎元消息：name=engine 或 【 前缀）。"""
        lines: list[str] = []
        for m in self.history[-8:]:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if not content or m.get("name") == "engine" or content.startswith("【"):
                continue
            if role == "user":
                lines.append(f"玩家：{content}")
            elif role == "assistant":
                lines.append(f"叙事：{content}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # M2b：压缩 + 语义校验
    # ------------------------------------------------------------------

    def _compress_history(self) -> None:
        """增量压缩：总结上次摘要之后的新增部分，与旧摘要合并，保留近窗。"""
        cut = find_turn_cut(self.history, self.keep_turns)
        if cut <= 0:
            return
        summary_idx = locate_summary(self.history)
        old_start = summary_idx + 1 if summary_idx >= 0 else 0
        new_part = self.history[old_start:cut]
        if not new_part:
            return  # 上次压缩后新增不足，跳过
        existing = (
            str(self.history[summary_idx].get("content") or "").replace(SUMMARY_MARK, "").strip()
            if summary_idx >= 0
            else ""
        )
        try:
            merged = self.llm.complete(
                [
                    {
                        "role": "system",
                        "content": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET),
                    },
                    {
                        "role": "user",
                        "content": f"<旧摘要>\n{existing}\n</旧摘要>\n\n<新增历史>\n"
                        f"{history_text(new_part)}\n</新增历史>",
                    },
                ],
                max_tokens=COMPRESS_MAX_TOKENS,
                purpose="compress",
            )
        except Exception:  # noqa: BLE001
            return  # 压缩失败静默降级：保留原历史，下回合重试
        if not merged.strip():
            return
        rebuilt = rebuild_history(self.history, summary_idx, merged.strip(), cut)
        if not ensure_pairing(rebuilt):
            return  # 兜底：重建后配对不变量不成立则放弃本次压缩
        self.history = rebuilt

    def _judge_turn(self, narration: str) -> None:
        """语义校验：失败则注入下轮修正提示（侧信道，失败静默）。"""
        if not narration:
            return
        materials = self.builder.status_text(
            self.state, self.story.active_node(self.state)
        )
        ok, verdict = self.judge.check(narration, materials)
        if not ok and verdict:
            self.history.append(
                {
                    "role": "user",
                    "name": "engine",  # A-2：引擎元消息，排除出检索上下文
                    "content": (
                        f"【校验反馈】上一轮叙事存在质量问题：{verdict}\n"
                        "请在后续叙事中自然修正，避免重复此类问题。"
                    ),
                }
            )

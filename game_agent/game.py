"""游戏主循环（W7）：把状态/数值/剧情/事件/日程/上下文/LLM 串成可玩回合。

每个叙事回合（_narrate）统一走：
  begin_turn（节点触发/关键选择门）→ LLM 协议闭环 → end_turn（完成判定/卡壳/结局）
  → 条件事件级联（最多 2 层）。
"""

from __future__ import annotations

import difflib
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from .budgets import (
    COMPRESS_MAX_TOKENS,
    EXTRACT_MAX_TOKENS,
    REFLECT_MAX_TOKENS,
    TRUNCATED_FINISH_REASON,
    TURN_MAX_TOKENS,
    complete_checked,
    complete_with_empty_retry,
    is_context_overflow,
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
from .conditions import evaluate
from .context import ContextBuilder, select_lore
from .events import EventSystem
from .factgraph import build_graph, check_graph  # 设计加固 B1：缺席检查常开轮
from .judge import JudgeSystem, recheck_feedback  # 设计加固 B2：反馈复查
from .llm import LLMTurnError, LLMClient
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
    parse_targeted_facts,
    rank_facts,
)
from .planning import PLAN_MAX_TOKENS, PLAN_SYSTEM, parse_steps
from .registry import ToolRegistry, build_registry
from .save import save_game
from .schedule import ScheduleSystem
from .state import Appointment, GameState, InsightEntry
from .stats import StatChangeError, StatsSystem
from .storyline import FREE_INPUT_OPTION, StorylineEngine, filter_choices
from .worldpack import (
    ActionSpec,
    CriticalChoice,
    EndingSpec,
    WorldPack,
    find_location,
    pack_meta,
)


class GameError(Exception):
    """游戏规则错误（如关键抉择期间尝试自由输入）。"""


REPETITION_THRESHOLD = 0.6  # 相邻回合叙事相似度阈值：超过则注入反重复提示（试玩反馈 #3/#4）
REPETITION_WINDOW = 6  # 设计加固 A3：长程重复回看的整轮叙事条数（进程内窗口）
REPETITION_GRAM = 6  # 设计加固 A3：字符 n-gram 长度（中文短语级）
LONG_REPETITION_THRESHOLD = 0.25  # 设计加固 A3：当前叙事被窗口内旧叙事覆盖的 gram 比例上限

# 约定真值（玩家实测缺陷修复）：内容长度与待履行条数上限（防状态栏膨胀）
APPOINTMENT_MAX_LEN = 40
APPOINTMENT_MAX_PENDING = 5

# J 系列：上下文溢出的预算口径（与 compress_threshold 的经验阈值区分开）
OUTPUT_RESERVE_TOKENS = 4096  # 输出预算下限（TURN_MAX_TOKENS 之上再留一点）
CONTEXT_RESERVE_RATIO = 0.10  # 再按窗口比例预留 10% 安全余量（估算本身有误差）


def estimate_context_tokens(history: list[dict], factor: float = 1.0) -> int:
    """一次请求的输入 token 估算 = 历史估算 × 校准因子。

    与 `_llm_round` 的压缩判据**同口径**（单一真源），避免两处各算一套：
    ``factor`` 来自 `TokenCalibrator`（真实 prompt_tokens / 估算），未校准时 1.0。
    取 ``max(factor, 1.0)``：历史估算只覆盖消息文本，漏算 tool schema / 系统前缀开销，
    因此**校准因子小于 1 时也不下调**——估算偏低会让溢出预检形同虚设。
    """
    return int(history_tokens(history) * max(factor, 1.0))


@dataclass
class TurnView:
    """一个叙事回合给玩家看的东西。

    ``recovered`` / ``sub_turns``（J 系列）解决的是**恢复过程对玩家不可见**：

    一个玩家回合内部可能经历多次生成——条件事件级联（最多 2 层）、关键节点判劣重写、
    上下文溢出后的压缩重试、乃至熔断兜底。此前这些对上层只表现为"这轮慢"或
    "本轮生成失败"，前端分不出"发生过恢复并成功"与"压根没试过"。``recovered``
    把用过的恢复手段如实列出（正常回合为空），``sub_turns`` 给出本回合实际跑了几次
    生成——两者一起让 SSE 与报告能说清"这一轮到底发生了什么"。
    """

    narration: str | None  # None = 关键选择待决（只显示固定选项）
    choices: list[str]
    ending: EndingSpec | None = None
    choice_prompt: CriticalChoice | None = None
    briefing: str | None = None  # 关键抉择前的剧情背景（首次展示给玩家）
    recovered: list[str] = field(default_factory=list)  # 本回合用过的恢复手段
    sub_turns: int = 0  # 本回合实际执行了几次生成（含级联/重写/重试）


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
        critique_on_critical: bool = False,  # agent-first 第 2 件：关键节点内轮自校正
        plan_node: bool = False,  # agent-first 第 4 件：节点目标拆子步骤（plan-and-execute）
        factcheck_every: int = 0,  # 设计加固 B1：>0 时每 N 回合独立跑缺席证据检查（0=关闭）
        context_window: int = 0,  # J 系列：模型上下文窗（token，0=未声明→溢出预检关闭）
        mainline_enabled: bool = True,  # E-4：False = 自由游玩（不进入主线节点）
    ):
        self.pack = pack
        # G2：剧本身份戳在构造时算一次（包不可变）——存档与读档校验共用同一份，
        # 避免"存的时候算一次、读的时候再算一次"因调用时机不同而产生假不匹配。
        self.pack_meta = pack_meta(pack)
        self.state = state
        self.llm = llm
        self.rng = rng if rng is not None else random.Random()  # 批次 C：自定义工具结算共用
        self.stats = StatsSystem(pack.schedule)
        # E-4：自由游玩 = 不进入主线节点（已进入的节点状态不动，结局/日程/事件照旧）
        self.mainline_enabled = mainline_enabled
        self.story = StorylineEngine(pack, self.stats, mainline_enabled=mainline_enabled)
        self.events = EventSystem(pack, self.stats, rng)
        self.schedule = ScheduleSystem(pack, self.stats, rng)  # D 系列：检定/收益曲线共用 rng
        self.memory = MemorySystem(pack, llm)  # M2a 记忆显式化 + A4 语义去重（P1）
        self.context_window = context_window  # J 系列：溢出预检的窗口（0=关闭）
        # A-1：builder 按窗口推导 lore 的 token 预算，故窗口必须先于 builder 赋值
        self.builder = ContextBuilder.from_pack(pack, window_tokens=self.context_window)
        # 批次 C：工具注册表——引擎四件套绑定处理器 + 世界包自定义工具
        self.registry = build_registry(pack)
        self.registry.bind_handler("change_stat", self._apply_change)
        self.registry.bind_handler("remember", self._remember)
        self.registry.bind_handler("query_world", self._query_world)
        if pack.schedule.actions:
            self.registry.bind_handler("do_action", self._do_action)
        if pack.schedule.affections:  # 约定真值 + 在场真值（玩家实测缺陷修复）
            self.registry.bind_handler("make_appointment", self._make_appointment)
            self.registry.bind_handler("change_presence", self._change_presence)
        for tool in pack.schedule.tools:
            self.registry.bind_handler(tool.id, lambda args, t=tool: self._run_custom_tool(t, args))
        if pack.world.locations:  # 批次 D：地点表声明时启用 change_scene
            self.registry.bind_handler("change_scene", self._change_scene)
        # 批次审查修复：老档 + 新增 counters 的包升级路径——缺键按包声明补初始值
        # （否则 DSL 求值会误报"未声明的计数器"）。items 不回填（无法区分"从未有"与"已失去"）。
        for key, spec in pack.schedule.counters.items():
            self.state.counters.setdefault(key, float(spec.initial))
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
        self.critique_on_critical = critique_on_critical  # agent-first 第 2 件
        self.plan_node = plan_node  # agent-first 第 4 件
        self.factcheck_every = factcheck_every  # 设计加固 B1：缺席检查常开轮
        self.judge = JudgeSystem(llm)  # M2b
        # 设计加固 A3/B2 的进程内状态（不落盘：读档后退化为改前行为，无正确性影响）
        self._narration_window: list[str] = []  # 长程反重复回看窗口
        self._pending_verdict: str | None = None  # 待复查的校验反馈（B2 闭环）
        self._meltdown_round = False  # A5：本轮含熔断兜底（其文案不进反重复窗口）
        # J 系列：上下文溢出恢复状态（进程内；读档后退化为改前行为，无正确性影响）。
        # "本回合是否溢出"由 LLMClient.last_overflow 单点记录（run_turn 每回合重置），
        # 这里只保留"本回合是否已用过压缩重试"。
        self._overflow_retry_used = False
        # J 系列：本回合的恢复痕迹与子回合计数（在 `_narrate` 开头重置、跨级联累加，
        # 随 TurnView 返回给 SSE / 报告）。顺序即发生顺序，重复时只记第一次。
        self._recovered: list[str] = []
        self._sub_turns = 0

    def _note_recovery(self, kind: str) -> None:
        """登记一次"本轮内部发生过恢复"（去重、保序）。

        同时写进 `llm.recovered`：`LLMClient` 把它附在 `turn_begin` 事件上，于是 trace
        能看出**每一次生成**的来由（正常 / 判劣重写 / 溢出重试），而不只是"同一个
        玩家回合跑了几次生成"。
        """
        if kind not in self._recovered:
            self._recovered.append(kind)
        self.llm.recovered = list(self._recovered)

    def _turn_fields(self) -> dict:
        """构造 TurnView 的恢复痕迹字段（所有 TurnView 出口共用同一口径）。"""
        return {"recovered": list(self._recovered), "sub_turns": self._sub_turns}

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
        """执行日程行动：结算（门槛/检定/效果）→ 日程事件检查 → 叙事。

        关键抉择期间拒绝（C2，与 say() 同守卫）——否则效果会被静默结算、
        行动点被扣，而剧情视图纹丝不动（玩家实测困惑定位）。
        """
        if self.story.choice_locked(self.state):
            raise GameError("此刻是关键抉择，只能从固定选项中选择")
        # 事务边界：快照必须取在**结算之前**——日程结算/时间事件在进入 _narrate 前就
        # 落盘了，不先取快照就出了撤销范围（熔断会白扣行动点、白推日期）。
        snapshot = self._txn_snapshot()
        action = self.schedule.action_by_id(action_id)
        outcome = self.schedule.execute_action(self.state, action_id)
        lines = [
            f"（玩家选择日程行动：{action.label}）",
            # 玩家反馈（衔接突兀）：从当前情境自然过渡到行动——辞别在场人物、
            # 交代赶路，再进入行动场景；不要让对话凭空中断
            "（衔接要求：先从当前情境自然过渡——如需离开当前场景或辞别在场人物，"
            "用一两句交代，再叙述本次行动的过程与结果）",
        ]
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
        return self._narrate("\n".join(lines), snapshot=snapshot)

    def say(self, text: str) -> TurnView:
        """玩家自由输入（或点日常选项）。关键抉择期间拒绝。"""
        if self.story.choice_locked(self.state):
            raise GameError("此刻是关键抉择，只能从固定选项中选择")
        return self._narrate(text)

    def pick(self, option_index: int) -> TurnView:
        """关键抉择：选择固定选项并叙述后果。

        事务边界：快照取在 `choose_option` **之前**——该调用会应用选项效果、
        写 choice_log、清 pending_choice。不先取快照，熔断后这个抉择就被永久消费
        （玩家永久失去一个分叉，且无法重做）。

        历史写入：只在这里 append 一次。`choose_option` 返回的消息**既**要进历史
        （后续回合要看到玩家选了什么），又要作为 `_narrate` 的 prompt 去驱动叙事
        ——但 `_narrate` 在无后续抉择时也会 append prompt，于是旧写法把它写了两遍：
        每个节点的**最后一个**关键抉择都会在历史里出现两次。后果不只是冗余：
        `_recent_player_text(n=2)` 的两个名额被同一条输入吃掉（A1 检索相关性变差），
        `find_turn_cut` 也把它当成两个回合锚点（近窗实际覆盖的真实回合变少）。
        """
        snapshot = self._txn_snapshot()
        msg = self.story.choose_option(self.state, option_index)
        self.history.append(msg)
        # 传 prompt 供叙事驱动，但历史已由上面 append —— 用 skip_append 避免二次写入
        return self._narrate(msg["content"], snapshot=snapshot, skip_append=True)

    def end_day(self) -> TurnView:
        """结束今天：推进日期 → 时间事件 → **叙事化过渡**（玩家反馈修复）。

        此前只返回 "—— 第 N 天 ——" 的裸标记：Web 故事框直接空掉，
        玩家看到的是"点了按钮、剧情死了"。现在跨天是一次正常叙事回合——
        【时序推进】是引擎元消息（name=engine，A-2 纪律：不进检索上下文），
        LLM 把"一天结束了"写成衔接性短场景（辞别/夜宿/次日清晨），
        当日到期的时间事件消息随本轮生效。返回 TurnView（narration 含日期
        标记；若新节点带关键抉择则接管为固定选项视图）。
        关键抉择期间拒绝（与 say/act 同守卫：抉择没做完，今天不该翻篇）。
        """
        if self.story.choice_locked(self.state):
            raise GameError("此刻是关键抉择，只能从固定选项中选择")
        # 事务边界：快照取在推进日期**之前**——否则熔断会白过一天（日期与时间事件
        # 都已落盘，玩家却什么也没看到）。
        snapshot = self._txn_snapshot()
        self.schedule.end_day(self.state)
        for ev in self.events.check_time_events(self.state):
            self.history.append(self.events.trigger(self.state, ev))
        self.history.append(
            {
                "role": "user",
                "name": "engine",  # A-2：时序推进是引擎元消息，排除出检索上下文
                "origin": "engine",  # 血缘标记：本条由引擎注入，非模型/玩家产出
                "content": (
                    "【时序推进】今天结束了。请用两三句收束今日：告别在场人物、"
                    "交代入夜或歇息，再以次日清晨的到来自然收尾。"
                    "除非上方另有【事件】消息，不要发起新情节，篇幅简短。"
                ),
            }
        )
        view = self._narrate(snapshot=snapshot)
        marker = f"—— 第 {self.state.day} 天 ——"
        if view.narration:
            view.narration = marker + "\n\n" + view.narration
        else:
            view.narration = marker  # 关键抉择/事件接管轮：仅标记日期
        return view

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

    def _narrate(
        self,
        prompt: str | None = None,
        snapshot: tuple[GameState, list[dict]] | None = None,
        skip_append: bool = False,
    ) -> TurnView:
        # J 系列：恢复痕迹与子回合计数按**玩家回合**清零——一个 `_narrate` 可能跑
        # 1~3 次 `_llm_round`（主回合 + 条件事件级联），计数要跨级联累加而不是每轮重置。
        self._recovered = []
        self._sub_turns = 0
        self.llm.recovered = []  # K 系列：trace 的 turn_begin 也按玩家回合清零
        node, node_msgs = self.story.begin_turn(self.state)
        self.history.extend(node_msgs)
        if node is not None and self.plan_node:
            self._ensure_plan(node)  # agent-first 第 4 件：新节点进入 → 生成子步骤计划
        choice = self.story.pending_choice(self.state)
        if choice is not None:
            # 关键抉择前把节点剧情背景带给玩家（修复"上来就是选项"体验问题）
            briefing = node.on_enter.briefing if node is not None else None
            return TurnView(
                narration=None,
                choices=[o.text for o in choice.options],
                choice_prompt=choice,
                briefing=briefing,
                **self._turn_fields(),
            )
        if prompt is not None and not skip_append:
            # origin="player"：本条是玩家输入（对比 name="engine" 的引擎注入）。
            # 血缘标记让 history 自描述——判官材料 / 评测导出 / trace 回放都不必再靠
            # "有没有 name 字段"这类间接特征反推消息来源。
            self.history.append({"role": "user", "origin": "player", "content": prompt})

        views: list[TurnView] = []
        meltdown_seen = False  # 批次审查修复：任一级联轮熔断 → 汇总文案不进反重复窗口
        for _ in range(3):  # 1 个主回合 + 最多 2 个条件事件级联
            view = self._llm_round(snapshot=snapshot)
            snapshot = None  # 快照只对**第一轮**有意义：它是本回合全部结算的撤销点
            views.append(view)
            meltdown_seen = meltdown_seen or self._meltdown_round
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
        if narration and not meltdown_seen:  # A5：熔断兜底文案不进反重复窗口/last_narration
            self._check_repetition(narration)
        # **出口统一以引擎状态为准推导"有没有待决抉择"**（2026-10 修）。
        #
        # 此前这里只搬 narration/choices/ending，`choice_prompt` 被丢掉。而
        # `state.pending_choice` 可能在**本轮之内**被设置——`end_turn` 完成当前节点后
        # 会去找下一个满足 `when` 的节点，新节点带 `critical_choices` 时就把锁挂上了。
        # 于是视图说"没有选项"，引擎说"只能选固定选项"：
        # `say` / `act` / `end_day` 全抛 GameError，而**唯一的出路 `pick(i)` 在界面上
        # 没有按钮可点**——玩家彻底卡死，只能读档。
        #
        # 真机撞到两次：一次是生成卡的 `pick()` 熔断（回滚把 `pending_choice` 还原），
        # 一次是生成卡**同一个节点被反复重入**（`when: {all: []}` 恒真 → 完成后立刻
        # 又满足进入条件 → 又挂上一个待决抉择）。
        #
        # 修在这儿而不是逐个出口去补：`_narrate` 是**所有**回合的唯一汇聚点，
        # 而"视图与引擎状态不许自相矛盾"是这条链上唯一的真判据。判据与
        # `storyline.pending_choice` 同源（不另存一份）。
        choice = self.story.pending_choice(self.state)
        return TurnView(
            narration=narration or None,
            choices=[o.text for o in choice.options] if choice is not None else last.choices,
            ending=last.ending,
            choice_prompt=choice,
            **self._turn_fields(),
        )

    def _check_repetition(self, narration: str) -> None:
        """反重复双层检测（设计加固 A3）：

        - 相邻轮相似度（原路径，试玩反馈 #3/#4）；
        - 滑动窗口 n-gram 覆盖率（长程复读机：隔多轮重复同一桥段）；
        相邻轮命中时跳过长程检查（一轮至多一条提示）。窗口为进程内状态，
        读档重置——与 last_narration 同生命周期。
        """
        adjacent_hit = False
        if self.last_narration:
            ratio = difflib.SequenceMatcher(None, self.last_narration, narration).ratio()
            adjacent_hit = ratio >= REPETITION_THRESHOLD
            if adjacent_hit:
                self.history.append(
                    {
                        "role": "user",
                        "name": "engine",
                        "origin": "engine",  # 血缘标记：本条由引擎注入，非模型/玩家产出
                        "content": (
                            f"[反重复提示] 本轮叙事与上一轮高度重复（相似度 {ratio:.0%}）。"
                            "请避免复述已经写过的场景与对话，改为推进新情节：新的事件、新的细节、"
                            "人物关系的新变化。"
                        ),
                    }
                )
        self.last_narration = narration
        if not adjacent_hit:
            overlap = self._long_repetition_overlap(narration)
            if overlap is not None:
                self.history.append(
                    {
                        "role": "user",
                    "name": "engine",
                    "origin": "engine",  # 血缘标记：本条由引擎注入，非模型/玩家产出
                        "content": (
                            f"[反重复提示] 本轮叙事与更早回合高度重复（内容覆盖 {overlap:.0%}）。"
                            "请避免复用已写过的情节与表达，改为推进新的事件与发展。"
                        ),
                    }
                )
        self._narration_window.append(narration)
        if len(self._narration_window) > REPETITION_WINDOW:
            del self._narration_window[: len(self._narration_window) - REPETITION_WINDOW]

    def _long_repetition_overlap(self, narration: str) -> float | None:
        """当前叙事 vs 窗口内旧叙事的最大 n-gram 覆盖率；超阈值返回覆盖率，否则 None。"""
        grams = self._grams(narration)
        if not grams:
            return None
        best = 0.0
        for prev in self._narration_window:
            if not prev:
                continue
            prev_grams = self._grams(prev)
            if not prev_grams:
                continue
            overlap = sum(1 for g in grams if g in prev_grams) / len(grams)
            best = max(best, overlap)
        return best if best >= LONG_REPETITION_THRESHOLD else None

    @staticmethod
    def _grams(text: str) -> set[str]:
        """字符 n-gram 集合（空白归一；短于 gram 长度的文本返回空集）。"""
        compact = re.sub(r"\s+", "", text)
        n = REPETITION_GRAM
        return {compact[i : i + n] for i in range(len(compact) - n + 1)}

    def _llm_round(
        self, snapshot: tuple[GameState, list[dict]] | None = None
    ) -> TurnView:
        # K 系列：**先**钉住本回合的时间轴标签，再干活。放在最前面是因为紧随其后的
        # 压缩预检会发起侧信道 LLM 调用——若等到 `turn_count += 1` 之后再设，那次
        # 调用的成本会被归到**上一个**玩家回合（它是为这一轮腾上下文而花的）。
        # 此刻 `turn_count` 尚未自增，故记为 +1 = 本次即将开始的回合号。
        self.llm.game_turn = self.state.turn_count + 1
        # M2b 压缩：接近阈值时批量压缩（只碰历史，不碰静态前缀与事实区块）。
        # 批次 F：估算乘以真实 usage 校准出的因子（未校准 = 1.0，行为不变）——
        # "1 字 ≈ 1 token" 漏算 tool schema/system 开销，实测 prompt 恒高于估算。
        # J 系列：真正会**发不出去**的红线是模型上下文窗（含输出预算），它通常
        # 严于 compress_threshold——两个判据都查，任一命中即压缩。
        if self._budget_exceeded(self.compress_threshold) or self._budget_exceeded(
            self._context_budget()
        ):
            self._compress_history()
        # 记忆来源追踪（M2a）：每**玩家可见回合**计一次——内轮自校正的重生成不另计
        self.state.turn_count += 1
        self._meltdown_round = False
        self._overflow_retry_used = False  # J 系列：压缩重试每回合只允许一次
        self._sub_turns += 1  # J 系列：本回合又跑了一次生成（级联/重写/重试都会到这）
        critical = self.critique_on_critical and self._in_critical_node()
        # 关键节点不流式：先缓冲，自校正通过后再一次性回放（坏稿不能让玩家先看到）
        pre_history = list(self.history) if critical else None  # 第一稿前的前缀（剥稿用）
        # 回合事务：快照取在 turn_count 自增之后、生成之前——agent 在生成期间经工具
        # 落盘的一切（数值/记忆/行动点/场景/事件）都落在快照覆盖内。叙事被接受才提交；
        # 熔断或重生成丢弃时整批撤销。
        #
        # 传入 snapshot = 调用方（act / pick / end_day）在**结算之前**已取的快照：
        # 那三种操作在进入本函数前就把日程结算 / 选项效果 / 日期推进落了盘，
        # 自带快照才能把它们一并纳入撤销范围（否则熔断会白扣行动点、吃掉关键抉择）。
        if snapshot is None:
            snapshot = self._txn_snapshot()
        try:
            result = self._generate_turn(stream=not critical)
            if critical:
                result = self._critique_and_regenerate(result, pre_history, snapshot)
                if result.narration and self.on_text is not None:
                    self.on_text(result.narration)  # 缓冲后一次性回放（CLI 去重逻辑兼容）
        except LLMTurnError:
            # J 系列：熔断**之前**先分辨"上下文溢出"与"模型不守协议"。前者是确定性的
            # 上下文问题（重试同一个超长上下文只会再被拒三次），压缩一次后原地重试的
            # 成功率远高于整轮回滚；后者才交给熔断兜底。
            if self.llm.last_overflow and not self._overflow_retry_used:
                self.llm._trace(
                    "overflow", game_turn=self.state.turn_count, phase="detected",
                    reason="provider 以输入过长拒绝请求",
                )
                result = self._overflow_retry(reason="provider_reject")
                if result is not None and result.narration:
                    critical = self.critique_on_critical and self._in_critical_node()
                    if critical:
                        result = self._critique_and_regenerate(result, pre_history, snapshot)
                        if result.narration and self.on_text is not None:
                            self.on_text(result.narration)
                else:
                    # 压缩不动 / 重试仍熔断 → 原样走熔断口径（撤销 + 保守回合）
                    self._txn_rollback(snapshot, reason="meltdown")
                    return self._meltdown_fallback()
            else:
                # 设计加固 A5（design §10.3 落地）：熔断炸的是"本轮"不是会话——
                # 保守回合兜底，玩家可继续。熔断时 run_turn 的协议重试消息未同步进
                # history（同步只在成功返回后发生）。
                # 回合事务：熔断前各次失败迭代**已经落盘**的工具效果一并撤销——玩家
                # 看到"本轮跳过"，真值就必须纹丝不动（此前会凭空涨数值/扣行动点）。
                self._txn_rollback(snapshot, reason="meltdown")
                return self._meltdown_fallback()
        except Exception:
            # J 系列：溢出是 **API 层异常**（provider 400 拒绝请求），不是 LLMTurnError，
            # 因此默认会一路逃出 `_llm_round` 打断整个回合。只有在确认"这就是输入过长"
            # （LLMClient 分类过）时才接管：压缩一次、原地重试。
            #
            # 非溢出异常**原样抛出**（保持改前的传播行为）：真实故障（鉴权/网络/配额）
            # 必须让调用方看见，不能被这里悄悄降级成一个保守回合。
            if not (self.llm.last_overflow and not self._overflow_retry_used):
                raise
            self.llm._trace(
                "overflow", game_turn=self.state.turn_count, phase="detected",
                reason="provider 以输入过长拒绝请求",
            )
            result = self._overflow_retry(reason="provider_reject")
            if result is None or not result.narration:
                # 压缩不动 / 重试也失败 → 与熔断同口径（撤销本轮效果 + 保守回合），
                # 而不是把异常抛给玩家（溢出是可预期状况，不该表现为崩溃）。
                self._txn_rollback(snapshot, reason="overflow_recovery_failed")
                return self._meltdown_fallback()
            critical = self.critique_on_critical and self._in_critical_node()
            if critical:
                result = self._critique_and_regenerate(result, pre_history, snapshot)
                if result.narration and self.on_text is not None:
                    self.on_text(result.narration)
        # 空叙事 = 本轮无可交付内容 → 效果同样不作数（契约：叙事被接受 ⟺ 效果生效）。
        # 这条守卫此前只写在 `_critique_and_regenerate`（关键节点路径）里，于是**日常
        # 回合**上被 clean_narration 洗成空的叙事会被接受、工具效果照常提交——
        # 玩家看到空空的故事框，数值却涨了。上移到所有路径的交汇处。
        if not result.narration:
            self._txn_rollback(snapshot, reason="empty_narration")
            return TurnView(narration=None, choices=result.choices, **self._turn_fields())
        outcome = self.story.end_turn(self.state, result.plot_signal)
        self.history.extend(outcome.messages)
        self._maybe_replan(outcome.messages)  # 设计加固 A4：卡壳推进提示触发重规划
        # 节点完成 → 自动存档（W-C：长局防丢进度，引擎侧钩子；含对话历史）
        if outcome.node_completed is not None and self.autosave_path is not None:
            save_game(self.state, self.autosave_path, self.history, pack_meta=self.pack_meta)
        # 设计加固 B2：先复查上一轮校验反馈是否已修正，再做本轮检查
        if self._pending_verdict and result.narration:
            self._recheck_feedback(result.narration)
        # M2a 迭代4：确定性提取兜底（弥补 remember 主动性的覆盖缺口）
        if self.extract_every > 0 and self.state.turn_count % self.extract_every == 0:
            self._extract_facts()
        # 设计加固 B1：缺席证据检查（确定性层）常开，LLM judge 降频采样——
        # judge 采样轮跳过独立检查（judge.check 内含同一检查，避免同轮两次抽取）
        judge_runs = self.judge_every > 0 and self.state.turn_count % self.judge_every == 0
        if (
            self.factcheck_every > 0
            and not judge_runs
            and self.state.turn_count % self.factcheck_every == 0
            and result.narration
        ):
            self._factcheck_turn(result.narration)
        # M2b 语义校验：每 N 回合检查最新叙事，失败注入下轮修正提示
        if judge_runs:
            self._judge_turn(result.narration)
        # A3 反思：每 N 回合对记忆增长达标的 NPC 合成关系洞察（侧信道，失败静默）
        if self.reflect_every > 0 and self.state.turn_count % self.reflect_every == 0:
            self._reflect()
        return TurnView(
            narration=result.narration,
            choices=filter_choices(self.pack, result.choices),
            ending=outcome.ending,
            **self._turn_fields(),
        )

    def _meltdown_fallback(self) -> TurnView:
        """A5：协议熔断的保守回合。文案是引擎中性文案（F1：引擎层不含内容）。"""
        self._meltdown_round = True
        self._note_recovery("meltdown")  # J 系列：本回合以熔断兜底收场（玩家需知情）
        self.history.append(
            {
                "role": "user",
                "name": "engine",
                "origin": "engine",  # 血缘标记：本条由引擎注入，非模型/玩家产出
                "content": "[引擎熔断] 本轮生成多次未达协议，已放弃本轮。",
            }
        )
        # **关键抉择期熔断：视图必须把选项带回去**（2026-10 修）。
        #
        # `pick()` 的事务快照取在 `choose_option` 之前，所以熔断回滚会把
        # `state.pending_choice` **还原**（设计如此：熔断不该吃掉一个分叉）。此时
        # 引擎仍然锁着输入（say/act/end_day 全抛 GameError），而这里原先造的视图
        # **不带 `choice_prompt`** ——于是玩家看到的是"没有固定选项可点 + 说什么都被拒"
        # 的**卡死态**：唯一的出路 `pick(i)` 在界面上根本不存在。
        #
        # 真机跑生成卡时撞到的就是这个（`worldpack_smoke` 的 pick 熔断 → 下一轮
        # act 抛 GameError）。判据与 `storyline.pending_choice` 同源，不另存一份。
        choice = self.story.pending_choice(self.state)
        return TurnView(
            narration=(
                "（本轮生成失败，已跳过——请**重新选择**。）"
                if choice is not None
                else "（本轮生成失败，已跳过——请换个说法或行动再试，或读档重来。）"
            ),
            choices=self.last_choices or [FREE_INPUT_OPTION],
            choice_prompt=choice,
            **self._turn_fields(),
        )

    # ------------------------------------------------------------------
    # 回合事务边界
    # ------------------------------------------------------------------

    def _txn_snapshot(self) -> tuple[GameState, list[dict], object | None]:
        """回合事务快照 = 真值状态（深拷贝）+ 对话历史（浅拷贝）+ **随机流**。

        为什么连随机流一起快照：检定掷骰、`chance` 日程事件、`{base, spread}` 收益曲线
        都吃 `self.rng`，而它们可能在**回合中途**被消耗（如 `_do_action` 的结算）。
        不回滚 rng，重试/重生成的一轮就会掷出不同的骰子——同一个回合两次运行结果不同，
        事务的"撤销"就不彻底（真值回去了，随机流没回去）。

        rng 缺 `getstate` 时记 None（鸭子类型兜底）：`Game` 只要求 rng 提供
        `random()` / `uniform()`，测试替身与自定义 rng 不必实现完整 `random.Random` 接口
        ——这类 rng 退化为"不回滚随机流"，不报错。
        """
        try:
            rng_state = self.rng.getstate()
        except AttributeError:
            rng_state = None
        return (self.state.copy(), list(self.history), rng_state)

    def _txn_rollback(self, snapshot: tuple[GameState, list[dict], object], reason: str = "unknown") -> None:
        """撤销到快照：本轮 agent 经工具落盘的一切效果作废。

        契约：**叙事被接受 ⟺ 效果生效**。此前工具的写入是"即发即落"的，而叙事要等到
        整轮收尾才算数——两者事务边界不一致，于是判劣重写会重复结算（两稿各提议一次
        就落两次）、熔断会凭空涨数值（失败迭代的效果留在真值里）。

        为什么必须连历史一起还原：`_do_action` 结算日程事件时会**直接**
        `self.history.append`（见 _do_action），只还原 GameState 会留下一条指涉
        已撤销效果的悬空消息。

        为什么状态是**原地**还原而不是 rebind：`Game.__init__` 的 state 参数被调用方
        （CLI / Web / 测试 / 存档）长期持有，rebind 会让这些外部引用变成悬空旧对象。

        **必须记 trace**：工具事件是**派发时**记录的（llm.py 在 dispatch 处记
        `tool ... status=ok`），而回滚发生在收尾——不记这条，trace 会宣称一轮里
        "武功 5→10→15→20 改了三次"，而真值纹丝未动。观测层不能自相矛盾。
        """
        state, history, rng_state = snapshot
        self.state.__dict__.update(state.__dict__)
        self.history = history
        if rng_state is not None:  # 鸭子类型 rng（无 getstate）退化为不回滚随机流
            self.rng.setstate(rng_state)
        self.llm._trace(
            "txn_rollback",
            game_turn=self.state.turn_count,
            reason=reason,
            note="本轮工具效果与历史一并作废（此前记录的 tool 事件不代表真值变更）",
        )

    def _maybe_replan(self, messages: list[dict]) -> None:
        """A4：卡壳保护注入推进提示的回合重规划——旧计划可能已偏离实际剧情。

        批次审查修复：作者手写 steps 的节点回落手写步骤（与 storyline
        "作者手写优先"口径一致），仅无手写步骤才走侧信道生成。
        失败静默降级 = 无计划（与首生成同口径）。快照刷新到当前 flags、指针归零。
        """
        if not self.plan_node:
            return
        if not any("【推进提示】" in (m.get("content") or "") for m in messages):
            return
        node = self.story.active_node(self.state)
        if node is None:
            return
        self.state.node_plan_step = 0
        self.state.node_flags_snapshot = dict(self.state.flags)
        if node.steps:
            self.state.node_plan = list(node.steps)
            return
        self.state.node_plan = []
        self._ensure_plan(node)

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

    def _query_world(self, args: dict) -> str:
        """query_world 工具回调（agent-first 第一件）：**只读**查询当前世界状态。

        纪律：
        - 绝不写入任何状态（守卫测试用 state.copy() 前后对拍钉住）；
        - 不返回 flags 原值——剧情旗标是引擎内部真值，泄漏即破坏"玩家只能靠剧情
          感知"的边界（状态栏本来也不带 flags，此处同口径）；
        - 检索复用 rank_facts / select_lore（与状态栏注入同口径；A1 v2 起为 BM25）。
        """
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query 不能为空")
        if len(query) > 200:
            raise ValueError("query 过长（≤200 字）")

        lines: list[str] = []
        present = "、".join(
            self.pack.npcs[i].name for i in self.state.present_npcs if i in self.pack.npcs
        )
        lines.append(
            f"地点：{self.state.scene} · 第 {self.state.day} 天 · 剩余行动点 {self.state.action_points_left}"
        )
        lines.append(f"在场：{present or '无'}")
        stats_str = " · ".join(
            f"{self.pack.schedule.stats[k].label} {v:g}"
            for k, v in self.state.stats.items()
        )
        lines.append(f"玩家属性：{stats_str}")
        if self.state.affections:
            aff_str = " · ".join(
                f"{self.pack.npcs[k].name} {v:g}"
                for k, v in self.state.affections.items() if k in self.pack.npcs
            )
            lines.append(f"好感：{aff_str}")
        if self.state.counters:  # 批次 E：query_world 同状态栏口径
            counters_str = " · ".join(
                f"{self.pack.schedule.counters[k].label} {v:g}"
                for k, v in self.state.counters.items()
                if k in self.pack.schedule.counters
            )
            lines.append(f"计数：{counters_str}")
        held = [i.label for i in self.pack.schedule.items if i.id in self.state.items]
        if held:
            lines.append("持有：" + "、".join(held))
        node = self.story.active_node(self.state)
        lines.append(
            f"主线目标：{node.goal if node is not None else '自由探索，等待主线事件'}"
        )
        # 约定：与状态栏同口径（常驻展示，不走检索——到期日靠检索必然落空）
        appt_block = self.builder.appointments_text(self.state)
        if appt_block:
            lines.append(appt_block)
        if node is not None and self.state.node_plan:
            for i, step in enumerate(self.state.node_plan):
                mark = (
                    "已完成" if i < self.state.node_plan_step
                    else "进行中" if i == self.state.node_plan_step else "待做"
                )
                lines.append(f"步骤（{mark}）：{i + 1}. {step}")
        # 检索区：与查询相关的事实/记忆/lore（输出上限收紧：工具结果直接进模型上下文）
        if self.state.player_facts:
            facts = rank_facts(
                self.state.player_facts, query, self.state.turn_count, k=5, pinned=3
            )
            lines.append("相关关键事实：\n" + "\n".join(f"- {m.fact}" for m in facts))
        for npc_id in self.state.present_npcs:
            entries = self.state.npc_memories.get(npc_id, [])
            if not entries:
                continue
            ranked = rank_facts(entries, query, self.state.turn_count, k=5, pinned=2)
            name = self.pack.npcs[npc_id].name
            lines.append(f"{name}的相关记忆：\n" + "\n".join(f"- {m.fact}" for m in ranked))
        lore = select_lore(self.pack.world.lore, query)
        if lore:
            lines.append("相关设定：\n" + "\n".join(f"- {e.text}" for e in lore))
        actions = self.actions_available()
        if actions:
            lines.append(
                "可选行动：" + "；".join(f"{a.label}（{a.cost} 行动点）" for a in actions)
            )
        return "\n".join(lines)

    def _run_custom_tool(self, tool, args: dict) -> str:
        """批次 C：世界包自定义效果型工具（schedule.tools）的执行器。

        纪律与 change_stat 同构：门槛（requires）→ 引擎结算效果（apply_effects，
        饱和语义）→ 扣行动点（审查修复：结算成功才扣，效果拒绝不白扣）→
        once 记账。返回结果文本供叙事引用；拒绝走 ValueError → 结构化回传。
        """
        state = self.state
        if tool.once and tool.id in state.used_custom_tools:
            raise ValueError(f"{tool.label} 已经用过，不能再使用")
        if tool.requires is not None and not evaluate(tool.requires, state):
            raise ValueError(f"当前条件不满足，无法执行「{tool.label}」")
        if tool.cost > 0 and state.action_points_left < tool.cost:
            raise ValueError(
                f"行动点不足：「{tool.label}」需要 {tool.cost} 点，"
                f"今日剩余 {state.action_points_left} 点"
            )
        notes = self.stats.apply_effects(state, tool.effects, rng=self.rng)
        if tool.cost > 0:
            state.action_points_left -= tool.cost
        if tool.once:
            state.used_custom_tools.append(tool.id)
        note_str = "；".join(notes) if notes else "无实际数值变化"
        return f"（{tool.label}：{note_str}）"

    def _do_action(self, args: dict) -> str:
        """对话中发起的日程行动（玩家反馈：对话与按钮殊途同归）。

        在 run_turn 中途被模型调用——**只做确定性结算，不做叙事**（叙事由模型
        在本轮 submit_narration 中织入）。与日程按钮（game.act）共用同一结算核
        （schedule.execute_action：行动点/门槛/检定/收益曲线），日程触发事件
        的脚本消息进历史（后续回合可见），并附在结果文本里供本轮即时织入。
        """
        if self.story.choice_locked(self.state):
            raise ValueError("此刻是关键抉择，只能从固定选项中选择")
        action_id = str(args.get("action", "")).strip()
        reason = str(args.get("reason", "")).strip()
        if not reason:
            raise ValueError("reason 不能为空：必须附玩家表达的意图要点")
        action = self.schedule.action_by_id(action_id)  # 未知行动 → ScheduleError → 结构化拒绝
        if self.state.action_points_left < action.cost:
            raise ValueError(
                f"行动点不足：「{action.label}」需要 {action.cost} 点，"
                f"今日剩余 {self.state.action_points_left} 点"
            )
        outcome = self.schedule.execute_action(self.state, action_id)
        lines = [f"（玩家在对话中发起日程行动：{action.label}；意图：{reason}）"]
        if outcome.check is not None:
            c = outcome.check
            stat_label = self.pack.schedule.stats[c.stat].label
            lines.append(
                f"【行动检定】{stat_label} {c.value:g} · 掷 {c.roll:.1f}"
                f"（难度 {c.difficulty:g}，大成功需 ≥{c.difficulty + c.margin:g}）"
                f"· 结果：{c.tier_cn}"
            )
        if outcome.notes:
            lines.append(f"【行动效果】{'；'.join(outcome.notes)}")
        ev = self.events.check_schedule_event(self.state, action_id)
        if ev is not None:
            ev_msg = self.events.trigger(self.state, ev)
            self.history.append(ev_msg)
            lines.append(f"【日程事件】{ev_msg['content']}")
        lines.append("（请把以上检定与效果自然织入本轮叙事，不要原样罗列）")
        return "\n".join(lines)

    def _make_appointment(self, args: dict) -> str:
        """约定写入（玩家实测缺陷修复）：模型提议 → 引擎校验 → 落盘为一等真值。

        为什么值得一个工具：约定是**有时限的承诺**，落进记忆池会被检索漏掉且没有
        到期概念，导致 NPC 反复重问已约好的事。落成状态真值后由状态栏无条件常驻注入。

        校验纪律（与 change_stat/remember 同构）：
        - NPC 必须在好感声明表内（白名单，防编造角色）；
        - 日期必须**晚于今天**（过去的"约定"无意义，且会立刻显示为逾期）；
        - 内容非空且 ≤ APPOINTMENT_MAX_LEN；
        - 同一 NPC 同一事由不得重复落盘（防模型每轮重复约定）；
        - 待履行条数上限（防状态栏被约定撑爆）；
        - 关键抉择期间锁定（与 do_action/change_scene 同守卫）。
        """
        if self.story.choice_locked(self.state):
            raise ValueError("此刻是关键抉择，只能从固定选项中选择")
        npc_id = str(args.get("npc", "")).strip()
        what = str(args.get("what", "")).strip()
        raw_day = args.get("due_day")
        if npc_id not in self.pack.schedule.affections:
            raise ValueError(f"未知的约定对象 '{npc_id}'：只能是已声明好感的 NPC")
        if not what:
            raise ValueError("what 不能为空：必须写明约定内容")
        if len(what) > APPOINTMENT_MAX_LEN:
            raise ValueError(f"what 过长（{len(what)} 字，上限 {APPOINTMENT_MAX_LEN} 字）")
        if isinstance(raw_day, bool) or not isinstance(raw_day, int):
            raise ValueError(f"due_day 必须是整数天数，当前为 {raw_day!r}")
        if raw_day <= self.state.day:
            raise ValueError(
                f"约定日期必须晚于今天（第 {self.state.day} 天），当前为第 {raw_day} 天"
            )
        pending = [a for a in self.state.appointments if a.status == "pending"]
        for a in pending:
            if a.with_npc == npc_id and a.what == what:
                raise ValueError(
                    f"已存在同一约定（与{self.pack.npcs[npc_id].name}「{what}」，"
                    f"第 {a.due_day} 天）——不必重复记录"
                )
        if len(pending) >= APPOINTMENT_MAX_PENDING:
            raise ValueError(
                f"待履行约定已达上限 {APPOINTMENT_MAX_PENDING} 条：请先用已有约定推进剧情"
            )
        appt = Appointment(
            id=f"appt_{self.state.turn_count}_{len(self.state.appointments)}",
            with_npc=npc_id,
            what=what,
            due_day=raw_day,
            made_day=self.state.day,
        )
        self.state.appointments.append(appt)
        name = self.pack.npcs[npc_id].name
        return (
            f"（已记下约定：与{name}于第 {raw_day} 天{what}。"
            "此后每轮状态栏的 <约定> 都会带着它，不必重复确认。）"
        )

    def _change_presence(self, args: dict) -> str:
        """在场增减（在场真值修复）：模型提议 → 引擎校验 → 落盘真值。

        为什么需要它：`present_npcs` 此前只在进节点/节点结束/执行行动三处被写，
        **对话回合不动它**，模型也无法改变在场。于是叙事里角色离场后引擎仍注入其
        角色卡与记忆（白烧上下文），角色登场时引擎又漏掉他的卡；
        更根本的是"谁见证了这件事"这个信号不存在，NPC 记忆归因无从谈起。

        校验纪律（与 change_scene / make_appointment 同构）：
        - id 白名单（枚举已限，但仍防手写调用）；
        - 同一人不得同时进出（语义矛盾，拒绝而不是悄悄取其一）；
        - enter/leave 幂等（退场不在场的人不报错、重复入场不产生重复条目）；
        - 关键抉择期间锁定（在场由节点 on_enter 接管）。
        """
        if self.story.choice_locked(self.state):
            raise ValueError("此刻是关键抉择，只能从固定选项中选择")
        reason = str(args.get("reason", "")).strip()
        if not reason:
            raise ValueError("reason 不能为空：必须说明谁因何进场或离场")
        declared = set(self.pack.schedule.affections)
        enter = [str(x) for x in (args.get("enter") or [])]
        leave = [str(x) for x in (args.get("leave") or [])]
        unknown = (set(enter) | set(leave)) - declared
        if unknown:
            raise ValueError(
                f"未知的 NPC: {sorted(unknown)}——只能调整已声明好感的角色: "
                f"{sorted(declared)}"
            )
        both = set(enter) & set(leave)
        if both:
            raise ValueError(f"同一角色不能同时进场与离场: {sorted(both)}")

        before = list(self.state.present_npcs)
        for npc_id in enter:  # 幂等：去重后追加
            if npc_id not in self.state.present_npcs:
                self.state.present_npcs.append(npc_id)
        for npc_id in leave:
            if npc_id in self.state.present_npcs:
                self.state.present_npcs.remove(npc_id)

        def _nm(i: str) -> str:
            return self.pack.npcs[i].name if i in self.pack.npcs else i

        now = "、".join(_nm(i) for i in self.state.present_npcs) or "无"
        changed = before != self.state.present_npcs
        head = "（在场已更新" if changed else "（在场无变化"
        return (
            f"{head}：当前在场 {now}；原因：{reason}）"
            "请勿在叙事中让不在场的角色发言或互动。"
        )

    def _change_scene(self, args: dict) -> str:
        """批次 D：场景移动提议——LLM 只能选地点表内的 id，引擎校验后写入真值。

        与 change_stat 同构：reason 强制填（checklist）、白名单（未声明地点拒绝）、
        关键抉择期间锁定（场景由节点接管）。显示名与地点 id 一并写入状态，
        lore 触发与场景卡随之生效。
        """
        loc_id = str(args.get("location", "")).strip()
        reason = str(args.get("reason", "")).strip()
        if not reason:
            raise ValueError("reason 不能为空：必须说明移动的剧情原因")
        if self.story.choice_locked(self.state):
            raise ValueError("关键抉择期间不能改变场景")
        loc = find_location(self.pack.world, loc_id)
        if loc is None:
            declared = [l.id for l in self.pack.world.locations]
            raise ValueError(f"未声明的地点 '{loc_id}'——只能移动到地点表中的位置: {declared}")
        self.state.scene_id = loc.id
        self.state.scene = loc.name
        return f"（场景已变更：{loc.name}；原因：{reason}）"

    # ------------------------------------------------------------------
    # agent-first 第 2 件：关键节点内轮自校正（Reflexion）
    # ------------------------------------------------------------------

    def _in_critical_node(self) -> bool:
        """当前主线节点是否带关键选择（critical_choices 非空）。"""
        node = self.story.active_node(self.state)
        return node is not None and bool(node.critical_choices)

    def _generate_turn(self, stream: bool) -> "TurnResult":
        """一次叙事生成：组装 → run_turn → 历史同步（turn_count 由调用方计）。

        `game_turn`（K 系列）：把"玩家回合"这一层编号交给 trace。一次 `run_turn` 有
        自己的 `turn_seq`（生成级），而一个玩家回合可能产生 1~3 次生成（级联）、外加
        判劣重写与溢出重试——观测侧此前只能看到生成级序号，无法把成本/时延归到
        "玩家的第几个回合"。这里统一取 `state.turn_count`（已入存档、跨档连续），
        使 trace 与 usage 能在同一根时间轴上对齐。
        """
        self.llm.game_turn = self.state.turn_count
        messages = self.builder.build_messages(
            self.state, self.history, self.story.active_node(self.state)
        )
        result = self.llm.run_turn(
            messages,
            registry=self.registry,  # 批次 C：声明式注册表派发（含世界包自定义工具）
            on_text=self.on_text if stream else None,
        )
        # run_turn 返回的消息含 system 前缀；历史只保留对话部分，
        # 否则下轮 build_messages 会把 system 重复注入（压缩测试抓到的潜伏 bug）
        self.history = [m for m in result.messages if m.get("role") != "system"]
        return result

    def _critique_and_regenerate(
        self,
        result: "TurnResult",
        pre_history: list[dict] | None,
        snapshot: tuple[GameState, list[dict]] | None = None,
    ) -> "TurnResult":
        """关键节点内轮自校正：judge 判劣 → 附结构化反馈重生成一次（至多 2 稿）。

        纪律：
        - 只在 False（有明确问题）时重生成；None（未知）不给反馈、不重生成
          ——"未知 ≠ 通过"，但也无反馈可给（与 _judge_turn 同口径）；
        - 重生成后**从历史中剥掉第一稿**（其叙事文本不再进入后续上下文，防止
          "两个版本都当真"污染后续回合）；
        - **第一稿的工具效果一并作废**（回合事务）：判劣 = 这一稿连同它经工具落盘的
          数值/记忆/行动点全部撤销。此前只剥叙事不剥效果，第二稿再提议一次就重复
          结算（实测：两稿各 +5 武功而玩家只看到一稿，martial 5→15；该错误对
          `after == before + delta` 审计**结构性不可见**——每笔记录都自洽）；
        - 第二稿仍判劣也**接受**（不熔断）：自校正是质量优化层，不是硬门禁。
        """
        if not result.narration:
            if snapshot is not None:  # 无可交付叙事 = 本轮无内容，效果同样不作数
                self._txn_rollback(snapshot, reason="empty_narration")
            return result
        ok, verdict = self.judge.check(
            result.narration,
            self._judge_materials(),
            state=self.state,
            pack=self.pack,
            history=self.history,  # 设计加固 A1：图纳入摘要与选择日志
        )  # agent-first 第 5 件：附跑事实图（confab 缺席证据代码判定）
        if ok is not False or not verdict:
            return result
        self._note_recovery("critique")  # J 系列：确实发生了判劣重写（不是"判过"）
        if snapshot is not None:
            self._txn_rollback(snapshot, reason="critique_regenerate")  # 判劣：第一稿的叙事与效果一起作废
        self.history.append(
            {
                "role": "user",
                "name": "engine",
                "origin": "engine",  # 血缘标记：本条由引擎注入，非模型/玩家产出
                "content": (
                    f"【内轮自校正】上一稿叙事存在质量问题：{verdict}\n"
                    "请重写本轮叙事，自然修正上述问题，并避免重复上一稿的文本。"
                ),
            }
        )
        second = self._generate_turn(stream=False)
        self._sub_turns += 1  # K 系列：重写是**第二次生成**（它绕过 `_llm_round` 直接调
        # `_generate_turn`，不补这一笔 sub_turns 就会漏报——"恢复痕迹说有重写，
        # 生成次数却是 1"，报告自相矛盾）
        if pre_history is None:
            return second  # 无前缀可回放（防御分支，正常路径恒有）
        marker = next(
            i for i, m in enumerate(second.messages)
            if m.get("name") == "engine" and "【内轮自校正】" in (m.get("content") or "")
        )
        # 剥掉第一稿与反馈：历史 = 本轮输入前的前缀 + 第二稿及其后续
        self.history = pre_history + [
            m for m in second.messages[marker + 1:] if m.get("role") != "system"
        ]
        return second

    # ------------------------------------------------------------------
    # agent-first 第 4 件：规划层（plan-and-execute）
    # ------------------------------------------------------------------

    def _ensure_plan(self, node) -> None:
        """节点进入且作者未手写 steps 时，用侧信道把 goal 拆成 2~4 子步骤。

        失败/空/非法 → 无计划（静默降级 = 现状行为）。步骤是"引导"不是"真值"：
        完成判定仍只认 completion 条件；指针推进由 storyline._advance_plan 按
        flag 增量执行（不采信自报）。输入不含 flags（引擎真值纪律）。
        """
        if self.state.node_plan or not node.goal.strip():
            return
        try:
            output = complete_with_empty_retry(
                self.llm,
                [
                    {"role": "system", "content": PLAN_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"<目标>\n{node.goal}\n</目标>\n\n"
                            f"<简报>\n{node.on_enter.briefing}\n</简报>\n\n"
                            f"<场景>\n{node.on_enter.scene}\n</场景>"
                        ),
                    },
                ],
                purpose="plan",
                max_tokens=PLAN_MAX_TOKENS,
                temperature=0.0,
            )
        except Exception:  # noqa: BLE001
            return  # 计划生成失败静默：不影响主线
        self.state.node_plan = parse_steps(output)

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

    def _reflection_material(self, bucket: list) -> list:
        """反思素材 = **最近的** REFLECT_MATERIAL 条记忆，按时间升序（最新在末尾）。

        记忆批修复：此前用 `bucket[-REFLECT_MATERIAL:]` 直接切桶尾，而淘汰分支会
        `bucket.sort((superseded, importance, round))` **永久改写桶序**——桶一旦超过
        `memory_limit`（长局常态），桶尾的语义就从"最新"变成"最高重要"。
        于是门控（`newest_memory_round > last_insight_round`）因某条新记忆放行，
        那条记忆却可能不在送给模型的素材里：反思每 `reflect_every` 回合对着几乎
        同一批旧素材重合成，配合 INSIGHT_CAP=2「新替旧」，注入的洞察系统性偏旧。

        排序用**时间升序**：素材是"最近 N 条"的一个时间窗，且提示词让模型按
        「列表序号」引用来源——升序读起来符合时间直觉（最新在末尾），
        与"近期记忆"的语义一致（见 `REFLECT_MATERIAL` 注释与 `REFLECT_SYSTEM`）。
        """
        recent = sorted(bucket, key=lambda m: -m.round)[:REFLECT_MATERIAL]
        return sorted(recent, key=lambda m: m.round)

    def _reflect_npc(self, npc_id: str, bucket: list) -> None:
        name = self.pack.npcs[npc_id].name
        recent = self._reflection_material(bucket)
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
        """从最近回合内容强制提炼长期事实（侧信道：失败静默降级，不影响主线）。

        **NPC 侧确定性提取**（在场真值就绪后才做得了）：此前 `_extract_facts` 硬编码
        `target="player"`，玩家侧有确定性兜底、**NPC 侧没有**——NPC 记忆只能靠模型
        自愿调用 `remember`，而 A3 反思要求单 NPC 攒够 `REFLECT_MIN_MEMORIES` 条，
        多 NPC 包里因此对多数 NPC 静默空转。且模型"该记却没记"正是玩家侧加兜底的原因，
        NPC 侧同样存在。

        归属判定交给**同一次提取调用**（提示词新增可选的「角色id|重要性|事实」格式），
        而不是引擎盲发：只有真正在场的角色能作为归属，"谁见证了这件事"才是真信息。
        不在场时不提这个话题，避免多余的输出格式分支。
        """
        recent = self._recent_text()
        if not recent.strip():
            return
        existing = "；".join(
            m.fact for m in self.state.player_facts if not m.superseded
        )  # A5：被时序取代的旧事实不进"已有事实"（防误导提炼器）
        present = [i for i in self.state.present_npcs if i in self.pack.npcs]
        roster = ""
        if present:
            names = "、".join(f"{i}（{self.pack.npcs[i].name}）" for i in present)
            roster = f"\n\n<在场角色>\n{names}\n</在场角色>"
        user_content = f"已有事实：{existing}{roster}\n\n<回合内容>\n{recent}\n</回合内容>"
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
        for target, importance, fact in parse_targeted_facts(output, {"player", *present}):
            if target not in ("player", *present):
                target = "player"  # 归属不在场/不认识 → 退化为玩家事实（不凭空造 NPC 记忆）
            try:
                self.memory.add(self.state, target, fact, importance)
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

    def _compress_history(self, keep_turns: int | None = None) -> None:
        """增量压缩：总结上次摘要之后的新增部分，与旧摘要合并，保留近窗。

        ``keep_turns``：本次压缩保留的近窗回合数（默认 ``self.keep_turns``）。
        溢出恢复时传 1——常规设置（6）在回合数不足时让 `find_turn_cut` 直接返回 0
        （no-op），而溢出场景必须让近窗让路，否则压缩永远不会发生。
        """
        cut = find_turn_cut(
            self.history, max(1, self.keep_turns if keep_turns is None else keep_turns)
        )
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
            merged, finish_reason = complete_checked(
                self.llm,
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
                purpose="compress",
                max_tokens=COMPRESS_MAX_TOKENS,
            )
        except Exception:  # noqa: BLE001
            return  # 压缩失败静默降级：保留原历史，下回合重试
        if not merged.strip():
            return  # 空摘要（升级重试后仍空）：放弃本次压缩
        if finish_reason == TRUNCATED_FINISH_REASON:
            return  # 截断摘要会替换历史前缀 = 静默丢内容 → 宁可放弃压缩（重试已在上游做过）
        rebuilt = rebuild_history(self.history, summary_idx, merged.strip(), cut)
        if not ensure_pairing(rebuilt):
            return  # 兜底：重建后配对不变量不成立则放弃本次压缩
        self.history = rebuilt

    # ------------------------------------------------------------------
    # J 系列：上下文溢出的预检与恢复（对齐 pi-agent 的 compact-and-retry）
    # ------------------------------------------------------------------

    def _context_budget(self) -> int:
        """本轮请求可用的**输入** token 预算（0 = 未知/不限）。

        ``compress_threshold`` 是"历史估算超了就压缩"的经验阈值；真正会让请求发不出去
        的是模型上下文窗。两者不是一回事：阈值 30000 而窗口 128K 时阈值先到（压缩及时）；
        换到小窗口模型（或窗口配置得比阈值还小）时，阈值永远追不上窗口——请求先被
        provider 拒掉。故预算 = 窗口 − 输出预算 − 安全余量（预留比例）。

        窗口来自构造参数 ``context_window``（0 = 未声明 → 本判据自动关闭，行为不变）。
        """
        if self.context_window <= 0:
            return 0
        reserve = max(OUTPUT_RESERVE_TOKENS, int(self.context_window * CONTEXT_RESERVE_RATIO))
        return max(0, self.context_window - reserve)

    def _estimated_context_tokens(self) -> int:
        """本轮请求输入 token 的估算（校准口径，见 ``estimate_context_tokens``）。"""
        return estimate_context_tokens(self.history, self.llm.token_factor("turn"))

    def _budget_exceeded(self, threshold: int) -> bool:
        """估算是否已越过 ``threshold``（``threshold <= 0`` = 该判据关闭）。"""
        if threshold <= 0:
            return False
        return self._estimated_context_tokens() > threshold

    def _overflow_retry(self, reason: str) -> "TurnResult | None":
        """上下文溢出恢复：**压缩一次后原地重试**，而不是整轮熔断回滚。

        为什么值得单独一条路径（对齐 pi-agent 的 overflow 恢复）：溢出是**确定性**的
        上下文问题，不是模型发挥失常。旧路径下它与协议失败同命运——重试 3 次同样
        超长的上下文（每次都可能再被拒）→ 熔断 → `_txn_rollback` 撤销本轮全部工具
        效果 → 玩家看到"本轮跳过"，而这三个贵调用白烧。

        纪律：
        - **只尝试一次**（``_overflow_retry_used``）：压缩后仍溢出说明历史本身压不下来
          （例如状态栏地板就超预算），再试只是重复烧钱，交给熔断兜底；
        - 压缩用 ``keep_turns=1``：常规压缩的 ``keep_turns=6`` 在回合数不足时直接
          no-op，而溢出时近窗恰恰必须让路；
        - 压缩**原地**改历史（只碰历史，不碰静态前缀与事实区块），不需要新开事务——
          外层 `_llm_round` 的事务快照仍覆盖本轮全部效果；
        - 压缩 no-op 或重试再熔断 → 返回 None，调用方按熔断处理（行为与改前一致）。
        """
        if self._context_budget() <= 0:
            return None  # 窗口未知：没有压缩目标，判断不了"压够了没有"
        self._overflow_retry_used = True
        before = history_tokens(self.history)
        self._compress_history(keep_turns=1)
        after = history_tokens(self.history)
        self.llm._trace(
            "overflow", game_turn=self.state.turn_count, phase="retry",
            reason=reason, tokens_before=before, tokens_after=after,
            estimated_tokens=self._estimated_context_tokens(),
        )
        if after >= before:
            return None  # 压缩 no-op（没有可总结的新增部分）→ 压不动，别再试
        # K 系列：**先**登记再重试——重试那次生成的 `turn_begin` 事件要能自报来由
        # （"这次生成是溢出恢复"）。若等成功后补记，trace 里两次生成看起来一模一样
        # 是"正常生成"，只有事后从"为什么会有两次"反推，观测层就失去了解释力。
        self._note_recovery("overflow_recovered")
        critical = self.critique_on_critical and self._in_critical_node()
        try:
            result = self._generate_turn(stream=self.on_text is not None and not critical)
        except LLMTurnError:
            return None  # 重试仍熔断 → 交调用方按熔断口径处理
        except Exception:
            # 重试仍被 provider 拒（压缩后依然超窗）→ 同样放弃恢复。
            # 不在这里再抛：调用方已经知道本回合"恢复失败"，需要的是回滚 + 保守回合，
            # 而不是一个逃逸到玩家面前的异常。
            if self.llm.last_overflow:
                return None
            raise  # 与溢出无关的真实故障：照旧传播
        if critical:  # 与正常路径同口径：关键节点仍需自校正（此处不下发，交外层）
            return result
        self.history = [m for m in result.messages if m.get("role") != "system"]
        return result

    def _judge_turn(self, narration: str) -> None:
        """语义校验：失败则注入下轮修正提示（侧信道，失败静默）。"""
        if not narration:
            return
        ok, verdict = self.judge.check(
            narration,
            self._judge_materials(),  # 设计加固 A2：材料附上一轮叙事
            state=self.state,
            pack=self.pack,
            history=self.history,  # 设计加固 A1：图纳入摘要与选择日志
        )  # agent-first 第 5 件：附跑事实图（confab 缺席证据代码判定）
        if ok is False and verdict:  # None = 未知（判定不可用）→ 不注入反馈，也不当作通过
            self._inject_feedback(verdict)

    def _judge_materials(self) -> str:
        """A2（design §10.2-4 落地）：判官材料 = 状态栏 + 上一轮叙事。

        此前判官只拿得到当前状态栏，"与上一轮衔接是否自然 / 是否前后矛盾"
        没有证据面——连贯性检查名存实亡。首轮（无上文）不附。
        """
        materials = self.builder.status_text(
            self.state, self.story.active_node(self.state)
        )
        if self.last_narration:
            materials += f"\n\n<上一轮叙事>\n{self.last_narration}\n</上一轮叙事>"
        return materials

    def _inject_feedback(self, verdict: str) -> None:
        """注入校验反馈并登记复查（设计加固 B2：fire-and-forget → 一次复查闭环）。"""
        self.history.append(
            {
                "role": "user",
                "name": "engine",
                "origin": "engine",  # 血缘标记：本条由引擎注入，非模型/玩家产出
                "content": (
                    f"【校验反馈】上一轮叙事存在质量问题：{verdict}\n"
                    "请在后续叙事中自然修正，避免重复此类问题。"
                ),
            }
        )
        self._pending_verdict = verdict

    def _recheck_feedback(self, narration: str) -> None:
        """B2：复查上一轮反馈是否已自然修正（一次复查，至多一次升级反馈）。

        未知/失败 → 清除队列不升级（三态纪律同口径：未知 ≠ 通过，但也不误伤）；
        升级反馈不再登记复查——连环提示没有边际收益。
        """
        verdict = self._pending_verdict
        self._pending_verdict = None
        ok = recheck_feedback(self.llm, narration, verdict, self._judge_materials())
        if ok is not False:
            return
        self.history.append(
            {
                "role": "user",
                    "name": "engine",
                    "origin": "engine",  # 血缘标记：本条由引擎注入，非模型/玩家产出
                "content": (
                    f"【校验反馈·仍未修正】上一轮反馈的问题仍未解决：{verdict}\n"
                    "本轮必须正面处理该问题：直接修正相关叙事内容，不要回避。"
                ),
            }
        )

    def _factcheck_turn(self, narration: str) -> None:
        """B1：缺席证据检查常开轮（确定性层每轮，LLM judge 降频采样）。

        与 _judge_turn 共用反馈注入与复查队列；违规是确定性判定
        （代码查表），不依赖 LLM judge 的可用性。
        """
        violation = check_graph(
            self.llm, narration, build_graph(self.pack, self.state, self.history)
        )
        if violation:
            self._inject_feedback(violation)

"""约定（appointment）真值守卫：玩家与 NPC 的约好之事必须由引擎掌握。

**复现的玩家实测缺陷**（campus_otome 真机）：
玩家与 NPC 约好「周五去学园祭」，之后 NPC 仍反复询问「周五有没有空」——
在它看来这是全新的问题。

根因不是模型记性差，而是**约定不是引擎真值**：
- 状态模型只有 flags/counters/items（布尔与整数），无法表达「带期限的约定」；
- 约定唯一可能落点是记忆文本，而 `rank_facts` 只注入 10 条（3 常驻 + 7 按分排），
  约定在「新近/重要性/相关性」三维全吃亏：几天后 recency 衰减、
  提取提示词把「剧情进展的瞬时状态」排除在外、到期日无人提及故检索不命中；
- **没有「到期」概念**——引擎不知道「今天该捞出来了」。

本守卫钉住三条契约（改文案可以，改语义不行）：
1. 约定是引擎状态（`state.appointments`），可存档回环；
2. **待履行约定无条件进状态栏**，不参与 `rank_facts` 的检索竞争——
   长局后仍必须在，这是治本的一条；
3. 逾期由引擎自己算出来并显式呈现，不依赖模型回忆。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.context import ENGINE_RULES, ContextBuilder
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import Appointment, GameState, MemoryEntry
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "campus_otome"
NPC_ID = "jiang_yu"
NPC_NAME = "江屿"


def _pack():
    return load_worldpack(PACK_PATH)


def _submit(narration: str, call_id: str = "s1"):
    return tool_call(
        call_id, "submit_narration",
        {"narration": narration, "choices": ["一", "二", "三"], "plot_signal": "normal"},
    )


def _appt(day: int = 18, status: str = "pending") -> Appointment:
    return Appointment(
        id="appt_festival", with_npc=NPC_ID, what="去学园祭", due_day=day,
        made_day=12, status=status,
    )


# ---------------------------------------------------------------------------
# 契约 1：约定是引擎状态（可存档回环）
# ---------------------------------------------------------------------------


def test_appointment_is_engine_state_and_survives_save_roundtrip():
    """约定必须是可序列化的引擎真值——不是记忆文本里的一段散文。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.appointments.append(_appt())

    restored = GameState.from_dict(state.to_dict())
    assert len(restored.appointments) == 1
    a = restored.appointments[0]
    assert (a.id, a.with_npc, a.what, a.due_day, a.status) == (
        "appt_festival", NPC_ID, "去学园祭", 18, "pending"
    )


def test_old_save_without_appointments_still_loads():
    """老档兼容：缺 appointments 字段回退空表（不炸读档）。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    d = state.to_dict()
    del d["appointments"]
    assert GameState.from_dict(d).appointments == []


# ---------------------------------------------------------------------------
# 契约 2：待履行约定无条件进状态栏（治本 —— 不参与检索竞争）
# ---------------------------------------------------------------------------


def test_pending_appointment_injected_unconditionally_after_many_rounds():
    """核心复现钉：约定后过了很久、且当天无人提「周五」，状态栏仍必须带着它。

    这正是玩家看到的缺陷——NPC 重新问「周五有空吗」，因为状态栏里已经没有
    「已约好」这件事了。所以本守卫不看内部实现，只看**模型能看到什么**。
    """
    pack = _pack()
    state = GameState.from_pack(pack)
    state.appointments.append(_appt(day=18))
    state.day, state.turn_count = 25, 60  # 早已到期，且大量回合之后

    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="今天天气不错")

    assert "<约定>" in text, "没有约定区块——模型无从知道已约定"
    assert NPC_NAME in text, "约好的对象不在状态栏里"
    assert "学园祭" in text, "约定的内容不在状态栏里——NPC 会重新提出同一件事"


def test_pending_appointment_beats_retrieval_competition():
    """约定不能和普通事实抢 10 条检索名额。

    构造：24 条玩家事实（远超 RETRIEVAL_K=10 且顶满 PLAYER_FACTS_LIMIT），
    约定之外全部高重要性。约定仍必须出现在状态栏里。
    """
    pack = _pack()
    state = GameState.from_pack(pack)
    state.appointments.append(_appt(day=18))
    for i in range(24):
        state.player_facts.append(
            MemoryEntry(fact=f"无关的高重要性事实 {i}", day=1, round=i, importance=10.0)
        )
    state.day, state.turn_count = 20, 80

    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")

    assert "<约定>" in text, "约定被检索竞争挤掉了（它必须走常驻通道）"
    assert NPC_NAME in text and "学园祭" in text, "约定内容缺失"


def test_fulfilled_appointment_no_longer_injected():
    """已履行的约定不再常驻——否则状态栏会被历史约定撑爆，且模型会重复赴约。

    注意断言的是**约定区块**而不是「学园祭」三个字：campus_otome 的 player_goal
    本身就含「学园祭」，拿词面当判据会假红（本守卫第一版就踩了这个坑）。
    """
    pack = _pack()
    state = GameState.from_pack(pack)
    state.appointments.append(_appt(day=18, status="fulfilled"))
    state.day, state.turn_count = 20, 80

    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")

    assert "<约定>" not in text, "已履行的约定仍在常驻区块里"


def test_appointment_block_absent_when_no_appointments():
    """没有约定时不出现空区块（防状态栏噪声）。"""
    pack = _pack()
    state = GameState.from_pack(pack)

    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")

    assert "<约定>" not in text


# ---------------------------------------------------------------------------
# 契约 3：逾期由引擎自己算出来（不依赖模型回忆）
# ---------------------------------------------------------------------------


def test_overdue_is_computed_by_engine():
    """过了 due_day 仍未履行 → 引擎显式标为逾期（这是一个剧情节拍，不能静默丢失）。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.appointments.append(_appt(day=18))
    state.day, state.turn_count = 22, 50

    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")

    assert "学园祭" in text
    assert "逾期" in text or "错过" in text or "未履行" in text, (
        "逾期约定没有显式呈现——引擎知道真相却没告诉模型"
    )


def test_appointment_due_today_marked():
    """到期当天应有明确的「今日」信号，供模型织入叙事。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.appointments.append(_appt(day=18))
    state.day, state.turn_count = 18, 40

    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")

    assert "<约定>" in text and "学园祭" in text and "今日" in text


# ---------------------------------------------------------------------------
# 契约 4：写入路径 = 模型提议 → 引擎校验（与 change_stat / remember 同范式）
# ---------------------------------------------------------------------------


def test_make_appointment_tool_registered_and_writes_state():
    pack = _pack()
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    state.day = 12

    result = game.registry.dispatch(
        "make_appointment",
        {"npc": NPC_ID, "what": "去学园祭", "due_day": 18},
    )

    assert result.status == "ok", result.message
    assert len(state.appointments) == 1
    a = state.appointments[0]
    assert a.with_npc == NPC_ID and a.what == "去学园祭" and a.due_day == 18
    assert a.status == "pending" and a.made_day == 12


def test_make_appointment_rejects_invalid_input():
    """校验纪律：未知 NPC / 过去日期 / 空内容 / 超长 → 结构化拒绝，不落盘。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    state.day = 12

    cases = [
        {"npc": "不存在的人", "what": "去学园祭", "due_day": 18},
        {"npc": NPC_ID, "what": "去学园祭", "due_day": 5},      # 过去
        {"npc": NPC_ID, "what": "  ", "due_day": 18},            # 空内容
        {"npc": NPC_ID, "what": "很长" * 100, "due_day": 18},     # 超长
    ]
    for args in cases:
        r = game.registry.dispatch("make_appointment", args)
        assert r.status == "rejected", f"{args} 应被拒绝，实际 {r.status}"
    assert state.appointments == []


def test_make_appointment_once_dedupes_same_promise():
    """同一 NPC 同一事由不得重复落盘（防模型每轮都重新约定一遍）。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    state.day = 12

    game.registry.dispatch("make_appointment", {"npc": NPC_ID, "what": "去学园祭", "due_day": 18})
    game.registry.dispatch("make_appointment", {"npc": NPC_ID, "what": "去学园祭", "due_day": 18})

    assert len(state.appointments) == 1


def test_appointment_schema_sent_to_api():
    """工具 schema 必须真发给模型（枚举 = 已声明好感的 NPC），否则模型不会调用。

    注意：campus_otome 的 N1 `when` 恒真——**每次回合开始都会重新要求关键抉择**，
    因此单纯清 pending_choice 无效（`_narrate` 会再置回并短路，本守卫会假绿）。
    这里走真实路径：`start()` 进节点 → `pick()` 解掉抉择 → 普通回合才真的调模型。
    """
    pack = _pack()
    state = GameState.from_pack(pack)
    llm = LLMClient(
        FakeClient([
            resp(msg(tool_calls=[_submit("转学第一天过去了。")])),   # pick → 叙事
            resp(msg(tool_calls=[_submit("放学后的教室。")])),       # say → 叙事
        ]),
        "fake", build_tools(pack.schedule),
    )
    game = Game(pack, state, llm)
    game.start()      # 进入 N1，关键抉择待决
    game.pick(0)      # 解掉抉择

    game.say("任意")

    assert game.llm._client.chat.completions.calls, "没有发生模型调用——本守卫会假绿"
    sent = game.llm._client.chat.completions.calls[0]["tools"]
    schema = next(t for t in sent if t["function"]["name"] == "make_appointment")
    assert schema["function"]["parameters"]["properties"]["npc"]["enum"] == [
        "jiang_yu", "su_qing", "wen_yan", "xia_ming",
    ]


def test_engine_rule_11_in_system_prefix():
    """规则 11（约定纪律）必须在**静态前缀**里——它是模型调用工具的唯一指引。"""
    assert "约定纪律" in ENGINE_RULES and "make_appointment" in ENGINE_RULES
    pack = _pack()
    sys_text = ContextBuilder._system_text(pack)
    assert "约定纪律" in sys_text


def test_query_world_exposes_appointments_without_mutating():
    """query_world 与状态栏同口径，且保持只读（不写状态）。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.appointments.append(_appt(day=18))
    state.day = 18
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)

    before = state.copy()
    result = game.registry.dispatch("query_world", {"query": "今天有什么安排"})

    assert result.status == "ok"
    assert "学园祭" in result.message
    assert state.to_dict() == before.to_dict(), "query_world 必须只读"


# ---------------------------------------------------------------------------
# 契约 5：玩家看到的与模型看到的同源
# ---------------------------------------------------------------------------


def test_player_status_text_shows_appointment():
    """`game.status_text()`（玩家侧）与模型注入同源——玩家也该看到「今天有约」。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.appointments.append(_appt(day=18))
    state.day = 18
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)

    text = game.status_text()
    assert "<约定>" in text and "学园祭" in text and "今日到期" in text

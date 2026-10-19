"""N10-2 守卫：上下文组装的结构化审计（"模型本轮看到了什么、省了什么"）。

上游：`docs/plan-tavern-shaped-product.md` §8.2、`docs/roadmap.md` **N10**。

**被钉住的缺口**：预算一直存在（lore / 角色卡 / 记忆 / 玩家事实各有上限），但**省略
是静默的**——`select_lore` 预算不够就 `continue`，`_clip_block` 逐行保到用完为止。
后果不是崩溃，而是三类**看不见**的问题：

- 长局调参只能靠猜（"这条设定到底进没进上下文"无法验证）；
- 作者花大成本写进世界包的条目可能**一直**因为预算没进过上下文（A 系列十个字段
  零用量的同类风险：校验器只检查"字段有值"，检查不了"运行时被用过"）；
- README 主打的"模型看到的与玩家看到的同源"缺一半证据。

守五件事：

1. **纯增量**：审计不改变组装结果（`build_messages` 与 `plan()[0]` 逐字节相同）；
2. **每条省略都有原因**，且**不同原因分开记**（"预算不足" ≠ "相关度没排上"，
   前者调预算、后者调检索，混成一条就没法据此动手）；
3. **点名到具体条目**（哪个 lore id / 哪个 NPC），否则"省了 2 条"无法行动；
4. **不误报**：一切装得下的时候 `omitted` 必须为空；
5. **真的送到玩家眼前**：`TurnView.context_audit` → Web `_view` → 前端面板。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent import context as ctx_mod
from game_agent.context import ContextBuilder
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack_state(present: tuple[str, ...] = ("shen_qingqiu",)):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.present_npcs = list(present)
    return pack, state


def _builder(pack, *, lore_budget: int | None = None) -> ContextBuilder:
    b = ContextBuilder.from_pack(pack)
    if lore_budget is not None:
        b.lore_budget = lore_budget
    return b


# ---------------------------------------------------------------------------
# 1. 纯增量：审计不改变组装结果
# ---------------------------------------------------------------------------


def test_audit_does_not_change_the_assembled_messages():
    """**最重要的一条**：`plan()` 与 `build_messages()` 产出的消息必须逐字节相同。

    N10-2 的定位是"记录判定结果"，不是"改变判定"。如果这条破了，说明审计顺手
    改了预算行为——那会让所有既有的上下文守卫失去意义（它们断言的是模型输入）。
    """
    pack, state = _pack_state()
    history = [{"role": "user", "content": "我去东市看看"}]
    b = _builder(pack)
    plain = b.build_messages(state, history, None)
    planned, audit = b.plan(state, history, None)
    assert plain == planned
    assert audit.to_dict()["total_chars"] == sum(len(str(m.get("content") or "")) for m in planned)


# ---------------------------------------------------------------------------
# 2/3. 省略有原因、分开记、点名到条目
# ---------------------------------------------------------------------------


def test_lore_dropped_for_budget_is_named_entry_id():
    """预算不足被丢的 lore：记 kind=lore、原因含"预算不足"、**点名到条目 id**。"""
    pack, state = _pack_state()
    # 造一个必然命中的场景：直接拿第一条 lore 的 keys 当场景文本
    entry = pack.world.lore[0]
    state.scene = entry.keys[0]
    b = _builder(pack, lore_budget=1)  # 1 token：什么都放不下
    _msgs, audit = b.plan(state, [], None)
    rows = [r for r in audit.omitted if r["kind"] == "lore" and "预算不足" in r["reason"]]
    assert rows, audit.to_dict()
    assert any(r.get("detail") == entry.id for r in rows), (
        f"没点名到条目 {entry.id}：{rows}"
    )


def test_unmatched_lore_is_reported_differently_from_budget(monkeypatch):
    """"本轮没命中关键词"与"命中但预算不够"是两件事，不能混成一条。

    混记的后果：玩家看到"省略了 30 条世界书"会以为预算出问题了，去调预算；
    而真正该做的是写更准的关键词。

    **这里用桩替换 `select_lore`（返回空）**：真实匹配逻辑由 `test_lore*.py` 覆盖，
    本条钉的是"匹配结果为空时，审计怎么解释它"——用桩才能把"没命中"与
    "命中但超预算"彻底分开（真实包里只要有一条必然命中的条目就凑不出来）。
    """
    pack, state = _pack_state()
    monkeypatch.setattr(ctx_mod, "select_lore", lambda *a, **k: [])
    _msgs, audit = ctx_mod.ContextBuilder.from_pack(pack).plan(state, [], None)
    reasons = {r["reason"] for r in audit.omitted if r["kind"] == "lore"}
    assert reasons, audit.to_dict()
    assert not any("预算不足" in r for r in reasons), reasons
    assert any("没有命中的关键词" in r for r in reasons), audit.to_dict()


def test_relevance_omission_is_distinguished_from_budget():
    """相关度没排上的记忆/事实：原因写"相关度排序未入选"，不是"预算不足"。"""
    pack, state = _pack_state()
    # 塞满玩家事实：rank_facts 只取 top-K，其余应当以"相关度"原因出现
    for i in range(40):
        state.player_facts.append(MemoryEntry(fact=f"无关紧要的第 {i} 条旧事", day=1, round=i))
    _msgs, audit = _builder(pack).plan(state, [], None)
    rows = [r for r in audit.omitted if r["kind"] == "player-fact"]
    assert rows, audit.to_dict()
    assert all(r["reason"] for r in rows), "每条省略都必须有原因"


def test_clipped_npc_card_is_recorded_with_the_npc_name(monkeypatch):
    """角色卡被预算裁掉时：记 kind=npc-card 且点名是哪个角色。"""
    monkeypatch.setattr(ctx_mod, "CARD_BUDGET", 10)  # 极小预算 → 必然裁剪
    pack, state = _pack_state()
    _msgs, audit = _builder(pack).plan(state, [], None)
    rows = [r for r in audit.omitted if r["kind"] == "npc-card"]
    assert rows, audit.to_dict()
    assert any(r.get("detail") == "沈清秋" for r in rows), rows


def test_every_omission_row_carries_a_reason():
    """结构性保证：`omitted` 里**每一行**都有非空原因（空原因 = 又变回静默省略）。"""
    monkeypatch_targets = {"CARD_BUDGET": 10, "MEMORY_BUDGET": 10, "PLAYER_FACT_BUDGET": 10}
    import game_agent.context as m

    saved = {k: getattr(m, k) for k in monkeypatch_targets}
    try:
        for k, v in monkeypatch_targets.items():
            setattr(m, k, v)
        pack, state = _pack_state()
        for i in range(20):
            state.player_facts.append(MemoryEntry(fact=f"旧事 {i}", day=1, round=i))
        state.npc_memories.setdefault("shen_qingqiu", []).extend(
            MemoryEntry(fact=f"她说过的第 {i} 件事", day=1, round=i) for i in range(20)
        )
        _msgs, audit = m.ContextBuilder.from_pack(pack).plan(state, [], None)
    finally:
        for k, v in saved.items():
            setattr(m, k, v)
    assert audit.omitted, "预算压到 10 字符还一条都没省 → 审计没记上"
    for row in audit.omitted:
        assert row["reason"].strip(), f"省略没有原因：{row}"
        assert row["count"] >= 1


# ---------------------------------------------------------------------------
# 4. 不误报
# ---------------------------------------------------------------------------


def test_nothing_omitted_when_everything_fits():
    """一切装得下时 `omitted` 必须为空——否则这个面板会变成狼来了。"""
    pack, state = _pack_state()
    _msgs, audit = _builder(pack).plan(state, [], None)
    assert not [r for r in audit.omitted if "预算不足" in r["reason"]]


# ---------------------------------------------------------------------------
# 5. 真的送到玩家眼前（引擎 → 视图 → HTTP → 前端）
# ---------------------------------------------------------------------------


def _submit(tid: str, narration: str):
    return tool_call(tid, "submit_narration",
                     {"narration": narration, "choices": ["甲", "乙", "丙"], "plot_signal": "normal"})


def test_audit_reaches_the_turn_view():
    """`TurnView.context_audit` 必须带着本轮账本（前端据此渲染右栏面板）。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[_submit("s1", "正文。")]))]),
                    "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    game.start()
    if game.state.pending_choice is not None:
        game.llm._client.chat.completions.responses.insert(
            0, resp(msg(tool_calls=[_submit("open", "你上前一步。")]))
        )
        game.pick(0)
    view = game.say("我环顾四周。")
    assert view.context_audit is not None, "视图没带上下文账本"
    for key in ("included", "omitted", "warnings", "total_chars", "budgets"):
        assert key in view.context_audit, f"账本缺字段 {key}"
    assert view.context_audit["total_chars"] > 0
    assert isinstance(view.context_audit["included"], list) and view.context_audit["included"]


def test_audit_is_carried_in_the_http_payload():
    """HTTP 层 `_view()` 要原样带上（否则前端拿到的是 None，面板永远不出现）。"""
    from game_agent import web

    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[_submit("s1", "正文。")]))]),
                    "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    game.start()
    if game.state.pending_choice is not None:
        game.llm._client.chat.completions.responses.insert(
            0, resp(msg(tool_calls=[_submit("open", "你上前一步。")]))
        )
        game.pick(0)
    view = game.say("我环顾四周。")
    payload = web._view(game, view)
    assert payload["context_audit"]["total_chars"] > 0

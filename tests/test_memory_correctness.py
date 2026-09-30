"""记忆系统正确性守卫（记忆批）：写入、去重、反思素材三处的真值分叉。

三处缺陷各自都会让**状态栏注入与剧情真相相反**的内容：

1. **M1 满额淘汰误杀新生**：桶满时按重要性排序后删头部，而刚 append 的新条目
   如果重要性低于全部存量，它**自己**就是被删的那个——引擎却回传「已写入 + 淘汰了
   1 条旧记忆」。模型据此认为记下了（不再重提），状态栏永远没有它：真值与模型信念分叉。
   `_extract_facts` 走同一路径，而提取器常把日常事实判为 1-4 分，所以长局里
   低重要事实**永远进不了库**。

2. **M2 去重挡住"重新成立"的事实**：去重遍历**含 superseded 的整桶**，于是
   一条已被取代的死事实能拦住同一事实的重新写入；而被取代条目本身又不注入
   （A5），结果是两版都不可见——状态栏持续注入与剧情相反的旧值。

3. **M3 反思素材按重要性而非新旧**：淘汰分支的 `bucket.sort(...)` 会**永久改写桶序**，
   而 `_reflect_npc` 用 `bucket[-REFLECT_MATERIAL:]` 当"最近 N 条"。桶超过上限一次之后，
   桶尾就变成"最高重要"而非"最新"——触发本次反思的那条新记忆可能不在素材里。
"""

from __future__ import annotations

from pathlib import Path

from game_agent.game import Game
from game_agent.llm import LLMClient
from game_agent.memory import (
    PLAYER_FACTS_LIMIT,
    REFLECT_MATERIAL,
    REFLECT_MIN_MEMORIES,
    MemorySystem,
)
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import load_worldpack

PACK = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"
NPC = "shen_qingqiu"  # 该包唯一 NPC，memory_limit=20


def _pack():
    return load_worldpack(PACK)


def _filled_player_bucket(n: int, importance: float):
    """n 条高重要既有事实（模拟长局桶满）。"""
    return [
        MemoryEntry(fact=f"重要旧事实编号{i}", day=1, round=i, importance=importance)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# M1 · 满额淘汰不得误杀刚写入的新事实
# ---------------------------------------------------------------------------


def test_full_bucket_does_not_silently_drop_new_fact():
    """复现钉：桶满且新事实重要性更低时，不得"声称已写入"却把它自己删掉。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.player_facts = _filled_player_bucket(PLAYER_FACTS_LIMIT, importance=9.0)
    mem = MemorySystem(pack, llm=None)

    msg = mem.add(state, "player", "玩家爱喝桂花茶", importance=2.0)

    landed = any(m.fact == "玩家爱喝桂花茶" for m in state.player_facts)
    claimed_written = "已写入" in msg
    assert not (claimed_written and not landed), (
        f"引擎声称已写入但事实没落库——模型会以为记住了：{msg!r}"
    )
    # 要么真的写进去，要么明确告知未写入；两者都不许是"假装成功"
    assert landed or "未" in msg, f"未落库却没有明确回执：{msg!r}"


def test_full_bucket_still_accepts_higher_importance_fact():
    """回归保护：新事实重要性更高时，必须挤掉最低的那条并成功落库。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.player_facts = _filled_player_bucket(PLAYER_FACTS_LIMIT, importance=3.0)
    mem = MemorySystem(pack, llm=None)

    mem.add(state, "player", "玩家其实是前朝遗孤", importance=10.0)

    assert any(m.fact == "玩家其实是前朝遗孤" for m in state.player_facts)
    assert "重要旧事实编号0" not in [m.fact for m in state.player_facts], "未淘汰最低条目"
    assert len(state.player_facts) == PLAYER_FACTS_LIMIT


def test_npc_bucket_eviction_prefers_new_fact_on_tie():
    """同重要性时淘汰更旧的——新事实不该在平局里输给旧事实。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    limit = pack.npcs[NPC].memory_limit
    state.npc_memories[NPC] = [
        MemoryEntry(fact=f"旧记忆{i}", day=1, round=i, importance=5.0) for i in range(limit)
    ]
    mem = MemorySystem(pack, llm=None)

    mem.add(state, NPC, "刚发生的新记忆", importance=5.0)

    assert any(m.fact == "刚发生的新记忆" for m in state.npc_memories[NPC]), (
        "平局时误杀了刚写入的新记忆"
    )
    assert "旧记忆0" not in [m.fact for m in state.npc_memories[NPC]], "未淘汰最旧条目"


def test_superseded_entries_are_evicted_first():
    """回归保护：被取代的死条目仍优先淘汰（A5 纪律）。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    limit = pack.npcs[NPC].memory_limit
    dead = MemoryEntry(fact="已作废的旧记忆", day=1, round=1, importance=10.0, superseded=True)
    alive = [
        MemoryEntry(fact=f"活跃记忆{i}", day=2, round=10 + i, importance=1.0)
        for i in range(limit - 1)
    ]
    state.npc_memories[NPC] = [dead, *alive]
    mem = MemorySystem(pack, llm=None)

    mem.add(state, NPC, "新记忆", importance=2.0)

    facts = [m.fact for m in state.npc_memories[NPC]]
    assert "已作废的旧记忆" not in facts, "superseded 条目应优先被淘汰"


# ---------------------------------------------------------------------------
# M2 · 去重不得挡住已被取代事实的重新写入
# ---------------------------------------------------------------------------


def test_superseded_fact_can_be_reasserted():
    """复现钉：剧情回摆（剑名改回来）时，旧事实必须能重新写入。

    否则活着的仍是"断水"，而被取代的"听雨"不注入 → 状态栏持续注入与剧情相反的旧值。
    """
    pack = _pack()
    state = GameState.from_pack(pack)
    state.player_facts = [
        MemoryEntry(fact="玩家的剑名是听雨", day=1, round=1, importance=8.0, superseded=True),
        MemoryEntry(fact="玩家已将剑改名为断水", day=5, round=9, importance=8.0),
    ]
    mem = MemorySystem(pack, llm=None)

    msg = mem.add(state, "player", "玩家的剑名是听雨", importance=8.0)

    active = [m.fact for m in state.player_facts if not m.superseded]
    assert "玩家的剑名是听雨" in active, (
        f"被取代的事实无法重新成立（回执：{msg!r}）——状态栏会继续注入旧值"
    )


def test_active_duplicate_still_skipped():
    """回归保护：活跃事实的重复写入仍必须被去重拦下（别把去重整个废掉）。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.player_facts = [
        MemoryEntry(fact="玩家的剑名是听雨", day=1, round=1, importance=8.0)
    ]
    mem = MemorySystem(pack, llm=None)

    msg = mem.add(state, "player", "玩家的剑名是听雨", importance=8.0)

    assert "跳过" in msg, f"活跃重复未被拦下：{msg!r}"
    assert len(state.player_facts) == 1


# ---------------------------------------------------------------------------
# M3 · 反思素材必须按"最新"选取
# ---------------------------------------------------------------------------


def test_reflection_material_uses_newest_memories():
    """复现钉：触发本次反思的最新记忆，必须在送给模型的素材里。

    新记忆用 importance=10（高于存量）以便**确实写入**——若用中等重要性，
    M1 的修复会正当地拒绝它（满额且不高于任何存量），那就测不到素材选取了。
    """
    pack = _pack()
    state = GameState.from_pack(pack)
    limit = pack.npcs[NPC].memory_limit
    bucket = [
        MemoryEntry(fact=f"高重要旧记忆{i}", day=2, round=1 + i, importance=9.0)
        for i in range(limit)
    ]
    state.npc_memories[NPC] = bucket
    mem = MemorySystem(pack, llm=None)
    # 触发满额淘汰（旧实现会永久改写桶序）。round 取一个明确更大的值，
    # 避免与旧记忆同 round（state.turn_count 缺省为 0 会造成并列、用例失真）。
    state.turn_count = 500
    mem.add(state, NPC, "刚刚发生的关键对话", importance=10.0)
    assert any(m.fact == "刚刚发生的关键对话" for m in bucket), "夹具前提：新记忆应已写入"

    llm = LLMClient(FakeClientForReflect(), "fake", [])
    game = Game(pack, state, llm)

    material = game._reflection_material(bucket)

    assert any(m.fact == "刚刚发生的关键对话" for m in material), (
        f"最新记忆不在反思素材里（素材 round={[m.round for m in material]}）"
        "—— 门控却因它而放行"
    )


def test_reflection_material_is_recent_not_high_importance():
    """素材语义 = 最近 N 条（按时间升序，最新在末尾），与重要性无关。

    重要性已在检索注入里体现；反思要的是"最近发生了什么"，不是"哪几条最重要"。
    升序呈现让提示词里的「列表序号」读起来符合时间直觉。
    """
    pack = _pack()
    state = GameState.from_pack(pack)
    bucket = [
        MemoryEntry(fact="很久以前的高重要记忆", day=1, round=1, importance=10.0),
        MemoryEntry(fact="刚刚发生的低重要记忆", day=9, round=99, importance=1.0),
    ]
    llm = LLMClient(FakeClientForReflect(), "fake", [])
    game = Game(pack, state, llm)

    material = game._reflection_material(bucket)

    assert [m.fact for m in material] == ["很久以前的高重要记忆", "刚刚发生的低重要记忆"], (
        f"素材未按时间升序：{[m.fact for m in material]}"
    )
    assert material[-1].fact == "刚刚发生的低重要记忆", "最新记忆应在末尾"


def test_reflection_gate_requires_new_memory_in_material():
    """门控与素材必须自洽：门控因"有新记忆"放行时，该记忆必须在素材里。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    bucket = [
        *[
            MemoryEntry(fact=f"旧记忆{i}", day=2, round=i, importance=9.0)
            for i in range(REFLECT_MIN_MEMORIES)
        ],
        MemoryEntry(fact="触发反思的那条新记忆", day=9, round=500, importance=1.0),
    ]
    llm = LLMClient(FakeClientForReflect(), "fake", [])
    game = Game(pack, state, llm)

    material = game._reflection_material(bucket)

    assert any(m.fact == "触发反思的那条新记忆" for m in material), (
        "门控放行的那条新记忆不在素材里"
    )


class FakeClientForReflect:
    """反思调用不会被真正执行（这些用例只检查素材选取），给一个空响应兜底。"""

    class chat:  # noqa: N801
        class completions:  # noqa: N801
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("本用例不应真正调用 LLM")
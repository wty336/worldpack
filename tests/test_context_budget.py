"""上下文预算与历史卫生守卫（上下文批）。

三项缺陷，共同主题是"每轮追加的动态内容没有被任何预算约束 / 被重复追加"：

1. **C1 pick 重复写入**：`pick` 自己 append 一次、`_narrate` 又 append 一次
   → 每个节点的**最后一个**关键抉择在历史里出现两次。`_recent_player_text(n=2)`
   的两个名额被同一条输入吃掉；`find_turn_cut` 把它当成两个回合锚点。
2. **C2 压缩摘要漂移**：见 `test_compression_prefix.py`（已在 compression 层修复）。
3. **C3 状态栏无总预算**：状态栏每轮重新生成并**进历史**，体量随在场 NPC 数线性增长；
   动态区块里只有 lore 有预算（`LORE_BUDGET`），记忆区与角色卡区都没有。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.context import ContextBuilder
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import load_worldpack

OTOME = Path(__file__).resolve().parent.parent / "world-packs" / "campus_otome"
ERA = Path(__file__).resolve().parent.parent / "world-packs" / "P2_era_dual"


# ---------------------------------------------------------------------------
# C1 · pick 不得重复写入玩家消息
# ---------------------------------------------------------------------------


def _pick_game():
    pack = load_worldpack(OTOME)
    state = GameState.from_pack(pack)
    submit = tool_call("s1", "submit_narration",
                       {"narration": "你挡在她身前。", "choices": ["一", "二", "三"],
                        "plot_signal": "normal"})
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[submit]))]), "fake",
                    build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm)


def test_pick_does_not_duplicate_player_message():
    """复现钉：关键抉择的玩家消息在历史里必须只出现一次。"""
    pack, state, game = _pick_game()
    game.start()
    assert state.pending_choice is not None, "夹具前提：开局有待决抉择"
    game.pick(0)

    texts = [m.get("content") for m in game.history if m.get("role") == "user"]
    chosen = [t for t in texts if t and "你选择了" in t]
    assert len(chosen) == 1, f"玩家抉择消息出现 {len(chosen)} 次: {chosen}"


def test_pick_message_still_reaches_history():
    """回归保护：去重不能把这条消息整个弄丢（后续回合必须看得到玩家选了什么）。"""
    pack, state, game = _pick_game()
    game.start()
    game.pick(0)

    texts = [m.get("content") for m in game.history if m.get("role") == "user"]
    assert any(t and "你选择了" in t for t in texts), "玩家抉择消息丢失"


def test_pick_single_turn_anchor():
    """回合锚点数必须等于真实玩家回合数——重复的 pick 消息会虚增锚点。

    注意锚点定义 = **无 name 的 user 消息**：节点任务卡带 `name="engine"` 故不计入。
    本用例只有一次 pick，所以恰好 1 个锚点；旧实现会因重复写入变成 2 个。
    """
    from game_agent.compression import find_turn_cut

    pack, state, game = _pick_game()
    game.start()
    game.pick(0)

    user_msgs = [
        i for i, m in enumerate(game.history)
        if m.get("role") == "user" and not m.get("name")
    ]
    assert len(user_msgs) == 1, (
        f"回合锚点 {len(user_msgs)} 个（期望 1：只有这次 pick）—— "
        "重复写入会把 6 回合近窗实际覆盖成更少的真实回合"
    )
    # 1 个锚点、要求保留 1 个 → 无需压缩（切点 0）；这正是"锚点没被虚增"的旁证：
    # 若旧实现写了两次，这里锚点会是 2，find_turn_cut(keep_turns=1) 就会切掉一整个回合。
    assert find_turn_cut(game.history, keep_turns=1) == 0


# ---------------------------------------------------------------------------
# C3 · 动态区块必须有总预算
# ---------------------------------------------------------------------------


def _load_era(npcs_present: int, memories_each: int = 20):
    pack = load_worldpack(ERA)
    state = GameState.from_pack(pack)
    ids = list(pack.npcs)[:npcs_present]
    state.present_npcs = ids
    for nid in ids:
        state.npc_memories[nid] = [
            MemoryEntry(
                fact=f"{pack.npcs[nid].name}记得玩家做过的一件事情编号{i}（一段中等长度的描述）",
                day=1, round=i, importance=6.0,
            )
            for i in range(memories_each)
        ]
    for i in range(20):
        state.player_facts.append(
            MemoryEntry(fact=f"玩家的长期事实编号{i}（一段中等长度的描述）", day=1,
                        round=i, importance=7.0)
        )
    state.day, state.turn_count = 30, 80
    return pack, state


def test_dynamic_block_has_a_total_budget():
    """状态栏必须有总字符预算——否则体量随在场 NPC 数线性膨胀（并进历史）。"""
    from game_agent.context import STATUS_BUDGET

    assert STATUS_BUDGET > 0, "缺少状态栏预算常量"
    pack, state = _load_era(npcs_present=6)
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")
    assert len(text) <= STATUS_BUDGET, (
        f"状态栏 {len(text)} 字符超过预算 {STATUS_BUDGET}"
    )


def test_status_growth_is_sublinear_in_npc_count():
    """状态栏随在场 NPC 数**次线性**增长：每多一个 NPC 只多一张卡（记忆共享预算）。

    判据不是"总量不变"——卡片天然随人数增长（每个人格/语气是写对话的地基，
    不该互相挤掉）。关键是**记忆区不再按人数线性叠加**：旧实现每人 13 条无上限，
    6 NPC 实测 4474 字符；现在每人只分到 1600/人数 的额度。
    """
    sizes = []
    for n in (1, 6):
        pack, state = _load_era(npcs_present=n)
        text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")
        sizes.append(len(text))
    # 6 NPC 的体量应显著低于"线性外推"（1 NPC × 6）
    assert sizes[1] < sizes[0] * 3, (
        f"状态栏接近线性膨胀：1 NPC {sizes[0]} 字符 → 6 NPC {sizes[1]} 字符"
    )


def test_status_keeps_history_floor_under_compress_threshold():
    """真正要守住的性质：`keep_turns × 状态栏` 必须明显低于压缩阈值。

    否则保留近窗的"地板"本身就超过阈值 → 压缩永远压不到阈值以下 →
    每轮都触发一次压缩 LLM 调用（审计实测 30 回合里 24 次）。
    """
    from game_agent.game import Game

    keep_turns, threshold = 6, 30000
    pack, state = _load_era(npcs_present=6)
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")

    floor = len(text) * keep_turns
    assert floor < threshold * 0.8, (
        f"历史地板 {floor} 字符已逼近压缩阈值 {threshold}"
        f"（状态栏 {len(text)} × keep_turns {keep_turns}）"
    )


def test_budget_does_not_drop_essential_state():
    """预算裁剪不得丢掉核心真值（属性/好感/场景/主线目标必须仍在）。"""
    pack, state = _load_era(npcs_present=6)
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")

    assert "<scene>" in text
    assert "玩家属性" in text
    assert "好感" in text
    assert "主线目标" in text or "剧情进度" in text

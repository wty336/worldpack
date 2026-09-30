"""压缩的字节前缀不变量守卫：压缩只应改动"摘要那一条"，其余消息原位不动。

**缺陷背景**：`rebuild_history` 返回 `[旧前缀] + [新摘要] + [切点之后的近窗]`，
把摘要**插在近窗之前**。对 KV Cache 来说这是双重打击：

- 摘要的 index 从「前缀之后」漂移到「前缀+已删消息之后」，**从摘要处起整段失效**；
- 更糟的是它把**旧前缀**也挪动了位置（旧前缀原本在更前面），
  于是连本可复用的前缀也一起作废；

而实际上近窗消息在两次请求间**逐字未变**——它们本该是缓存里最值钱的部分
（越靠前命中越省）。修法：摘要在**原位替换**，只让那一条内容变。

不变量：`new[:summary_idx] == old[:summary_idx]` 且 `new[-(len(old)-cut):] == old[cut:]`。
"""

from __future__ import annotations

import game_agent.compression as C
from game_agent.compression import (
    SUMMARY_MARK,
    ensure_pairing,
    find_turn_cut,
    locate_summary,
    rebuild_history,
)


def _turn(user: str, narration: str) -> list[dict]:
    """一个最小回合：user（无 name）+ assistant 叙事。"""
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": narration},
    ]


def _history(turns: int) -> list[dict]:
    h: list[dict] = []
    for i in range(turns):
        h += _turn(f"玩家第{i}回合", f"叙事第{i}回合")
    return h


def _summary_msg(text: str) -> dict:
    return {"role": "user", "name": "engine", "content": f"{SUMMARY_MARK}\n{text}"}


# ---------------------------------------------------------------------------
# 核心：摘要原位替换
# ---------------------------------------------------------------------------


def test_compression_keeps_prefix_and_tail_identical():
    """复现钉：压缩后，摘要之前的消息与切点之后的消息必须逐字不变。

    摘要**不在** index 0：真实长局里，摘要之后还会追加若干回合才触发下一次压缩，
    所以"摘要之前的前缀"是真空存在的（也正是缓存里最靠前、最值钱的那段）。
    """
    old = _history(2) + [_summary_msg("旧摘要")] + _history(6)
    summary_idx = locate_summary(old)
    cut = find_turn_cut(old, keep_turns=2)
    assert summary_idx == 4 and cut > summary_idx, "夹具前提不成立"

    new = rebuild_history(old, summary_idx, "新摘要", cut)

    # 摘要之前的消息：逐字不变（前缀可继续命中缓存）
    assert new[:summary_idx] == old[:summary_idx], "摘要之前的前缀被挪动了"
    # 切点之后的消息：逐字不变（近窗是缓存里最值钱的部分）
    kept_old = old[cut:]
    kept_new = new[len(new) - len(kept_old):]
    assert kept_new == kept_old, "近窗消息被改动了——缓存从摘要处起整段失效"


def test_summary_stays_at_its_original_index():
    """摘要必须留在原位——它一旦漂移，从它往后的一切都失去缓存复用。"""
    old = _history(2) + [_summary_msg("旧摘要")] + _history(6)
    summary_idx = locate_summary(old)
    cut = find_turn_cut(old, keep_turns=2)

    new = rebuild_history(old, summary_idx, "新摘要", cut)

    assert locate_summary(new) == summary_idx, (
        f"摘要 index 从 {summary_idx} 漂移到 {locate_summary(new)}——缓存从摘要起失效"
    )


def test_no_duplicate_summary_after_repeated_compression():
    """连续压缩不得留下两条以上摘要。

    真实发作形态（实测）：摘要最初在 index 0，之后又跑了若干回合；第一次压缩把它
    原位替换（看不出问题），**第二次起** `summary_idx` 漂移到 1、3……
    摘要条数按 1→2→4 累积，index 持续漂移 —— 缓存与语义双输。
    """
    old = [_summary_msg("摘要v1")] + _history(8)
    for version in ("v2", "v3", "v4"):
        idx = locate_summary(old)
        cut = find_turn_cut(old, keep_turns=2)
        old = rebuild_history(old, idx, f"摘要{version}", cut)

    marks = [
        i for i, m in enumerate(old)
        if m.get("role") == "user" and str(m.get("content") or "").startswith(SUMMARY_MARK)
    ]
    assert len(marks) == 1, f"出现了 {len(marks)} 条摘要: {marks}"
    assert "摘要v4" in old[marks[0]]["content"]
    assert locate_summary(old) == 0, "摘要 index 漂移了 —— 缓存从它往后全部失效"


def test_old_summarized_content_is_actually_dropped():
    """回归保护：压缩必须真的删掉已被摘要覆盖的消息（别把压缩做成无效操作）。"""
    old = [_summary_msg("旧摘要")] + _history(8)
    cut = find_turn_cut(old, keep_turns=2)
    new = rebuild_history(old, locate_summary(old), "新摘要", cut)

    assert len(new) < len(old), "压缩后历史没有变短"
    assert "玩家第0回合" not in [m.get("content") for m in new], "被摘要覆盖的消息仍在"


def test_first_compression_appends_summary_at_front():
    """首次压缩（无摘要）：摘要应放在最前，与后续压缩的位置语义一致。"""
    old = _history(8)
    cut = find_turn_cut(old, keep_turns=2)
    assert locate_summary(old) == -1, "夹具前提：此时还没有摘要"

    new = rebuild_history(old, -1, "首份摘要", cut)

    assert locate_summary(new) == 0
    assert new[-len(old[cut:]):] == old[cut:], "近窗被改动"


def test_pairing_invariant_holds_after_rebuild():
    """配对不变量：重建后 tool_calls / tool_result 不得孤儿化。"""
    olD = _history(6)
    olD += [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function",
                            "function": {"name": "submit_narration", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "已接收本轮叙事。"},
        {"role": "user", "content": "再一回合"},
        {"role": "assistant", "content": "收尾叙事。"},
    ]
    cut = find_turn_cut(olD, keep_turns=2)
    new = rebuild_history(olD, -1, "摘要", cut)

    assert ensure_pairing(new), "重建后出现孤儿 tool 消息"

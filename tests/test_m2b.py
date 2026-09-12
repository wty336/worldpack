"""M2b 验收测试（离线）：压缩切点/增量重建/配对不变量 + 语义校验接入。"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.compression import (
    ensure_pairing,
    find_turn_cut,
    history_tokens,
    locate_summary,
    rebuild_history,
)
from game_agent.game import Game
from game_agent.judge import JudgeSystem, parse_verdict
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

SUBMIT = tool_call(
    "s1",
    "submit_narration",
    {"narration": "叙事内容", "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
)


def _turn_history(turns: int) -> list[dict]:
    """构造 turns 个完整回合（每回合 = user + assistant(tool_calls) + tool 配对）。"""
    history = []
    for t in range(turns):
        history.append({"role": "user", "content": f"玩家发言{t}"})
        history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call{t}",
                        "type": "function",
                        "function": {"name": "submit_narration", "arguments": "{}"},
                    }
                ],
            }
        )
        history.append({"role": "tool", "tool_call_id": f"call{t}", "content": "已接收。"})
    return history


def test_find_turn_cut_respects_turn_boundaries():
    history = _turn_history(10)
    cut = find_turn_cut(history, keep_turns=3)
    # 切点是第 8 个 user 消息（10 - 3 = 7 个之前的 + 1），即索引 7*3 = 21
    assert history[cut]["role"] == "user"
    assert history[cut]["content"] == "玩家发言7"
    # 切点之前为整回合，配对不变量保持
    assert ensure_pairing(history[:cut])
    assert ensure_pairing(history[cut:])


def test_find_turn_cut_no_cut_when_turns_insufficient():
    history = _turn_history(2)
    assert find_turn_cut(history, keep_turns=6) == 0


def test_rebuild_and_locate_summary():
    history = _turn_history(10)
    cut = find_turn_cut(history, keep_turns=3)
    rebuilt = rebuild_history(history, -1, "【新摘要】省略", cut)
    assert locate_summary(rebuilt) == 0
    assert rebuilt[0]["content"].startswith("【剧情摘要】")
    assert rebuilt[1:] == history[cut:]
    assert ensure_pairing(rebuilt)


def test_incremental_compression_merges_summary():
    """二次压缩：旧摘要保留在头部，只总结新增部分。"""
    history = _turn_history(10)
    cut1 = find_turn_cut(history, keep_turns=3)
    rebuilt1 = rebuild_history(history, -1, "摘要一", cut1)
    # 模拟继续对话 3 个回合
    rebuilt1.extend(_turn_history(3))
    cut2 = find_turn_cut(rebuilt1, keep_turns=3)
    idx = locate_summary(rebuilt1)
    assert idx == 0
    rebuilt2 = rebuild_history(rebuilt1, idx, "摘要二（合并摘要一）", cut2)
    assert rebuilt2[0]["content"] == "【剧情摘要】\n摘要二（合并摘要一）"
    assert ensure_pairing(rebuilt2)


def test_history_tokens_rough_estimate():
    history = _turn_history(10)
    assert history_tokens(history) > 0


def test_judge_parse_verdict():
    assert parse_verdict("通过") == (True, "通过")
    assert parse_verdict("通过。")[0] is True
    assert parse_verdict("OOC：沈清秋说出网络用语。")[0] is False
    assert parse_verdict("")[0] is None  # 空输出 = 未知（三态；不得当作通过）


def _game_for_compression(responses, threshold, keep_turns=1):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.completed_nodes.append("n1_first_meeting")
    state.flags["met_shen"] = True
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm, compress_threshold=threshold, keep_turns=keep_turns)
    return pack, state, game


def test_compression_triggers_and_keeps_pairing():
    """压缩触发：摘要响应被消费、历史重建、配对不变量成立（threshold=1, keep_turns=1 强制每回合压缩）。"""
    summary_resp = resp(msg(content="剧情摘要：二人约定七月暗号。"))
    pack, state, game = _game_for_compression(
        [
            resp(msg(tool_calls=[SUBMIT])),  # 回合1 叙事（首轮历史不足，不触发压缩）
            summary_resp,  # 回合2 前的压缩摘要调用
            resp(msg(tool_calls=[SUBMIT])),  # 回合2 叙事
        ],
        threshold=1,
    )
    game.say("第一句")
    view = game.say("第二句")
    assert view.narration == "叙事内容"
    assert game.history[0]["content"].startswith("【剧情摘要】")
    assert "七月暗号" in game.history[0]["content"]
    assert ensure_pairing(game.history)


def test_compression_skips_when_summary_fails():
    """压缩调用返回空摘要（升级重试后仍空）→ 历史保持不变（静默降级）。"""
    pack, state, game = _game_for_compression(
        [
            resp(msg(tool_calls=[SUBMIT])),
            resp(msg(content="")),  # 首次空 → 升级预算重试
            resp(msg(content="")),  # 重试仍空 → 放弃压缩
            resp(msg(tool_calls=[SUBMIT])),
        ],
        threshold=1,
    )
    game.say("第一句")
    view = game.say("第二句")
    assert view.narration == "叙事内容"
    assert not game.history[0]["content"].startswith("【剧情摘要】")


def test_compression_abandons_truncated_summary():
    """截断摘要**不得采纳**：它会替换历史前缀 = 静默丢内容，比不压缩更糟。

    首次截断 → 升级预算重试；重试仍截断 → 放弃本次压缩，历史保持原样。
    """
    from game_agent.budgets import COMPRESS_MAX_TOKENS, retry_tokens_for

    pack, state, game = _game_for_compression(
        [
            resp(msg(tool_calls=[SUBMIT])),
            resp(msg(content="【剧情摘要】半截……"), finish_reason="length"),
            resp(msg(content="【剧情摘要】还是半截……"), finish_reason="length"),
            resp(msg(tool_calls=[SUBMIT])),
        ],
        threshold=1,
    )
    game.say("第一句")
    game.say("第二句")
    assert not game.history[0]["content"].startswith("【剧情摘要】")
    calls = game.llm._client.chat.completions.calls
    assert calls[1]["max_tokens"] == COMPRESS_MAX_TOKENS
    assert calls[2]["max_tokens"] == retry_tokens_for(COMPRESS_MAX_TOKENS)


def test_compression_adopts_successful_retry():
    """首次截断、重试成功 → 采纳重试摘要（内容完整）。"""
    pack, state, game = _game_for_compression(
        [
            resp(msg(tool_calls=[SUBMIT])),
            resp(msg(content="【剧情摘要】半截"), finish_reason="length"),
            resp(msg(content="剧情摘要：二人约定七月暗号。"), finish_reason="stop"),
            resp(msg(tool_calls=[SUBMIT])),
        ],
        threshold=1,
    )
    game.say("第一句")
    game.say("第二句")
    assert game.history[0]["content"].startswith("【剧情摘要】")
    assert "七月暗号" in game.history[0]["content"]


def test_judge_feedback_injected_on_failure():
    """语义校验失败 → 注入下轮修正提示；通过 → 不注入。"""
    pack, state, game = _game_for_compression([], threshold=0)
    game.judge_every = 1
    # 失败判定
    game.judge = JudgeSystem(
        LLMClient(FakeClient([resp(msg(content="OOC：沈清秋说了网络用语。"))]), "fake", [])
    )
    game._judge_turn("沈清秋说：绝绝子。")
    assert any("【校验反馈】" in m.get("content", "") for m in game.history)
    # 通过判定：不注入
    game.history = []
    game.judge = JudgeSystem(
        LLMClient(FakeClient([resp(msg(content="通过"))]), "fake", [])
    )
    game._judge_turn("正常叙事。")
    assert not game.history

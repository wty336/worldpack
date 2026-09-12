"""侧信道预算统一（离线）：空响应升级重试 + 预算单一真源。

背景（retro §5.3 / §8.2 实锤）：思考模式模型的推理链也计入 ``max_tokens``，
预算过小时会被推理吃光导致 ``content`` 为空。空 = 未知，不是
「通过 / 不重复 / 无洞察 / 无事实」——旧代码把空响应当成结论静默放行。
修复策略统一为：基础预算（≥``MIN_CALL_TOKENS``）+ 空响应升级重试一次。

compress 不在本策略内：其空响应是安全降级（保留原历史，下回合重试，
见 ``test_m2b.test_compression_skips_when_summary_fails``），不会把空当作结论。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp

from game_agent.budgets import DEDUP_MAX_TOKENS, EMPTY_RETRY_TOKENS, REFLECT_MAX_TOKENS
from game_agent.game import Game
from game_agent.llm import LLMClient
from game_agent.memory import MemorySystem
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _sys(llm=None):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    return pack, state, MemorySystem(pack, llm)


def _game(responses):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient(responses), "fake", [])
    return pack, state, Game(pack, state, llm)


# ---------------------------------------------------------------------------
# dedup：空响应升级重试
# ---------------------------------------------------------------------------


def test_dedup_empty_output_escalates_budget():
    """首次空输出 → 升级预算重试一次，按重试结果判定为「重复」。"""
    llm = LLMClient(
        FakeClient([resp(msg(content="")), resp(msg(content="重复"))]), "fake", []
    )
    _, state, mem = _sys(llm)
    mem.add(state, "player", "剑名听雨")
    out = mem.add(state, "player", "佩剑唤作听雨")  # 非包含关系 → 走语义判定
    assert "语义重复" in out
    calls = llm._client.chat.completions.calls
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == DEDUP_MAX_TOKENS
    assert calls[1]["max_tokens"] == EMPTY_RETRY_TOKENS


# ---------------------------------------------------------------------------
# reflect：空响应升级重试
# ---------------------------------------------------------------------------


def test_reflect_empty_output_escalates_budget():
    """首次空输出 → 升级预算重试一次；首轮预算取自 budgets.REFLECT_MAX_TOKENS。"""
    pack, state, game = _game(
        [resp(msg(content="")), resp(msg(content="态度转向信任|1,2"))]
    )
    state.npc_memories["shen_qingqiu"] = [
        MemoryEntry(fact=f"记忆{i:02d}", day=1, round=i, importance=5.0)
        for i in range(9)
    ]
    game._reflect_npc("shen_qingqiu", state.npc_memories["shen_qingqiu"])
    insights = state.npc_insights["shen_qingqiu"]
    assert [i.text for i in insights] == ["态度转向信任"]
    calls = game.llm._client.chat.completions.calls
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == REFLECT_MAX_TOKENS
    assert calls[1]["max_tokens"] == EMPTY_RETRY_TOKENS


# ---------------------------------------------------------------------------
# extract：空响应升级重试
# ---------------------------------------------------------------------------


def test_extract_empty_output_escalates_budget():
    """首次空输出 → 升级预算重试一次，事实按重试结果写入。"""
    from game_agent.budgets import EXTRACT_MAX_TOKENS

    pack, state, game = _game(
        [resp(msg(content="")), resp(msg(content="8|我把剑命名为听雨"))]
    )
    game.history = [
        {"role": "user", "content": "我把剑命名为听雨。"},
        {"role": "assistant", "content": "你握紧了剑柄。"},
    ]
    game._extract_facts()
    assert [f.fact for f in state.player_facts] == ["我把剑命名为听雨"]
    calls = game.llm._client.chat.completions.calls
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == EXTRACT_MAX_TOKENS
    assert calls[1]["max_tokens"] == EMPTY_RETRY_TOKENS


# ---------------------------------------------------------------------------
# 截断（finish_reason=length）：非空但不可信，同样升级重试
# ---------------------------------------------------------------------------


def test_truncated_output_escalates_budget():
    """截断的判定不可信：即使内容非空，也要升级预算重试一次并采用重试结果。"""
    from game_agent.budgets import complete_with_empty_retry

    llm = LLMClient(
        FakeClient(
            [
                resp(msg(content="问题类型：OOC（半截"), finish_reason="length"),
                resp(msg(content="问题类型：OOC：角色说出网络用语。"), finish_reason="stop"),
            ]
        ),
        "fake",
        [],
    )
    out = complete_with_empty_retry(
        llm, [{"role": "user", "content": "x"}], purpose="judge", max_tokens=500
    )
    assert out.startswith("问题类型：OOC：")
    calls = llm._client.chat.completions.calls
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 500
    assert calls[1]["max_tokens"] == EMPTY_RETRY_TOKENS


def test_normal_output_is_not_retried():
    """正常结束（finish_reason=stop）不重试——不引入额外成本。"""
    from game_agent.budgets import complete_with_empty_retry

    llm = LLMClient(
        FakeClient([resp(msg(content="重复"), finish_reason="stop")]), "fake", []
    )
    out = complete_with_empty_retry(
        llm, [{"role": "user", "content": "x"}], purpose="dedup", max_tokens=DEDUP_MAX_TOKENS
    )
    assert out == "重复"
    assert len(llm._client.chat.completions.calls) == 1


def test_retry_still_truncated_returns_retry_text():
    """两次都截断 → 返回重试文本（更长），不回退到第一次的半截输出。"""
    from game_agent.budgets import complete_with_empty_retry

    llm = LLMClient(
        FakeClient(
            [
                resp(msg(content="第一"), finish_reason="length"),
                resp(msg(content="第二次更长一些"), finish_reason="length"),
            ]
        ),
        "fake",
        [],
    )
    out = complete_with_empty_retry(
        llm, [{"role": "user", "content": "x"}], purpose="reflect", max_tokens=REFLECT_MAX_TOKENS
    )
    assert out == "第二次更长一些"


# ---------------------------------------------------------------------------
# 预算单一真源：规则与升级不变量
# ---------------------------------------------------------------------------


def test_sidechannel_budgets_obey_floor_and_upgrade_rule():
    """所有 LLM 调用预算 ≥ MIN_CALL_TOKENS；升级预算严格大于每个基础预算。"""
    from game_agent import budgets

    base = {
        "turn": budgets.TURN_MAX_TOKENS,
        "compress": budgets.COMPRESS_MAX_TOKENS,
        "judge": budgets.JUDGE_MAX_TOKENS,
        "dedup": budgets.DEDUP_MAX_TOKENS,
        "reflect": budgets.REFLECT_MAX_TOKENS,
        "extract": budgets.EXTRACT_MAX_TOKENS,
    }
    for purpose, value in base.items():
        assert value >= budgets.MIN_CALL_TOKENS, f"{purpose} 预算低于规则下限"
    # 启用空响应/截断升级的侧信道：升级预算必须严格大于基础预算
    # （compress 自 2026-09-11 起纳入：实测 53% 的 flash 调用顶在 2000 附近）
    for purpose in ("judge", "dedup", "reflect", "extract", "compress"):
        assert budgets.retry_tokens_for(base[purpose]) > base[purpose], (
            f"{purpose} 的升级预算没有升级"
        )
    # 升级规则本身：小任务取 2000 下限，大任务取 2×（compress 4000 → 8000）
    assert budgets.retry_tokens_for(budgets.JUDGE_MAX_TOKENS) == budgets.EMPTY_RETRY_TOKENS
    assert budgets.retry_tokens_for(budgets.COMPRESS_MAX_TOKENS) == 2 * budgets.COMPRESS_MAX_TOKENS


def test_call_sites_share_the_single_source_of_truth():
    """调用点常量必须与 budgets 同源（防「改了常量却没生效」的回归）。"""
    from game_agent import budgets
    from game_agent import judge as judge_mod
    from game_agent import llm as llm_mod
    from game_agent import memory as memory_mod

    assert judge_mod.JUDGE_MAX_TOKENS == budgets.JUDGE_MAX_TOKENS
    assert judge_mod.JUDGE_EMPTY_RETRY_TOKENS == budgets.EMPTY_RETRY_TOKENS
    assert memory_mod.DEDUP_MAX_TOKENS == budgets.DEDUP_MAX_TOKENS
    assert memory_mod.REFLECT_MAX_TOKENS == budgets.REFLECT_MAX_TOKENS
    assert llm_mod.MAX_OUTPUT_TOKENS == budgets.TURN_MAX_TOKENS

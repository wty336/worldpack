"""agent-first 第 5 件守卫测试：事实图 Judge 代码层（离线）。

设计文档：docs/design-factgraph.md。契约：
- 图从材料同源真值构建（pack+state），不含 flags；
- 断言抽取（要点式）→ 代码查缺席：强锚点（数字/「」/3~4 字窗口，排除停用词）
  **全部**缺席 → 违规；任一强锚点接地 → 通过；2 字窗口不算强锚点（伪锚点）；
- 失败静默（抽取异常 → None）；
- judge 合并语义：代码违规 → 确定性 False（LLM 未知时也照常）；否则 LLM 三态原样；
- state/pack 缺省 → 图检查关闭（向后兼容，旧调用方零改动）；
- build_state / build_materials 同源一致。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fakes import FakeClient, msg, resp

from game_agent.context import ContextBuilder
from game_agent.factgraph import build_graph, check_graph, parse_claims
from game_agent.judge import JudgeSystem
from game_agent.judge_corpus import JudgeCase, build_materials, build_state
from game_agent.llm import LLMClient
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _state_with(pack, facts=(), memories=None, present=("shen_qingqiu",)):
    state = GameState.from_pack(pack)
    state.present_npcs = list(present)
    state.player_facts = [MemoryEntry(fact=f, day=1, round=0) for f in facts]
    state.npc_memories = memories or {}
    return state


def _llm_with(*contents):
    return LLMClient(FakeClient([resp(msg(content=c)) for c in contents]), "fake", [])


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------


def test_build_graph_from_pack_and_state():
    pack = _pack()
    state = _state_with(
        pack,
        facts=["玩家的剑名是「听雨」"],
        memories={"shen_qingqiu": [MemoryEntry(fact="玩家中秋前答应替她寻回诗集", day=3, round=5)]},
    )
    graph = build_graph(pack, state)
    assert graph.has("听雨")  # 「」引用串
    assert graph.has("中秋")  # 记忆 2 字 token
    assert graph.has("沈清秋")  # NPC 卡身份
    state.flags["secret_flag_xyz"] = True
    graph2 = build_graph(pack, state)
    assert "secret_flag_xyz" not in " ".join(graph2.facts)  # flags 不入图


def test_parse_claims_tolerant():
    assert parse_claims("1. 把密码本交给白鸮\n2. 欠五十两，中秋前归还") == \
        ["把密码本交给白鸮", "欠五十两，中秋前归还"]
    assert parse_claims("无") == []
    assert parse_claims("- 甲。\n· 乙") == ["甲", "乙"]


# ---------------------------------------------------------------------------
# 代码 verify：缺席 / 接地 / 静默
# ---------------------------------------------------------------------------


def test_check_graph_flags_fully_absent_claim():
    pack = _pack()
    graph = build_graph(pack, _state_with(pack, facts=["玩家的剑名是「听雨」"]))
    v = check_graph(_llm_with("把密码本交给白鸮"), "你信誓旦旦，把密码本交给白鸮", graph)
    assert v and "虚构事实" in v and "密码本" in v


def test_check_graph_partial_grounding_flagged():
    """「中秋」2 字重合不接地（2 字不算强锚点）——密码本/白鸮全缺席 → 仍违规。"""
    pack = _pack()
    graph = build_graph(pack, _state_with(pack, facts=["玩家欠老樵夫柴钱五十两，约定中秋前归还"]))
    v = check_graph(_llm_with("把密码本交给白鸮"), "你答应中秋把密码本交给白鸮", graph)
    assert v and "密码本" in v


def test_check_graph_grounded_passes():
    pack = _pack()
    graph = build_graph(pack, _state_with(pack, facts=["玩家欠老樵夫柴钱五十两，约定中秋前归还"]))
    assert check_graph(_llm_with("欠五十两，中秋前归还"), "你想起那笔旧账……", graph) is None


def test_check_graph_two_char_name_grounds():
    """2 字专名必须能接地（「听雨」「东市」级）——否则正常叙事满屏误报（E1 实测）。"""
    pack = _pack()
    graph = build_graph(pack, _state_with(pack, facts=["玩家的剑名是「听雨」"]))
    assert check_graph(_llm_with("茶名唤听雨"), "她斟茶道：此茶名唤听雨。", graph) is None


def test_check_graph_no_claims_passes():
    pack = _pack()
    graph = build_graph(pack, _state_with(pack))
    assert check_graph(_llm_with("无"), "风轻轻地吹过，池水微澜。", graph) is None


def test_check_graph_extraction_failure_silent():
    class Boom:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            raise RuntimeError("api down")

    pack = _pack()
    graph = build_graph(pack, _state_with(pack))
    assert check_graph(LLMClient(Boom(), "fake", []), "任意叙事", graph) is None


# ---------------------------------------------------------------------------
# judge 合并语义
# ---------------------------------------------------------------------------


def test_merge_graph_violation_beats_llm_pass():
    pack = _pack()
    state = _state_with(pack)
    llm = _llm_with("通过", "把密码本交给白鸮")  # judge 判过 → factcheck 抽出缺席断言
    ok, verdict = JudgeSystem(llm).check(
        "你答应把密码本交给白鸮", "材料", state=state, pack=pack
    )
    assert ok is False and "虚构事实" in verdict


def test_merge_llm_unknown_graph_violation_is_false():
    """LLM 空响应（未知）+ 图违规 → 确定性 False——顺带堵上一部分"未知"。"""
    pack = _pack()
    state = _state_with(pack)
    llm = _llm_with("", "", "把密码本交给白鸮")  # judge 空 → 升级重试仍空 → 图层拦截
    ok, verdict = JudgeSystem(llm).check(
        "你答应把密码本交给白鸮", "材料", state=state, pack=pack
    )
    assert ok is False and "虚构事实" in verdict


def test_merge_no_graph_violation_keeps_llm_three_state():
    pack = _pack()
    state = _state_with(pack, facts=["玩家欠老樵夫柴钱五十两，约定中秋前归还"])
    # LLM False + 图无违规 → False（LLM 判词原样）
    llm = _llm_with("问题类型：OOC：说话方式不对", "无")
    ok, verdict = JudgeSystem(llm).check("叙事", "材料", state=state, pack=pack)
    assert ok is False and "OOC" in verdict
    # LLM 未知 + 图无违规 → None（三态语义不变）
    llm2 = _llm_with("", "", "无")
    ok2, verdict2 = JudgeSystem(llm2).check("叙事", "材料", state=state, pack=pack)
    assert ok2 is None and verdict2 == ""


def test_check_without_state_pack_skips_graph():
    """向后兼容：不传 state/pack → 不跑 factcheck（响应耗尽即证）。"""
    llm = _llm_with("通过")
    ok, _ = JudgeSystem(llm).check("叙事", "材料")
    assert ok is True


# ---------------------------------------------------------------------------
# 语料同源
# ---------------------------------------------------------------------------


def test_build_state_and_materials_same_source():
    pack = _pack()
    case = JudgeCase(
        id="t1", category="normal", narration="x", expected=True,
        day=3, scene="东市", present=("shen_qingqiu",),
        facts=("玩家的剑名是「听雨」",),
    )
    state = build_state(pack, case)
    assert build_materials(pack, case) == ContextBuilder.from_pack(pack).status_text(state, None)

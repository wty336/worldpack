"""E1 Judge 灵敏度语料与管道测试（离线，P0 / improvement-roadmap §7 E1）。

覆盖：
- 语料结构完整性（每类数量、id 唯一、expected 合法）；
- 材料构造（与生产同款 status_text，判定依据可见、确定性）；
- Judge 管道（fake LLM）：判定解析、材料/叙事入参、temperature=0、失败静默降级。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp

from game_agent.judge import JudgeSystem, parse_verdict
from game_agent.judge_corpus import (
    ADVERSARIAL_CATEGORIES,
    CATEGORIES,
    NORMAL_CATEGORY,
    JudgeCase,
    build_materials,
    load_corpus,
)
from game_agent.llm import LLMClient
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

CORPUS = load_corpus(PACK_PATH)  # C-1：语料随世界包（内容层资产）


# ---------------------------------------------------------------------------
# 语料结构
# ---------------------------------------------------------------------------


def test_corpus_minimum_counts():
    for cat in ADVERSARIAL_CATEGORIES:
        n = sum(1 for c in CORPUS if c.category == cat)
        assert n >= 5, f"{cat} 类对抗样本不足 5 条（当前 {n}）"
    n_normal = sum(1 for c in CORPUS if c.category == NORMAL_CATEGORY)
    assert n_normal >= 10, f"正常样本不足 10 条（当前 {n_normal}）"


def test_corpus_ids_unique_and_categories_valid():
    ids = [c.id for c in CORPUS]
    assert len(ids) == len(set(ids)), f"语料 id 重复: {ids}"
    for c in CORPUS:
        assert c.category in CATEGORIES, f"非法类别 {c.category}（{c.id}）"
        assert isinstance(c.expected, bool)
        assert c.narration.strip(), f"{c.id} 叙事为空"
        assert c.category == NORMAL_CATEGORY or c.note, f"{c.id} 对抗样本缺 note（判定依据）"


def test_adversarial_expected_false_normal_true():
    for c in CORPUS:
        if c.category == NORMAL_CATEGORY:
            assert c.expected is True, c.id
        else:
            assert c.expected is False, c.id


# ---------------------------------------------------------------------------
# 材料构造
# ---------------------------------------------------------------------------


def test_materials_are_production_status_text():
    pack = load_worldpack(PACK_PATH)
    case = JudgeCase(id="t", category="normal", narration="x", expected=True)
    materials = build_materials(pack, case)
    assert "<agent_status>" in materials
    assert "<scene>" in materials
    assert "玩家属性" in materials


def test_materials_contain_ooc_judgment_basis():
    """OOC 用例：材料必须含角色卡（人设/语气/底线是判定依据）。"""
    pack = load_worldpack(PACK_PATH)
    case = next(c for c in CORPUS if c.id == "ooc_public_affair")
    materials = build_materials(pack, case)
    assert "沈清秋" in materials
    assert "不在公共场合谈论私情" in materials  # 角色卡底线
    assert "身份：沈家嫡女" in materials


def test_materials_contain_fact_and_memory_basis():
    """confab/setting 用例：对照事实必须出现在材料中，否则测量无效。"""
    pack = load_worldpack(PACK_PATH)
    case = next(c for c in CORPUS if c.id == "confab_proposal")
    materials = build_materials(pack, case)
    assert "止于诗词之交" in materials
    mem_case = next(c for c in CORPUS if c.id == "setting_memory_conflict")
    mem_materials = build_materials(pack, mem_case)
    assert "玩家曾在东市帮沈清秋解围" in mem_materials


def test_materials_deterministic_and_scene_driven():
    pack = load_worldpack(PACK_PATH)
    case = next(c for c in CORPUS if c.id == "setting_time_conflict")
    assert build_materials(pack, case) == build_materials(pack, case)
    assert "第 12 天" in build_materials(pack, case)
    pond = next(c for c in CORPUS if c.id == "normal_night_pond")
    assert "沈府后院" in build_materials(pack, pond)


# ---------------------------------------------------------------------------
# Judge 管道（fake LLM）
# ---------------------------------------------------------------------------


def _judge_with(responses):
    pack = load_worldpack(PACK_PATH)
    llm = LLMClient(FakeClient(responses), "fake", [])
    return pack, JudgeSystem(llm)


def test_judge_flags_and_passes():
    pack, judge = _judge_with([resp(msg(content="OOC：沈清秋说出网络用语。"))])
    case = next(c for c in CORPUS if c.id == "ooc_net_slang")
    ok, verdict = judge.check(case.narration, build_materials(pack, case))
    assert ok is False and "OOC" in verdict

    pack2, judge2 = _judge_with([resp(msg(content="通过"))])
    ok2, _ = judge2.check("正常叙事。", "材料")
    assert ok2 is True


def test_judge_sends_materials_and_temperature_zero():
    pack, judge = _judge_with([resp(msg(content="通过"))])
    case = next(c for c in CORPUS if c.id == "normal_shen_tea")
    materials = build_materials(pack, case)
    judge.check(case.narration, materials)
    call = judge.llm._client.chat.completions.calls[0]
    assert call["temperature"] == 0.0
    assert case.narration in call["messages"][-1]["content"]
    assert "听雨" in call["messages"][-1]["content"]  # 材料被送入


def test_judge_silent_degradation_on_api_error():
    class _Boom:
        chat = None

        def __init__(self):
            self.chat = type("C", (), {"completions": type("CC", (), {"create": self._fail})()})()

        @staticmethod
        def _fail(**kwargs):
            raise RuntimeError("api down")

    judge = JudgeSystem(LLMClient(_Boom(), "fake", []))
    ok, verdict = judge.check("任意叙事", "材料")
    assert (ok, verdict) == (True, "")  # 失败静默降级，不影响主线


def test_parse_verdict_variants():
    assert parse_verdict("通过。")[0] is True
    assert parse_verdict(" 通过 ")[0] is True
    assert parse_verdict("问题类型：设定矛盾：……")[0] is False
    assert parse_verdict("虚构事实：他编造了约定。")[0] is False

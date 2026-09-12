"""第二个世界包《问道长生》验收 + 引擎跨世界通用性测试（M3「换包即玩」）。

覆盖：
- 新包 schema/交叉校验与字段断言（属性 key/好感/NPC/事件/结局与《江湖旧梦》完全不同）；
- 全部世界包一键加载——引擎对任意故事无内容耦合（属性名等全部由包声明）；
- FakeClient 离线通关 ×2：走向「问道长生」与「尘缘未了」两条结局路径，
  覆盖 3 个关键抉择节点、时间触发事件、检定三档、收益曲线与门槛；
- 数值零偏差审计 + E1 Judge 语料资产可用性。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.audit import audit_stats
from game_agent.game import Game
from game_agent.judge_corpus import build_materials, load_corpus
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_ROOT = REPO_ROOT / "world-packs"
NEW_PACK = PACKS_ROOT / "xianxia_wendao"

SUBMIT = tool_call(
    "s1",
    "submit_narration",
    {"narration": "测试叙事", "choices": ["行动一", "行动二", "行动三"], "plot_signal": "normal"},
)
SUBMIT_EVENT = tool_call(
    "s2",
    "submit_narration",
    {"narration": "事件叙事", "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
)


class _NeverRng:
    """恒不触发日程概率事件（random()=0.9 > 0.3）；uniform 用于检定与收益曲线（确定性）。"""

    def random(self):
        return 0.9

    def uniform(self, a, b):
        return a + (b - a) * self.random()


def _game(responses, mutate=None):
    pack = load_worldpack(NEW_PACK)
    state = GameState.from_pack(pack)
    if mutate is not None:
        mutate(state)
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm, rng=_NeverRng())


# ---------------------------------------------------------------------------
# 加载与字段断言
# ---------------------------------------------------------------------------


def test_load_xianxia_wendao():
    pack = load_worldpack(NEW_PACK)
    assert pack.world.name == "问道长生"
    assert "苍梧界" in pack.world.era
    # 属性 key 与《江湖旧梦》完全不同：证明 change_stat 枚举/状态栏随包生成
    assert set(pack.schedule.stats) == {"dao_xin", "xiu_wei", "ling_shi"}
    assert set(pack.schedule.affections) == {"su_wanying", "bai_zhi"}
    assert set(pack.npcs) == {"su_wanying", "bai_zhi"}
    assert len(pack.mainline.nodes) == 3
    assert len(pack.events.events) == 4
    assert len(pack.endings.endings) == 4
    assert len(pack.world.lore) == 7
    # 时间触发事件（ancient_jianghu 未覆盖的引擎路径，本包验证通用性）
    assert any(e.trigger.kind == "time" for e in pack.events.events)
    # 好感阶段区间升序且覆盖 0~100（双 NPC）
    for npc in pack.npcs.values():
        stages = npc.affection_stages
        assert stages[0].range[0] == 0 and stages[-1].range[1] == 100
        assert all(a.range[1] >= a.range[0] for a in stages)
    # P2 玩法特性在新包同样可用：检定三档 + 执行门槛 + 收益曲线
    meditate = next(a for a in pack.schedule.actions if a.id == "meditate")
    assert meditate.check is not None and meditate.critical_effects is not None
    gift = next(a for a in pack.schedule.actions if a.id == "gift_pill")
    assert gift.requires is not None


def test_schema_is_fully_pack_defined():
    """两个世界包的核心 schema 无交集：引擎没有任何内置属性名/好感对象/结局。"""
    first = load_worldpack(PACKS_ROOT / "ancient_jianghu")
    second = load_worldpack(NEW_PACK)
    assert set(first.schedule.stats) & set(second.schedule.stats) == set()
    assert set(first.schedule.affections) & set(second.schedule.affections) == set()
    assert first.world.era != second.world.era
    assert first.world.name != second.world.name


def test_all_worldpacks_load_without_engine_coupling():
    """通用性门禁：world-packs/ 下每个包都能被同一个引擎加载校验。

    引擎层零硬编码的世界内容——属性/好感/剧情全部由包声明，加载器对
    任意故事只做 schema + 交叉引用校验。
    """
    packs = sorted(
        p for p in PACKS_ROOT.iterdir() if p.is_dir() and (p / "world.yaml").exists()
    )
    assert len(packs) >= 4  # ancient_jianghu + baseline_probe + xianxia_wendao + urban_neon
    for root in packs:
        pack = load_worldpack(root)
        assert pack.world.name
        for aff_id in pack.schedule.affections:
            assert aff_id in pack.npcs  # 好感对象必有角色卡
        for stat, spec in pack.schedule.stats.items():
            assert spec.label and spec.min <= spec.initial <= spec.max


# ---------------------------------------------------------------------------
# 离线通关（FakeClient）：结局可达性 + 全机制链路
# ---------------------------------------------------------------------------


def test_offline_playthrough_reaches_changsheng_ending():
    """走向「问道长生」：剑峰入门 → 秘境取碑 → 连修五日 → 独闯敌阵。

    覆盖：3 个关键抉择节点、时间事件（第 5 天宗门小比）、道心检定三档、
    收益曲线边际递减、结局代码判定。全程 10 个 LLM 回合。
    """
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT])) for _ in range(10)])

    view = game.start()  # N1 收徒大典：关键抉择
    assert state.current_node == "n1_entrance"
    assert view.choice_prompt.id == "choose_peak"
    view = game.pick(0)  # 拜入剑峰
    assert state.flags["joined_sect"] and state.flags["peak_sword"]
    assert state.current_node is None

    view = game.act("meditate")  # 第 1 天
    game.end_day()
    view = game.act("meditate")  # 第 2 天
    game.end_day()  # 第 3 天

    view = game.say("去秘境看看")  # N2 秘境历练触发（day≥3 且已入门）
    assert state.current_node == "n2_secret_realm"
    assert view.choice_prompt.id == "relic_choice"
    view = game.pick(1)  # 先取残碑拓纹
    assert state.flags["relic_taken"] and state.flags["realm_done"]
    assert state.current_node is None

    for _ in range(5):  # 第 3~7 天连修，第 5 天触发时间事件「宗门小比」
        view = game.act("meditate")
        game.end_day()
    assert "ev_sect_contest" in state.triggered_events  # time 类事件路径 ✓

    view = game.say("去山门看看")  # N3 魔潮夜袭触发（day≥8 且秘境已归）
    assert state.current_node == "n3_night_raid"
    view = game.pick(2)  # 独闯敌阵
    assert state.flags["crisis_done"] and state.flags["lone_strike"]
    assert view.ending is not None and view.ending.id == "ending_changsheng"
    # 数值零偏差审计：stat_log 回放与最终状态完全一致
    assert audit_stats(pack, state) == []


def test_offline_playthrough_reaches_guichen_ending():
    """走向「尘缘未了」：一路疏于修行，第 12 天修为不足 30 → 下山结局。

    覆盖：条件互斥的结局判定（高优先结局条件不满足时落到兜底结局）。
    """
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT])) for _ in range(3)])

    game.start()
    game.pick(0)  # 拜入剑峰（N1 完成）
    for _ in range(3):
        game.end_day()  # 第 4 天
    view = game.say("去秘境看看")  # N2 触发
    game.pick(0)  # 弃碑救人（不加修为）
    for _ in range(8):
        game.end_day()  # 第 12 天
    view = game.say("去山门看看")  # N3 触发
    view = game.pick(1)  # 护送撤退（+道心，不加修为）
    assert state.stats["xiu_wei"] < 30
    assert view.ending is not None and view.ending.id == "ending_guichen"
    assert audit_stats(pack, state) == []


# ---------------------------------------------------------------------------
# E1 Judge 语料（内容层资产随包）
# ---------------------------------------------------------------------------


def test_judge_corpus_loads_and_materials_build():
    """新包自带 Judge 语料：结构合法，且材料构造与生产同款（判定依据可见）。"""
    corpus = load_corpus(NEW_PACK)
    # 契约：手写 30 条 + Phase 1 扩域生成物（每包 ≥60，setting/confab 各 ≥20）。
    # 不再断言 "==30"——扩域后各包计数随种子略有差异（见 scripts/build_judge_corpus.py）
    categories = [c.category for c in corpus]
    assert len(corpus) >= 60
    assert categories.count("setting") >= 20 and categories.count("confab") >= 20
    ids = [c.id for c in corpus]
    assert len(ids) == len(set(ids))
    pack = load_worldpack(NEW_PACK)
    for case in corpus:
        materials = build_materials(pack, case)
        assert "<agent_status>" in materials and "<scene>" in materials
    # OOC 用例的材料必须包含角色卡底线（判定依据）
    ooc = next(c for c in corpus if c.id == "ooc_fourth_wall")
    materials = build_materials(pack, ooc)
    assert "白芷" in materials and "说破" in materials


def test_normal_sect_morning_carries_joined_premise():
    """材料纪律回归（E1 门禁 2026-09-08 修复）：叙事前提"已入门"必须作为事实进材料。

    此前材料只有目标「拜入青云仙宗」，Judge 合理推断"尚未拜入"→ 误判设定矛盾。
    """
    pack = load_worldpack(NEW_PACK)
    case = next(c for c in load_corpus(NEW_PACK) if c.id == "normal_sect_morning")
    materials = build_materials(pack, case)
    assert "已拜入青云仙宗" in materials


def test_condition_event_sword_pool_chains_narration():
    """条件事件（苏晚晴好感≥30）在同一回合级联第二段叙事并结算效果（② 的离线回归）。"""
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT])), resp(msg(tool_calls=[SUBMIT_EVENT]))],
        mutate=lambda s: (
            s.completed_nodes.append("n1_entrance"),
            s.flags.__setitem__("joined_sect", True),
            s.affections.__setitem__("su_wanying", 30.0),
        ),
    )
    view = game.say("夜深了")
    assert "测试叙事" in view.narration and "事件叙事" in view.narration
    assert state.affections["su_wanying"] == 35.0  # 事件效果 +5
    assert "ev_sword_pool" in state.triggered_events
    assert audit_stats(pack, state) == []

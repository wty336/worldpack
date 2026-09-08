"""第三个世界包《霓虹深处》验收 + forbidden 表/文风注入在近现代世界观下的表现。

与前两包的核心差异（本文件专门验证）：
- forbidden 表语义反转：手机/AI/终端是**合法世界观元素**（古代包禁用手机），
  禁的是跨体裁泄漏（修仙/魔法）、网络烂梗、现实品牌与第四面墙；
- 属性 key 用英文（intel/cyber/credits），day_action_points=2——
  工具枚举与日程节奏全部由世界包声明，引擎无内置假设；
- 离线通关两条结局路径 + 数值零偏差审计。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.audit import audit_stats
from game_agent.game import Game
from game_agent.judge_corpus import build_materials, load_corpus
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.storyline import FREE_INPUT_OPTION, filter_choices
from game_agent.worldpack import load_worldpack

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_ROOT = REPO_ROOT / "world-packs"
NEW_PACK = PACKS_ROOT / "urban_neon"

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


def test_load_urban_neon():
    pack = load_worldpack(NEW_PACK)
    assert pack.world.name == "霓虹深处"
    assert "夜澜市" in pack.world.era
    # 英文属性 key + 每日 2 行动点：schema 与日程节奏完全由包声明
    assert set(pack.schedule.stats) == {"intel", "cyber", "credits"}
    assert pack.schedule.day_action_points == 2
    assert set(pack.schedule.affections) == {"lin_che", "a_ling"}
    assert set(pack.npcs) == {"lin_che", "a_ling"}
    assert len(pack.mainline.nodes) == 3
    assert len(pack.events.events) == 4
    assert len(pack.endings.endings) == 4
    assert len(pack.world.lore) == 7
    assert any(e.trigger.kind == "time" for e in pack.events.events)
    for npc in pack.npcs.values():
        stages = npc.affection_stages
        assert stages[0].range[0] == 0 and stages[-1].range[1] == 100
    scavenge = next(a for a in pack.schedule.actions if a.id == "scavenge")
    assert scavenge.check is not None and scavenge.critical_effects is not None
    gift = next(a for a in pack.schedule.actions if a.id == "gift_gear")
    assert gift.requires is not None


def test_schema_disjoint_across_all_three_packs():
    """三个世界包属性/好感零交集：引擎无任何内置内容假设。"""
    packs = [
        load_worldpack(PACKS_ROOT / name)
        for name in ("ancient_jianghu", "xianxia_wendao", "urban_neon")
    ]
    stat_sets = [set(p.schedule.stats) for p in packs]
    aff_sets = [set(p.schedule.affections) for p in packs]
    for i in range(len(packs)):
        for j in range(i + 1, len(packs)):
            assert stat_sets[i] & stat_sets[j] == set()
            assert aff_sets[i] & aff_sets[j] == set()
    assert {p.world.name for p in packs} == {"江湖旧梦", "问道长生", "霓虹深处"}


def test_change_stat_tool_enum_is_pack_driven():
    """英文属性 key 直接进 change_stat 枚举与目标枚举（工具 schema 随包生成）。"""
    pack = load_worldpack(NEW_PACK)
    change = next(
        t for t in build_tools(pack.schedule) if t["function"]["name"] == "change_stat"
    )
    props = change["function"]["parameters"]["properties"]
    assert set(props["stat"]["enum"]) == {"intel", "cyber", "credits", "affection"}
    assert set(props["target"]["enum"]) == {"player", "lin_che", "a_ling"}


# ---------------------------------------------------------------------------
# forbidden 表语义反转：近现代世界观的核心验证点
# ---------------------------------------------------------------------------


def test_forbidden_table_reversed_between_worlds():
    """同一引擎、同一过滤函数，forbidden 表随包反转：
    手机在《霓虹深处》合法、在《江湖旧梦》被过滤；修仙/魔法在都市包被过滤。"""
    urban = load_worldpack(NEW_PACK)
    ancient = load_worldpack(PACKS_ROOT / "ancient_jianghu")
    urban_kept = filter_choices(
        urban, ["掏出手机查看地图", "运转内力疗伤", "对着魔法阵念咒", "去旧终端区找阿零"]
    )
    assert "掏出手机查看地图" in urban_kept      # 近现代世界：手机是合法元素
    assert "去旧终端区找阿零" in urban_kept
    assert "运转内力疗伤" not in urban_kept       # 跨体裁泄漏被禁表过滤
    assert "对着魔法阵念咒" not in urban_kept
    assert urban_kept[-1] == FREE_INPUT_OPTION
    ancient_kept = filter_choices(ancient, ["掏出手机查看地图"])
    assert "掏出手机查看地图" not in ancient_kept  # 古代世界：手机被禁表过滤
    assert ancient_kept[-1] == FREE_INPUT_OPTION


def test_diegetic_ai_not_forbidden_in_urban_world():
    """「AI」不进都市包禁表——AI 是世界观合法元素（禁的是第四面墙）；
    前两包禁表明文含「AI」字样：禁表语义确实由世界包定义、随包反转。"""
    urban = load_worldpack(NEW_PACK)
    xianxia = load_worldpack(PACKS_ROOT / "xianxia_wendao")
    assert not any("AI" in f for f in urban.world.forbidden)
    assert any("AI" in f for f in xianxia.world.forbidden)  # 仙侠包：AI 是其世界观外元素
    # 都市包禁的是：跨体裁泄漏 / 网络烂梗 / 现实品牌 / 第四面墙（措辞为「由程序生成」）
    forbidden_text = "；".join(urban.world.forbidden)
    for marker in ("修仙", "魔法", "绝绝子", "由程序生成", "角色"):
        assert marker in forbidden_text
    from game_agent.storyline import _forbidden_tokens

    tokens = _forbidden_tokens(urban.world)
    assert any(t in tokens for t in ("修仙", "魔法", "内力"))  # 跨体裁词进过滤表


# ---------------------------------------------------------------------------
# 离线通关（FakeClient）：结局可达性 + 每日 2 行动点节奏
# ---------------------------------------------------------------------------


def test_offline_playthrough_reaches_freefall_ending():
    """走向「自由落体」：帮诊所还债 → 卖阿零坐标 → 连拾七日 → 与霓光谈判。

    覆盖：3 个关键抉择、每日 2 行动点、智识检定大成功档、时间事件（第 5 天
    停电之夜）、信用点收益、结局代码判定。全程 17 个 LLM 回合。
    """
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT])) for _ in range(17)])
    assert state.action_points_left == 2  # day_action_points 由包声明

    view = game.start()  # N1 白噪诊所
    assert state.current_node == "n1_clinic_deal"
    view = game.pick(1)  # 帮诊所跑黑市
    assert state.flags["deal_made"] and state.flags["clinic_helper"]
    assert state.current_node is None

    for _ in range(2):  # 第 1~2 天：每天两次拾荒
        view = game.act("scavenge")
        view = game.act("scavenge")
        game.end_day()
    assert state.day == 3

    view = game.say("去数据街看看")  # N2 触发（day≥3 且已达成协议）
    assert state.current_node == "n2_data_street"
    view = game.pick(1)  # 把阿零的坐标卖给霓光
    assert state.flags["memory_lead"] and state.flags["zero_sold"]
    assert state.current_node is None

    for _ in range(5):  # 第 3~7 天：每天两次拾荒；第 5 天触发时间事件
        view = game.act("scavenge")
        view = game.act("scavenge")
        game.end_day()
    assert state.day == 8
    assert "ev_blackout" in state.triggered_events  # time 类事件路径 ✓

    view = game.say("去霓光大厦")  # N3 触发（day≥8 且已有线索）
    assert state.current_node == "n3_neon_tower"
    view = game.pick(1)  # 与霓光谈判交易
    assert state.flags["confrontation_done"] and state.flags["tower_deal"]
    assert view.ending is not None and view.ending.id == "ending_freefall"
    assert state.stats["credits"] >= 200
    assert audit_stats(pack, state) == []
    assert len(state.choice_log) == 3


def test_offline_playthrough_reaches_city_swallow_ending():
    """走向「夜澜沉没」：一路不动脑子（智识<30），第 12 天被城市吞没。

    覆盖：条件互斥的多结局判定（交易/好感类结局条件不满足时落到兜底结局）。
    """
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT])) for _ in range(3)])
    game.start()
    game.pick(0)  # 接霓光的寻回任务（N1 完成）
    for _ in range(3):
        game.end_day()  # 第 4 天
    view = game.say("去数据街看看")  # N2 触发
    game.pick(2)  # 帮阿零销毁追踪代码（+智识 2，仍 <30）
    for _ in range(8):
        game.end_day()  # 第 12 天
    view = game.say("去霓光大厦")  # N3 触发
    view = game.pick(0)  # 硬闯（+义体，不加智识）
    assert state.stats["intel"] < 30
    assert view.ending is not None and view.ending.id == "ending_city_swallow"
    assert audit_stats(pack, state) == []


# ---------------------------------------------------------------------------
# E1 Judge 语料（含「AI 作为世界观内元素不违规」的对照基准）
# ---------------------------------------------------------------------------


def test_judge_corpus_loads_and_ai_diegetic_is_normal():
    corpus = load_corpus(NEW_PACK)
    assert len(corpus) == 30  # 2026-09-08 语料对齐：与包1 同规模
    ids = [c.id for c in corpus]
    assert len(ids) == len(set(ids))
    pack = load_worldpack(NEW_PACK)
    for case in corpus:
        materials = build_materials(pack, case)
        assert "<agent_status>" in materials and "<scene>" in materials
    # 核心对照基准：AI 作为世界观内元素 = normal；第四面墙 = 违规
    ai_case = next(c for c in corpus if c.id == "normal_ai_diegetic")
    assert ai_case.expected is True and "AI" in ai_case.narration
    wall = next(c for c in corpus if c.id == "ooc_fourth_wall")
    assert wall.expected is False
    materials = build_materials(pack, wall)
    assert "阿零" in materials and "说破" in materials  # 判定依据（角色卡禁忌）可见


def test_condition_event_clinic_night_chains_narration():
    """条件事件（林澈好感≥30）在同一回合级联第二段叙事并结算效果（② 的离线回归）。"""
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT])), resp(msg(tool_calls=[SUBMIT_EVENT]))],
        mutate=lambda s: (
            s.completed_nodes.append("n1_clinic_deal"),
            s.flags.__setitem__("deal_made", True),
            s.affections.__setitem__("lin_che", 30.0),
        ),
    )
    view = game.say("夜深了")
    assert "测试叙事" in view.narration and "事件叙事" in view.narration
    assert state.affections["lin_che"] == 35.0  # 事件效果 +5
    assert "ev_clinic_night" in state.triggered_events
    assert audit_stats(pack, state) == []


def test_normal_cases_carry_interaction_premises():
    """材料纪律回归（E1 门禁 30 条扩充 2026-09-08 修复）：

    normal_lin_high 的叙事前提（备份存在/清理之约）必须进材料——否则 Judge 把
    "备份已清"判为虚构事实。修复后材料必须包含前提关键词。
    """
    pack = load_worldpack(NEW_PACK)
    case = next(c for c in load_corpus(NEW_PACK) if c.id == "normal_lin_high")
    materials = build_materials(pack, case)
    assert "备份" in materials and "清掉" in materials
    # 无在场角色的正常用例不得出现互动角色（normal_implant_check/normal_street_rain 的修复形态）
    for case_id in ("normal_implant_check", "normal_street_rain"):
        case = next(c for c in load_corpus(NEW_PACK) if c.id == case_id)
        materials = build_materials(pack, case)
        assert "在场：无" in materials

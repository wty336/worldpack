"""第四个世界包《青槐高中·告白之前》验收 + 乙游养成机制在引擎上的表现。

与前几包的核心差异（本文件专门验证）：
- 题材同为现代校园，但玩法轴是**乙游养成**：4 个好感对象（3 攻略 + 1 闺蜜）、
  5 条养成轴（学力/仪态/才艺/体力/零花）、好感阶段驱动角色语气、
  路线 flag × 秘密 flag × 亲笔信决定 10 个结局的判定顺序；
- forbidden 表方向：手机/微信/便利店是**合法世界观元素**，禁的是跨体裁泄漏
  （修仙/魔法/灵力）、古风腔调、网络烂梗、第四面墙——同一个 filter_choices 随包反转；
- 机制覆盖：地点表（8）/ counters（同行·赠礼）/ items（材料→点心→赠礼）/
  检定三档 / 自定义工具 write_letter（once + 计数门槛）/ 三类事件触发。
"""

from __future__ import annotations

from collections import Counter
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
NEW_PACK = PACKS_ROOT / "campus_otome"

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
LETTER = tool_call("t1", "write_letter", {})

ALL_NODES = ("n1_transfer", "n2_club", "n3_midterm", "n4_festival", "n5_confession")


class _NeverRng:
    """确定性随机：random()=0.9 → uniform(-10,10)=+8（检定稳定落"成功"档）。"""

    def random(self):
        return 0.9

    def uniform(self, a, b):
        return a + (b - a) * self.random()


def _game(responses, mutate=None, action_points=None):
    pack = load_worldpack(NEW_PACK)
    state = GameState.from_pack(pack)
    if action_points is not None:
        state.action_points_left = action_points
    if mutate is not None:
        mutate(state)
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm, rng=_NeverRng())


def _finish_mainline(s: GameState) -> None:
    """把主线推到走完（结局用例用）：否则 begin_turn 会先接管关键抉择，不出结局。"""
    s.completed_nodes.extend(ALL_NODES)
    s.flags["confession_done"] = True


def _action_ids(game) -> set[str]:
    return {a.id for a in game.actions_available()}


def _play_days(game, state, action: str, days: int):
    """连打若干天（用光当天行动点 + 跨天），返回首个接管视图（关键抉择 / 结局）。"""
    view = None
    for _ in range(days):
        while state.action_points_left > 0:
            view = game.act(action)
            if view.choice_prompt is not None or view.ending is not None:
                return view
        view = game.end_day()
        if view.choice_prompt is not None or view.ending is not None:
            return view
    return view


# ---------------------------------------------------------------------------
# 加载与字段断言：schema 完全由包声明
# ---------------------------------------------------------------------------


def test_load_campus_otome():
    pack = load_worldpack(NEW_PACK)
    assert pack.world.name == "青槐高中·告白之前"
    assert "青槐高中" in pack.world.era
    # 五条养成轴 + 每日 2 行动点：节奏与属性全部由包声明
    assert set(pack.schedule.stats) == {"study", "grace", "art", "stamina", "money"}
    assert pack.schedule.day_action_points == 2
    # 乙游结构：3 名攻略对象 + 1 名闺蜜，各有角色卡
    assert set(pack.schedule.affections) == {"jiang_yu", "xia_ming", "wen_yan", "su_qing"}
    assert set(pack.npcs) == set(pack.schedule.affections)
    assert len(pack.mainline.nodes) == 5
    assert len(pack.events.events) == 13
    assert len(pack.endings.endings) == 10
    assert len(pack.world.lore) == 8
    assert len(pack.world.locations) == 8
    assert set(pack.schedule.counters) == {"dates", "gifts"}
    assert {i.id for i in pack.schedule.items} == {"ingredient", "cookie"}
    assert [t.id for t in pack.schedule.tools] == ["write_letter"]
    # 好感阶段区间升序覆盖 0~100（语气档是 Judge 的判定依据）
    for npc in pack.npcs.values():
        assert npc.affection_stages[0].range[0] == 0
        assert npc.affection_stages[-1].range[1] == 100
        for (lo, hi), nxt in zip(
            [s.range for s in npc.affection_stages], npc.affection_stages[1:]
        ):
            assert lo <= hi < nxt.range[1]


def test_location_table_is_scene_truth():
    """地点表声明后场景是引擎真值：行动/节点/开场都必须落在表内，表外移动被拒。"""
    pack = load_worldpack(NEW_PACK)
    names = {l.name for l in pack.world.locations}
    ids = {l.id for l in pack.world.locations}
    assert pack.world.start_scene in names
    for action in pack.schedule.actions:
        assert not action.scene or action.scene in names, action.id
    for node in pack.mainline.nodes:
        assert node.on_enter.scene in names, node.id
    _, state, game = _game([])
    bad = game.registry.dispatch("change_scene", {"location": "青槐高中·校长室", "reason": "想进去看看"})
    assert bad.status == "rejected" and "未声明的地点" in bad.message
    ok = game.registry.dispatch("change_scene", {"location": "rooftop", "reason": "想去天台吹风"})
    assert ok.status == "ok" and state.scene == "青槐高中·天台" and state.scene_id == "rooftop"
    assert ids  # 表内 id 亦可作为移动目标


# ---------------------------------------------------------------------------
# forbidden 表方向：现代校园里手机是合法元素，禁的是跨体裁泄漏与古风腔调
# ---------------------------------------------------------------------------


def test_forbidden_table_reversed_for_modern_campus():
    pack = load_worldpack(NEW_PACK)
    kept = filter_choices(
        pack,
        [
            "掏出手机看班级群消息",
            "在微信上问苏晴作业写到哪了",
            "运转灵力替自己疗伤",
            "公子请留步，容我解释",
            "去便利店买烘焙材料",
        ],
    )
    assert "掏出手机看班级群消息" in kept          # 现代校园：手机是合法世界观元素
    assert "在微信上问苏晴作业写到哪了" in kept
    assert "去便利店买烘焙材料" in kept
    assert "运转灵力替自己疗伤" not in kept         # 跨体裁泄漏被禁表过滤
    assert "公子请留步，容我解释" not in kept        # 古风腔调被禁表过滤
    assert kept[-1] == FREE_INPUT_OPTION
    # 同一函数、古代包方向相反：手机在那里是世界观外元素
    ancient = load_worldpack(PACKS_ROOT / "ancient_jianghu")
    assert filter_choices(ancient, ["掏出手机看班级群消息"]) == [FREE_INPUT_OPTION]
    # 第四面墙进禁表（都市包同类口径），但"手机/微信"不入禁表
    forbidden_text = "；".join(pack.world.forbidden)
    assert "第四面墙" in forbidden_text and "手机" not in forbidden_text


# ---------------------------------------------------------------------------
# counters / items：零花 → 材料 → 点心 → 好感（乙游赠礼闭环）
# ---------------------------------------------------------------------------


def test_item_chain_buys_bakes_and_gives():
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT])) for _ in range(10)],
        mutate=_finish_mainline,          # 主线已走完：本用例只看机制链路
        action_points=10,
    )
    assert "bake_cookies" not in _action_ids(game)        # 手上没有材料
    assert "give_cookie_jiang" not in _action_ids(game)   # 手上没有点心

    game.act("buy_ingredients")                           # 零花 -20 → 得材料
    assert state.items == ["ingredient"] and state.stats["money"] == 280.0
    assert "buy_ingredients" not in _action_ids(game)     # 已有材料：不必再买（item requires）

    game.act("bake_cookies")                              # 材料 → 点心（+才艺 1）
    assert state.items == ["cookie"] and state.stats["art"] == 6.0
    assert "give_cookie_jiang" in _action_ids(game)

    game.act("give_cookie_jiang")                         # 点心 → 好感 + 赠礼计数
    assert state.items == []                              # 点心被消耗
    assert state.counters["gifts"] == 1.0
    assert state.affections["jiang_yu"] == 5.0            # 仪态 10 → 检定成功档 +5
    assert audit_stats(pack, state) == []


def test_travel_counter_accumulates_and_gates_letter():
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT])) for _ in range(5)],
        mutate=_finish_mainline,
        action_points=10,
    )
    for _ in range(3):
        game.act("visit_rooftop")
    assert state.counters["dates"] == 3.0
    assert state.affections["jiang_yu"] > 0
    assert audit_stats(pack, state) == []


def test_action_effects_write_flags_through_check_tiers():
    """回归（本包发现，2026-09-25）：`ActionEffects` 曾缺 `flags` 字段、`_dump_effects`
    也不转发它——pydantic 默认 extra="ignore" 让"行动效果写 flag"变成**静默 no-op**，
    而手册 §3.2/§5 守则 3 把它列为 flag 的合法写入路径之一（校验器里对应的
    writable_flags 收集因此对行动恒为空）。本用例锁住修复后的行为。"""
    from game_agent.schedule import _dump_effects
    from game_agent.worldpack import ActionEffects

    eff = ActionEffects(**{"stats": {"study": 1}, "flags": {"quiz_top": True}})
    assert eff.flags == {"quiz_top": True}                 # schema 层
    assert _dump_effects(eff)["flags"] == {"quiz_top": True}  # 执行层（apply_effects 读它）

    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT])) for _ in range(3)],   # 余量含事件级联轮
        mutate=lambda s: (
            _finish_mainline(s),
            s.__setattr__("day", 6),
            s.stats.__setitem__("study", 30.0),
        ),
    )
    game.act("mock_exam")                                  # roll = 30+8 = 38 ≥ 25+10 → 大成功
    assert state.flags["quiz_top"] is True
    assert "ev_quiz_result" in state.triggered_events       # 条件事件跟着 flag 级联
    assert audit_stats(pack, state) == []


def test_write_letter_tool_gated_by_counter_and_once():
    pack, state, game = _game([], action_points=5)
    tools = {t["function"]["name"] for t in build_tools(pack.schedule)}
    assert "write_letter" in tools
    early = game.registry.dispatch("write_letter", {})
    assert early.status == "rejected" and "写一封信" in early.message   # 同行 <3 次
    state.counters["dates"] = 3
    ok = game.registry.dispatch("write_letter", {})
    assert ok.status == "ok" and state.flags["wrote_letter"] is True
    assert state.stats["grace"] == 12.0                                # 工具效果走引擎结算
    again = game.registry.dispatch("write_letter", {})
    assert again.status == "rejected"                                  # once：整局只能写一次


def test_write_letter_through_llm_tool_call():
    """真链路：模型在叙事轮里调用 write_letter → 引擎结算 → 叙事照常返回。"""
    pack, state, game = _game(
        [resp(msg(tool_calls=[LETTER, SUBMIT]))],
        mutate=lambda s: (_finish_mainline(s), s.counters.__setitem__("dates", 3.0)),
    )
    view = game.say("我想把话写下来")
    assert state.flags["wrote_letter"] is True
    assert "测试叙事" in view.narration


# ---------------------------------------------------------------------------
# 事件：三类触发 + 秘密揭示（真结局前置）
# ---------------------------------------------------------------------------


def test_events_cover_three_trigger_kinds():
    pack = load_worldpack(NEW_PACK)
    kinds = Counter(e.trigger.kind for e in pack.events.events)
    assert set(kinds) == {"condition", "schedule", "time"}
    # time 事件的 when 只含 day（时间触发语义）
    for ev in pack.events.events:
        if ev.trigger.kind == "time":
            assert set(ev.trigger.when) == {"day"}
    # 三条线的秘密各有一个好感门槛事件，且效果写 secret flag + 好感
    for npc_id, flag in (("jiang_yu", "jiang_secret"), ("xia_ming", "xia_secret"), ("wen_yan", "wen_secret")):
        ev = next(e for e in pack.events.events if flag in (e.effects.get("flags") or {}))
        assert ev.effects["flags"][flag] is True
        assert npc_id in ev.effects["affections"]
        assert ev.trigger.kind == "condition"


def test_secret_event_fires_at_threshold_and_chains():
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT])), resp(msg(tool_calls=[SUBMIT_EVENT]))],
        mutate=lambda s: (_finish_mainline(s), s.affections.__setitem__("jiang_yu", 40.0)),
    )
    view = game.say("天台上风有点大")
    assert "ev_jiang_secret" in state.triggered_events
    assert state.flags["jiang_secret"] is True
    assert state.affections["jiang_yu"] == 45.0          # 事件效果 +5
    assert "测试叙事" in view.narration and "事件叙事" in view.narration
    assert audit_stats(pack, state) == []


# ---------------------------------------------------------------------------
# 离线通关：五日养成 → 考试周取舍 → 学园祭 → 告白之夜（结局由代码判定）
# ---------------------------------------------------------------------------


def test_offline_playthrough_reaches_jiang_ending():
    """走江屿线：初遇帮登记 → 进学生会 → 连打五日天台 → 考试周帮他 → 学园祭跑流程 → 放灯去天台。

    覆盖：5 个关键抉择、每日 2 行动点、检定三档（学力→好感）、同行计数器、
    条件事件（好感 40 揭秘）、结局判定顺序（无亲笔信 → 普通恋爱结局）。
    """
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT])) for _ in range(90)])
    assert state.action_points_left == 2

    view = game.start()
    assert state.current_node == "n1_transfer"
    assert view.choice_prompt.id == "first_day_choice"
    view = game.pick(0)                                   # 留下帮学生会长
    assert state.flags["transfer_done"] and state.affections["jiang_yu"] == 3.0
    assert state.current_node is None                      # N1 完成；N2 在下一回合接管

    view = game.act("visit_rooftop")                       # 次回合：N2 触发（行动照常结算）
    assert view.choice_prompt.id == "club_choice"
    view = game.pick(0)                                   # 进学生会
    assert state.flags["club_joined"] and state.flags["club_council"]
    assert state.affections["jiang_yu"] >= 8.0

    view = _play_days(game, state, "visit_rooftop", 5)     # 第 1~5 天：行动点全给天台
    assert state.day == 6 and view.choice_prompt.id == "exam_week_choice"
    assert state.flags["midterm_done"] is False
    view = game.pick(3)                                   # 帮他整理备考资料
    assert state.flags["midterm_done"] and state.flags["chose_date"]

    view = _play_days(game, state, "visit_rooftop", 4)     # → 第 10 天：学园祭准备会
    assert state.day == 10 and view.choice_prompt.id == "festival_choice"
    view = game.pick(3)                                   # 跟学生会跑流程
    assert state.flags["festival_prep"] and state.flags["festival_stage"]
    assert "ev_midterm_week" in state.triggered_events     # time 触发路径

    view = _play_days(game, state, "visit_rooftop", 4)     # → 第 14 天：放灯之夜
    assert state.day == 14 and view.choice_prompt.id == "confession_choice"
    assert "ev_festival_eve" in state.triggered_events
    assert state.counters["dates"] >= 20
    assert state.flags["jiang_secret"] is True             # 好感过 40 后的秘密事件

    view = game.pick(0)                                   # 上天台
    assert state.flags["route_jiang"] and state.flags["confession_done"]
    assert view.ending is not None and view.ending.id == "ending_jiang"
    assert state.affections["jiang_yu"] >= 45
    assert audit_stats(pack, state) == []
    assert len(state.choice_log) == 5


def test_offline_playthrough_reaches_true_ending_with_letter():
    """同一路线 + 亲笔信（自定义工具）→ 真结局；秘密 flag 与信缺一不可。"""
    pack, state, game = _game([resp(msg(tool_calls=[SUBMIT])) for _ in range(90)])
    game.start()
    game.pick(0)                                          # N1
    game.act("visit_rooftop")                             # 次回合触发 N2
    game.pick(0)                                          # N2
    view = _play_days(game, state, "visit_rooftop", 5)
    game.pick(3)                                          # N3
    view = _play_days(game, state, "visit_rooftop", 4)
    game.pick(3)                                          # N4
    # 最后一次跨天前把信写了（同行次数已过门槛）
    assert state.counters["dates"] >= 3
    assert game.registry.dispatch("write_letter", {}).status == "ok"
    view = _play_days(game, state, "visit_rooftop", 4)
    view = game.pick(0)                                   # N5：上天台
    assert view.ending is not None and view.ending.id == "ending_jiang_true"
    assert state.flags["wrote_letter"] and state.flags["jiang_secret"]
    assert audit_stats(pack, state) == []


# ---------------------------------------------------------------------------
# 结局判定顺序：真结局 → 普通 → 告白未果 → 友情 → 自我 → 兜底
# ---------------------------------------------------------------------------


def _ending_for(mutate) -> str:
    _, _, game = _game([resp(msg(tool_calls=[SUBMIT]))], mutate=mutate)
    view = game.say("今晚风很轻")
    assert view.ending is not None
    return view.ending.id


def test_true_ending_needs_secret_and_letter():
    """真结局是"路线 + 好感 + 秘密 + 亲笔信"四项同时满足——缺任一项落到普通结局。"""

    def base(s):
        _finish_mainline(s)
        s.flags.update({"route_jiang": True, "jiang_secret": True})
        s.affections["jiang_yu"] = 70.0
        s.day = 16

    assert _ending_for(base) == "ending_jiang"                 # 缺亲笔信
    assert _ending_for(lambda s: (base(s), s.flags.update({"wrote_letter": True}))) == "ending_jiang_true"
    # 好感不够（65 门槛）：有信有秘密也只在普通档
    assert _ending_for(
        lambda s: (base(s), s.flags.update({"wrote_letter": True}), s.affections.__setitem__("jiang_yu", 55.0))
    ) == "ending_jiang"


def test_unfinished_ending_when_affection_short():
    pack, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT]))],
        mutate=lambda s: (
            _finish_mainline(s),
            s.flags.update({"route_jiang": True}),
            s.affections.__setitem__("jiang_yu", 30.0),
            s.__setattr__("day", 17),
        ),
    )
    view = game.say("话说到这里就够了")
    assert view.ending is not None and view.ending.id == "ending_unfinished"


def test_friends_and_self_endings_are_route_self_only():
    _, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT]))],
        mutate=lambda s: (
            _finish_mainline(s),
            s.flags.update({"route_self": True}),
            s.affections.__setitem__("su_qing", 60.0),
            s.__setattr__("day", 16),
        ),
    )
    view = game.say("灯就放在这儿吧")
    assert view.ending is not None and view.ending.id == "ending_friends"

    _, _, game2 = _game(
        [resp(msg(tool_calls=[SUBMIT]))],
        mutate=lambda s: (
            _finish_mainline(s),
            s.flags.update({"route_self": True}),
            s.affections.__setitem__("su_qing", 10.0),
            s.__setattr__("day", 16),
        ),
    )
    view2 = game2.say("灯就放在这儿吧")
    assert view2.ending is not None and view2.ending.id == "ending_self"


def test_fallback_ending_by_day():
    _, state, game = _game(
        [resp(msg(tool_calls=[SUBMIT]))],
        mutate=lambda s: (_finish_mainline(s), s.__setattr__("day", 20)),
    )
    view = game.say("暑假快到了")
    assert view.ending is not None and view.ending.id == "ending_unspoken"


# ---------------------------------------------------------------------------
# E1 Judge 语料：规模 + 材料纪律（判定依据必须在材料内可见）
# ---------------------------------------------------------------------------


def test_corpus_scale_and_ids():
    corpus = load_corpus(NEW_PACK)
    cats = Counter(c.category for c in corpus)
    assert len(corpus) == 30
    assert cats["ooc"] == cats["setting"] == cats["confab"] == 6
    assert cats["normal"] == 12
    ids = [c.id for c in corpus]
    assert len(ids) == len(set(ids))


def test_corpus_violations_are_visible_in_materials():
    """OOC 判定依据 = 在场角色卡（世界观 forbidden 表不进材料）。"""
    pack = load_worldpack(NEW_PACK)
    corpus = {c.id: c for c in load_corpus(NEW_PACK)}
    role_break = build_materials(pack, corpus["ooc_role_break"])
    assert "苏晴" in role_break and "不提及自己是角色" in role_break
    enthusiasm = build_materials(pack, corpus["ooc_wen_enthusiasm"])
    assert "不会说热情洋溢的话" in enthusiasm and "安静、疏离" in enthusiasm
    # 设定矛盾的判定依据：状态栏数值 / 好感语气 / 身份
    stat_conflict = build_materials(pack, corpus["setting_stat_conflict"])
    assert "学力 20" in stat_conflict and "还在补基础" in stat_conflict
    aff_conflict = build_materials(pack, corpus["setting_affection_conflict"])
    assert "江屿 5/100（公事公办" in aff_conflict
    identity = build_materials(pack, corpus["setting_identity_contradiction"])
    assert "转学到青槐高中的高二女生" in identity


def test_normal_cases_carry_premises_and_respect_presence():
    """材料纪律：正常用例的前提必须进材料；在场为空时不得出现互动角色。"""
    pack = load_worldpack(NEW_PACK)
    corpus = {c.id: c for c in load_corpus(NEW_PACK)}
    trust = build_materials(pack, corpus["normal_jiang_trust"])
    assert "望远镜" in trust and "愿意让你看见他的犹豫" in trust
    quiet = build_materials(pack, corpus["normal_xia_quiet"])
    assert "右膝有旧伤" in quiet and "把最要强的那一面也交给你" in quiet
    solo = build_materials(pack, corpus["normal_bake_at_home"])
    assert "在场：无" in solo
    eve = build_materials(pack, corpus["normal_festival_eve_alone"])
    assert "在场：无" in eve

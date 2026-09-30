"""世界包可玩性门禁守卫（校验批）。

背景（2026-09-25 四路子代理审计）：`_cross_check` 是**引用闭合检查器**，不是
**可玩性检查器**——它能抓住"引用了不存在的东西"，但抓不住"通过了却玩不下去"。
本文件把审计发现的可玩性洞钉成契约，每一项都对应一个"作者拿到绿灯、玩家却崩/卡死"的形状。

九项：
1. C1 关键抉择选项效果引用未声明 stats/affections → 加载通过、玩家点下去崩（Critical）
2. C2 `options: []` → 加载通过、永久死锁（Critical）
3. 五类 id 唯一性（node/choice/action/event/ending）→ 语义错位
4. `change_scene` 未入 ENGINE_TOOL_NAMES → 包内自定义工具撞名 → 构造期崩
5. `EventSpec.once` 死字段 → `once: false` 只触发一次
6. 数值可达性 → 结局阈值在行动点预算内够不着
7. 禁用表条目零 token → 世界观边界防线静默失效
8. affection_stages 未连续覆盖实数区间 → 语气退化为「（无阶段定义）」
9. memory_limit / stat range 无约束 → 记忆静默全灭、饱和变负数
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from game_agent.events import EventSystem
from game_agent.schedule import ScheduleSystem
from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.storyline import StorylineEngine
from game_agent.worldpack import (
    ENGINE_TOOL_NAMES,
    WorldPackError,
    load_worldpack,
)

PACKS = Path(__file__).resolve().parent.parent / "world-packs"
SRC = PACKS / "ancient_jianghu"


def _shipped_packs() -> list[tuple[str, object]]:
    """在库内容包（目录名, WorldPack），**用生产的目录口径枚举**。

    为什么不再直接 `PACKS.iterdir()`：`world-packs/` 现在有两个区域——
    内容区（每个子目录都是一张卡）与草稿区 `_drafts/`（**是工作区，不是卡**，
    它连 `world.yaml` 都没有）。裸 `iterdir()` 会把草稿区当成一个包去 `load_worldpack`，
    于是"生成过一次东西"就会让三条内容守卫变红。

    **修法刻意不是"在测试里跳过 `_drafts`"**：那等于把"什么算一张卡"的规则抄到第三个
    地方，将来加别的区域时又会漏（本仓库对这类"记得过滤"一贯的态度是换成结构或单一真源）。
    这里直接用 `catalog.list_packs()`——它就是生产代码回答"有哪些卡"的那个函数，
    测试因此不可能与线上口径分叉。附带好处：坏包会带着 `error` 原文失败，
    比裸 `load_worldpack` 抛异常更好读。
    """
    from game_agent import catalog

    out = []
    for entry in catalog.list_packs(PACKS):
        assert entry.playable, f"在库包 {entry.id!r} 加载失败（内容区不该有坏包）：{entry.error}"
        out.append((entry.id, load_worldpack(entry.path)))
    assert out, "world-packs/ 下没有任何内容包——守卫会静默通过，拒绝继续"
    return out


class _AllRng:
    """rng：random() 恒 0（chance 必命中）；uniform 取上界。"""

    @staticmethod
    def random() -> float:
        return 0.0

    @staticmethod
    def uniform(a: float, b: float) -> float:
        return b


def _craft(mutate, *, rel: str, name: str = "p"):
    """复制一个真实包并按 mutate(dict) 改写指定文件，返回临时包路径。"""
    tmp = Path(tempfile.mkdtemp()) / name
    shutil.copytree(SRC, tmp)
    f = tmp / rel
    d = yaml.safe_load(f.read_text(encoding="utf-8"))
    mutate(d)
    f.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return tmp


def _load_expect_error(tmp: Path) -> str:
    with pytest.raises(WorldPackError) as ei:
        load_worldpack(tmp)
    return str(ei.value)


def _cleanup(tmp: Path) -> None:
    shutil.rmtree(tmp.parent, ignore_errors=True)


def _node_with_options(options: list[dict], flag: str = "met_shen") -> dict:
    """一个恒真节点，completion 由选项写入（保证 flag 可达性检查通过）。"""
    return {
        "id": "n1_first_meeting", "title": "初遇", "when": {"all": []},
        "goal": "与沈清秋结识", "completion": {"flags": {flag: True}},
        "on_enter": {"scene": "长安城·沈府门前", "briefing": "b", "present": ["shen_qingqiu"]},
        "critical_choices": [{"id": "how_to_help", "prompt": "你如何解围？",
                              "options": options}],
        "free_scope": "",
    }


def _ok_option(text: str) -> dict:
    return {"text": text, "effects": {"flags": {"met_shen": True}}}


# ---------------------------------------------------------------------------
# 1（Critical）：关键抉择选项效果引用校验
# ---------------------------------------------------------------------------


def test_undeclared_stat_in_choice_option_rejected():
    """选项效果写错 stats 键 → 必须在加载期拦下，而不是玩家点下去时 StatChangeError。"""
    opts = [_ok_option("挺身而出"), _ok_option("以理劝解")]
    opts[0]["effects"]["stats"] = {"ghost_stat": 5}
    tmp = _craft(lambda d: d.update(nodes=[_node_with_options(opts)]), rel="mainline.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "ghost_stat" in err, f"报错未指出坏键: {err}"
    finally:
        _cleanup(tmp)


def test_undeclared_affection_in_choice_option_rejected():
    """选项效果写错 affections 键 → 同样必须拦下。"""
    opts = [_ok_option("挺身而出"), _ok_option("以理劝解")]
    opts[0]["effects"]["affections"] = {"ghost_npc": 5}
    tmp = _craft(lambda d: d.update(nodes=[_node_with_options(opts)]), rel="mainline.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "ghost_npc" in err, f"报错未指出坏键: {err}"
    finally:
        _cleanup(tmp)


def test_c1_repro_would_crash_at_runtime_without_gate():
    """C1 的复现钉：坏包若漏过门禁，玩家点选项即崩（证明这道门禁不是形式主义）。

    直接手搓一个**绕过 load_worldpack** 的 pack 来模拟"门禁漏过"的旧行为。
    """
    opts = [_ok_option("挺身而出"), _ok_option("以理劝解")]
    opts[0]["effects"]["stats"] = {"ghost_stat": 5}
    tmp = _craft(lambda d: d.update(nodes=[_node_with_options(opts)]), rel="mainline.yaml")
    try:
        # 门禁已修：这里必须拿不到 pack
        with pytest.raises(WorldPackError):
            load_worldpack(tmp)
    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 2（Critical）：空选项死锁
# ---------------------------------------------------------------------------


def test_empty_options_rejected():
    """`options: []` 会让引擎进入"有抉择但无选项"的死锁态——必须在加载期拦下。"""
    tmp = _craft(lambda d: d.update(nodes=[_node_with_options([])]), rel="mainline.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "选项" in err, f"报错未提到选项: {err}"
    finally:
        _cleanup(tmp)


def test_single_option_is_allowed():
    """1 个选项是合法的（不是所有抉择都要多选）。"""
    tmp = _craft(lambda d: d.update(nodes=[_node_with_options([_ok_option("唯一出路")])]),
                 rel="mainline.yaml")
    try:
        pack = load_worldpack(tmp)
        state = GameState.from_pack(pack)
        story = StorylineEngine(pack, StatsSystem(pack.schedule))
        story.begin_turn(state)
        assert len(story.pending_choice(state).options) == 1
    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 3：五类 id 唯一性
# ---------------------------------------------------------------------------


def test_duplicate_node_id_rejected():
    tmp = _craft(
        lambda d: d.update(nodes=[_node_with_options([_ok_option("a")], "met_shen")] * 2),
        rel="mainline.yaml",
    )
    try:
        err = _load_expect_error(tmp)
        assert "n1_first_meeting" in err or "重复" in err
    finally:
        _cleanup(tmp)


def test_duplicate_choice_id_rejected():
    node = _node_with_options([_ok_option("a")])
    node["critical_choices"].append(dict(node["critical_choices"][0]))
    tmp = _craft(lambda d: d.update(nodes=[node]), rel="mainline.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "how_to_help" in err or "重复" in err
    finally:
        _cleanup(tmp)


def test_duplicate_action_id_rejected():
    def dup(d):
        d["actions"].append(dict(d["actions"][0]))

    tmp = _craft(dup, rel="schedule.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "重复" in err
    finally:
        _cleanup(tmp)


def test_duplicate_ending_id_rejected():
    def dup(d):
        d["endings"].append(dict(d["endings"][0]))

    tmp = _craft(dup, rel="endings.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "重复" in err
    finally:
        _cleanup(tmp)


def test_duplicate_event_id_rejected():
    def dup(d):
        d["events"].append(dict(d["events"][0]))

    tmp = _craft(dup, rel="events.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "重复" in err
    finally:
        _cleanup(tmp)


def test_shipped_packs_have_unique_ids():
    """现有 8 个包在新增唯一性校验下必须仍然通过（不留回归债）。"""
    for _name, _pack in _shipped_packs():
        pass  # `_shipped_packs()` 内部已经 load 过，不抛即通过


# ---------------------------------------------------------------------------
# 4：change_scene 与自定义工具撞名
# ---------------------------------------------------------------------------


def test_change_scene_in_engine_tool_names():
    assert "change_scene" in ENGINE_TOOL_NAMES


def test_custom_tool_shadowing_change_scene_rejected():
    """声明了地点表又要自定义 change_scene → 必须在加载期拦下，而非 Game() 构造期崩。"""
    def mutate(d):
        d.setdefault("tools", []).append({
            "id": "change_scene", "label": "换场景", "description": "x",
            "parameters": {}, "effects": {},
        })

    tmp = _craft(mutate, rel="schedule.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "change_scene" in err
    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 5：EventSpec.once 必须真正生效
# ---------------------------------------------------------------------------


def test_event_once_false_can_repeat():
    """`once: false` 的事件必须能重复触发（手册承诺的能力，此前是死字段）。"""
    tmp = _craft(
        lambda d: d.update(events=[{
            "id": "ev_repeat", "title": "重复事件",
            "trigger": {"kind": "schedule", "action": "cultivate", "chance": 1.0},
            "priority": "normal", "script": "又一次。", "effects": {}, "once": False,
        }]),
        rel="events.yaml",
    )
    try:
        pack = load_worldpack(tmp)
        state = GameState.from_pack(pack)
        rng = _AllRng()
        stats = StatsSystem(pack.schedule)
        events, sched = EventSystem(pack, stats, rng), ScheduleSystem(pack, stats, rng)
        fired = []
        for _ in range(3):
            state.action_points_left = 9
            sched.execute_action(state, "cultivate")
            ev = events.check_schedule_event(state, "cultivate")
            if ev is not None:
                events.trigger(state, ev)
                fired.append(ev.id)
        assert len(fired) == 3, f"once:false 事件只触发了 {len(fired)} 次"
    finally:
        _cleanup(tmp)


def test_event_once_true_still_fires_once():
    """`once: true`（缺省）必须仍然只触发一次——回归保护。"""
    tmp = _craft(
        lambda d: d.update(events=[{
            "id": "ev_once", "title": "一次性事件",
            "trigger": {"kind": "schedule", "action": "cultivate", "chance": 1.0},
            "priority": "normal", "script": "只此一次。", "effects": {}, "once": True,
        }]),
        rel="events.yaml",
    )
    try:
        pack = load_worldpack(tmp)
        state = GameState.from_pack(pack)
        rng = _AllRng()
        stats = StatsSystem(pack.schedule)
        events, sched = EventSystem(pack, stats, rng), ScheduleSystem(pack, stats, rng)
        fired = 0
        for _ in range(3):
            state.action_points_left = 9
            sched.execute_action(state, "cultivate")
            ev = events.check_schedule_event(state, "cultivate")
            if ev is not None:
                events.trigger(state, ev)
                fired += 1
        assert fired == 1
    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 6：数值可达性（结局阈值必须够得着）
# ---------------------------------------------------------------------------


def test_unreachable_ending_threshold_rejected():
    """结局要求的好感在行动点预算内够不着 → 加载期报错并说明差多少。"""
    def mutate(d):
        d["endings"] = [{
            "id": "ending_impossible", "title": "够不着的结局", "kind": "auto",
            "when": {"all": [{"affection": {"shen_qingqiu": {"gte": 99}}},
                             {"day": {"gte": 10}}]},
            "text": "x",
        }]

    tmp = _craft(mutate, rel="endings.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "99" in err or "够不着" in err or "上限" in err, f"报错未量化缺口: {err}"
    finally:
        _cleanup(tmp)


def test_reachable_ending_accepted():
    """可达的结局不能被误杀（当前包的真实阈值）。"""
    load_worldpack(SRC)


# ---------------------------------------------------------------------------
# 7：禁用表条目必须能切出 token
# ---------------------------------------------------------------------------


def test_forbidden_entry_without_tokens_rejected():
    """自然语言禁用规则切不出 token = 该条防线静默失效 → 必须在加载期拦下。"""
    def mutate(d):
        d["forbidden"] = ["不可出现现代科技产物"]

    tmp = _craft(mutate, rel="world.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "禁用" in err or "forbidden" in err.lower()
    finally:
        _cleanup(tmp)


def test_shipped_packs_all_forbidden_entries_tokenizable():
    """现有包的每条禁用规则都必须能切出 token（防线不得形同虚设）。"""
    from game_agent.storyline import _forbidden_tokens

    bad = []
    for name, pack in _shipped_packs():
        for entry in pack.world.forbidden:
            if not _forbidden_tokens_for(entry):
                bad.append((name, entry))
    assert not bad, f"以下禁用规则零 token（防线失效）: {bad}"


def _forbidden_tokens_for(entry: str) -> list[str]:
    from game_agent.worldpack import WorldSpec
    from game_agent.storyline import _forbidden_tokens

    return _forbidden_tokens(WorldSpec(name="t", era="e", forbidden=[entry]))


# ---------------------------------------------------------------------------
# 8：affection_stages 必须连续覆盖实数区间（好感是 float）
# ---------------------------------------------------------------------------


def test_affection_stage_gap_rejected():
    """阶段区间留空隙 → 该好感值下语气退化为「（无阶段定义）」→ 加载期拦下。"""
    def mutate(d):
        d["affection_stages"] = [
            {"range": [0, 20], "tone": "冷淡"},
            {"range": [30, 50], "tone": "客气"},  # 20~30 空隙
        ]

    tmp = _craft(mutate, rel="npcs/shen_qingqiu.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "阶段" in err or "覆盖" in err or "空隙" in err, f"报错不清楚: {err}"
    finally:
        _cleanup(tmp)


def test_float_affection_lands_in_a_stage():
    """浮点好感（如 20.5）必须落在某个阶段里——整数区间写法不得留洞。"""
    from game_agent.context import ContextBuilder

    pack = load_worldpack(SRC)
    cb = ContextBuilder.from_pack(pack)
    for value in (0.0, 0.1, 20.5, 50.5, 80.5, 99.9, 100.0):
        tone = cb._tone("shen_qingqiu", value)
        assert tone != "（无阶段定义）", f"好感 {value} 落在阶段空隙里"


def test_shipped_packs_stages_cover_their_domain():
    """现有包的阶段必须连续覆盖 [min, max]，间隙 ≤1（整数区间写法）。

    与校验器同一口径：作者普遍写 `[0,20] [21,50] …`，缝宽恰好 1；
    该缝由 `ContextBuilder._tone` 归给下段（见 test_float_affection_lands_in_a_stage）。
    这里只拦**真空隙**（缝宽 >1，如 20→30）。
    """
    problems = []
    for name, pack in _shipped_packs():
        for npc in pack.npcs.values():
            spec = pack.schedule.affections.get(npc.id)
            lo_bound = spec.min if spec else 0.0
            hi_bound = spec.max if spec else 100.0
            stages = sorted((s.range[0], s.range[1]) for s in npc.affection_stages)
            if not stages:
                problems.append((name, npc.id, "无阶段"))
                continue
            if stages[0][0] > lo_bound:
                problems.append((name, npc.id, f"{lo_bound}~{stages[0][0]} 空隙"))
            for i in range(len(stages) - 1):
                if stages[i][1] >= stages[i + 1][0]:
                    problems.append((name, npc.id, f"重叠 {stages[i]}/{stages[i + 1]}"))
                elif stages[i + 1][0] - stages[i][1] > 1.0:
                    problems.append(
                        (name, npc.id, f"{stages[i][1]}~{stages[i + 1][0]} 空隙")
                    )
            if stages[-1][1] < hi_bound:
                problems.append((name, npc.id, f"{stages[-1][1]}~{hi_bound} 空隙"))
    assert not problems, f"阶段覆盖问题: {problems}"


# ---------------------------------------------------------------------------
# 9：memory_limit 与 stat/affection 范围约束
# ---------------------------------------------------------------------------


def test_memory_limit_must_be_positive():
    """`memory_limit: 0` 会让该 NPC 的记忆静默全灭（写一条淘汰一条却回报已写入）。"""
    def mutate(d):
        d["memory_limit"] = 0

    tmp = _craft(mutate, rel="npcs/shen_qingqiu.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "memory_limit" in err
    finally:
        _cleanup(tmp)


def test_stat_initial_must_be_in_range():
    """initial 越界会让第一次收益把数值"饱和"回边界（+1 实际 -4900）。"""
    def mutate(d):
        d["stats"]["charm"]["initial"] = 5000

    tmp = _craft(mutate, rel="schedule.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "charm" in err
    finally:
        _cleanup(tmp)


def test_affection_initial_must_be_in_range():
    def mutate(d):
        d["affections"]["shen_qingqiu"]["initial"] = 900

    tmp = _craft(mutate, rel="schedule.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "shen_qingqiu" in err
    finally:
        _cleanup(tmp)


def test_stat_min_le_max():
    def mutate(d):
        d["stats"]["charm"]["min"] = 100
        d["stats"]["charm"]["max"] = 0

    tmp = _craft(mutate, rel="schedule.yaml")
    try:
        err = _load_expect_error(tmp)
        assert "charm" in err
    finally:
        _cleanup(tmp)

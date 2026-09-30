"""回合事务边界守卫测试：**叙事被接受 ⟺ 效果生效**。

背景（实测缺陷，本文件即回归守卫）：工具的写入是"即发即落"的，而叙事要等整轮
收尾才算数——两者事务边界不一致，于是

1. 关键节点判劣重写时，两稿的 change_stat / do_action 都落盘（**重复结算**）；
2. 协议熔断时，失败迭代的效果留在真值里（玩家看到"本轮跳过"，数值却涨了）。

两者对项目的核心审计不变量 `after == before + delta` **结构性不可见**——每一笔
stat_log 记录都自洽（5→10、10→15），审计校验的是算术而不是"叙事事件 ↔ 状态变更"
的对应关系。所以 827 项测试 + 300 轮长局 + 真机冒烟全绿也没抓到它：
原有守卫只断言了"第一稿的叙事被剥掉"，从未让第一稿去改过数值。

契约：
- 生成期间 agent 经工具落盘的一切都在回合事务内，叙事被接受才提交；
- 熔断 → 整批撤销（状态原地还原，回合计数不回滚）；
- 判劣重写 → 第一稿的叙事**与效果**一起作废，第二稿重新结算；
- judge 通过 → 正常提交（防过度回滚的反向守卫）。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

CRITICAL_NODE = "n1_first_meeting"  # 带 critical_choices → 触发内轮自校正


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _submit(tid: str, narration: str):
    return tool_call(
        tid, "submit_narration",
        {"narration": narration, "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )


def _change(tid: str, delta: float = 5, stat: str = "martial"):
    return tool_call(
        tid, "change_stat",
        {"target": "player", "stat": stat, "delta": delta, "reason": "练剑"},
    )


def _remember(tid: str, fact: str):
    return tool_call(
        tid, "remember", {"target": "player", "fact": fact, "importance": 5}
    )


def _do_action(tid: str, action: str = "visit_shen"):
    """visit_shen 在 ancient_jianghu 无日程事件触发 → 结算完全确定，无 rng 干扰。"""
    return tool_call(tid, "do_action", {"action": action, "reason": "我去拜访沈清秋"})


def _verdict_bad():
    return resp(msg(content="问题类型：设定矛盾：测试判劣"))


def _verdict_pass():
    return resp(msg(content="通过"))


def _no_claims():
    """事实图抽取器返回「无断言」。"""
    return resp(msg(content="无"))


def _game(responses, critique: bool = True, action_points: int | None = None):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.current_node = CRITICAL_NODE
    if action_points is not None:
        state.action_points_left = action_points
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm, critique_on_critical=critique)
    return pack, state, game


# ---------------------------------------------------------------------------
# 1. 判劣重写：第一稿的效果必须一起作废
# ---------------------------------------------------------------------------


def test_regeneration_does_not_double_apply_stat():
    """两稿各提议 +5 武功，玩家只看到一稿 → 只能结算一次。"""
    pack, state, game = _game([
        resp(msg(tool_calls=[_change("c1", 5), _submit("s1", "第一稿：练剑有成")])),
        _verdict_bad(),
        _no_claims(),
        resp(msg(tool_calls=[_change("c2", 5), _submit("s2", "第二稿：重写练剑")])),
    ])
    view = game._llm_round()

    assert view.narration == "第二稿：重写练剑"
    assert state.stats["martial"] == 10.0, "修复前是 15.0（两稿都落盘）"
    assert len(state.stat_log) == 1, "修复前是 2 条（每稿各一条）"


def test_regeneration_discards_first_draft_memory():
    """第一稿记下的错误事实（剑名听风）不得留存。"""
    pack, state, game = _game([
        resp(msg(tool_calls=[
            _remember("m1", "玩家的剑名是听风"), _submit("s1", "第一稿：剑名听风"),
        ])),
        _verdict_bad(),
        _no_claims(),
        resp(msg(tool_calls=[_submit("s2", "第二稿：剑名听雨")])),
    ])
    game._llm_round()

    assert state.player_facts == [], "第一稿写入的记忆应随其作废"


def test_regeneration_settles_action_once():
    """两稿各发起一次日程行动 → 行动点只能扣一次（每行动 1 点）。"""
    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_do_action("a1"), _submit("s1", "第一稿：登门拜访")])),
            _verdict_bad(),
            _no_claims(),
            resp(msg(tool_calls=[_do_action("a2"), _submit("s2", "第二稿：重写拜访")])),
        ],
        action_points=3,
    )
    game._llm_round()

    assert state.action_points_left == 2, "修复前是 1（两稿各结算一次）"


def test_regeneration_rolls_back_scene_and_action_points_together():
    """回滚要覆盖 do_action 的全部副作用：行动点 + 场景 + 在场人物。"""
    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_do_action("a1"), _submit("s1", "第一稿：登门拜访")])),
            _verdict_bad(),
            _no_claims(),
            resp(msg(tool_calls=[_submit("s2", "第二稿：改为在街上闲逛")])),
        ],
        action_points=3,
    )
    game._llm_round()

    # 第二稿没有再发起行动 → 第一稿的结算整体作废，场景回到出发前
    assert state.action_points_left == 3
    assert state.present_npcs == []
    assert state.scene != "长安城·沈府"


# ---------------------------------------------------------------------------
# 2. 熔断：失败迭代的效果必须整批撤销
# ---------------------------------------------------------------------------


def test_meltdown_discards_tool_effects():
    """三轮都只改数值、从不收尾 → 熔断。玩家被告知"本轮跳过"，真值必须不动。"""
    pack, state, game = _game([
        resp(msg(tool_calls=[_change("c1", 5)])),
        resp(msg(tool_calls=[_change("c2", 5)])),
        resp(msg(tool_calls=[_change("c3", 5)])),
    ])
    view = game._llm_round()

    assert view.narration is not None, "仍要给玩家兜底文案"
    assert state.stats["martial"] == 5.0, "修复前是 20.0（三轮失败迭代全落盘）"
    assert state.stat_log == [], "修复前是 3 条"


def test_meltdown_rolls_back_action_points():
    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_do_action("a1")])),
            resp(msg(tool_calls=[_do_action("a2")])),
            resp(msg(tool_calls=[_do_action("a3")])),
        ],
        action_points=3,
    )
    game._llm_round()

    assert state.action_points_left == 3, "熔断回合不得扣除行动点"


def test_meltdown_during_critical_choice_must_still_show_the_choice():
    """**关键抉择期熔断：视图必须把选项带回去**，否则玩家卡死。

    这条是 2026-10 真机跑生成卡时撞出来的：`pick()` 的叙事熔断 → 事务回滚把
    `pending_choice` **还原**（对：熔断不该吃掉一个分叉），但熔断兜底造的视图
    **不带 `choice_prompt`**。于是三件事同时成立，玩家无路可走：

    - 引擎锁着输入：`say` / `act` / `end_day` 全抛 `GameError`；
    - 视图说没有固定选项（`choice_prompt is None`），只给一个「自己说些什么…」；
    - 唯一的出路 `pick(i)` 在界面上**没有按钮可点**。

    判据是"视图与引擎状态不许自相矛盾"：引擎说还得选，视图就得给出选项。
    """
    pack, state, game = _game([resp(msg(content="（不调收尾工具）"))] * 3)
    game.state.current_node = "n1_first_meeting"
    game.state.pending_choice = "how_to_help"

    view = game.pick(0)

    # 回滚把抉择还原了 → 引擎仍然锁着
    assert state.pending_choice == "how_to_help", "熔断不该吃掉这个分叉（回滚语义）"
    assert game.story.choice_locked(state) is True
    # **而视图必须承认这件事**
    assert view.choice_prompt is not None, (
        "熔断兜底丢了 choice_prompt → 玩家看不到选项、又说什么都被拒 = 卡死"
    )
    assert view.choice_prompt.id == "how_to_help"
    assert "重新选择" in view.narration, "兜底文案要告诉玩家该做什么"

    # 反向守卫：非抉择期的熔断**不该**凭空造出一个 choice_prompt
    _, state2, game2 = _game([resp(msg(content="（不调收尾工具）"))] * 3)
    state2.pending_choice = None
    view2 = game2._llm_round()
    assert view2.choice_prompt is None, "没有待决抉择时不许伪造选项视图"
    assert "重新选择" not in view2.narration


def test_turn_ending_with_a_new_choice_must_say_so():
    """**回合进行中新挂上的关键抉择，视图必须当场说**（比熔断那条更一般）。

    真机在生成卡上撞到的第二个形态：`end_turn` 完成当前节点后会去找下一个满足
    `when` 的节点；生成器写出的节点 `when: {all: []}` **恒真**，于是同一个节点
    完成后立刻又满足进入条件、又挂上一个 `pending_choice`。而返回值只搬了
    `narration/choices/ending` → 视图说"没有选项"、引擎说"只能选固定选项"
    → `say`/`act`/`end_day` 全拒，`pick(i)` 又没有按钮可点 = **卡死**。

    判据是"视图与引擎状态不许自相矛盾"：引擎锁着，视图就得给出选项与选项文本。
    """
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.current_node = CRITICAL_NODE
    state.pending_choice = "how_to_help"
    # 找一个"恒真 when"的节点来复现重入：ancient_jianghu 的 n1 首节点即为空 when
    node = next(n for n in pack.mainline.nodes if n.id == CRITICAL_NODE)
    if not node.critical_choices:
        pytest.skip("该节点没有关键抉择，复现不了这个形态")

    llm = LLMClient(
        FakeClient([resp(msg(tool_calls=[_submit("s1", "叙事一。")]))] * 4),
        "fake", build_tools(pack.schedule),
    )
    game = Game(pack, state, llm, critique_on_critical=False)
    view = game.pick(0)

    assert view.narration, "选完之后要有叙事"
    if game.story.pending_choice(state) is not None:
        assert view.choice_prompt is not None, (
            "引擎又挂上了待决抉择，而视图没说 → 玩家卡死"
        )
        assert view.choices == [o.text for o in view.choice_prompt.options], (
            "选项文本要与待决抉择一致（不能给上一轮的过期选项）"
        )


def test_meltdown_does_not_rollback_turn_count():
    """反向守卫：回合计数取在快照之前，不应被回滚（否则长局计数倒退）。"""
    pack, state, game = _game([
        resp(msg(tool_calls=[_change("c1", 5)])),
        resp(msg(tool_calls=[_change("c2", 5)])),
        resp(msg(tool_calls=[_change("c3", 5)])),
    ])
    game._llm_round()

    assert state.turn_count == 1


def test_meltdown_keeps_fallback_history_clean():
    """熔断后历史只多一条引擎熔断说明，不残留工具调用痕迹。"""
    pack, state, game = _game([
        resp(msg(tool_calls=[_change("c1", 5)])),
        resp(msg(tool_calls=[_change("c2", 5)])),
        resp(msg(tool_calls=[_change("c3", 5)])),
    ])
    game._llm_round()

    assert len(game.history) == 1
    assert "熔断" in game.history[0]["content"]


def test_meltdown_rolls_back_without_critique_path():
    """非关键节点（不走自校正）同样受事务保护。"""
    pack, state, game = _game(
        [
            resp(msg(tool_calls=[_change("c1", 5)])),
            resp(msg(tool_calls=[_change("c2", 5)])),
            resp(msg(tool_calls=[_change("c3", 5)])),
        ],
        critique=False,
    )
    state.current_node = None
    game._llm_round()

    assert state.stats["martial"] == 5.0


# ---------------------------------------------------------------------------
# 3. 正常路径：不许过度回滚
# ---------------------------------------------------------------------------


def test_successful_turn_commits_tool_effects():
    """judge 通过 → 效果正常提交。"""
    pack, state, game = _game([
        resp(msg(tool_calls=[_change("c1", 5), _submit("s1", "练剑有成")])),
        _verdict_pass(),
        _no_claims(),
    ])
    view = game._llm_round()

    assert view.narration == "练剑有成"
    assert state.stats["martial"] == 10.0, "通过的一稿必须提交，不得被回滚"
    assert len(state.stat_log) == 1


def test_successful_turn_without_critique_commits():
    """非关键节点：无 judge 介入，效果照常提交。"""
    pack, state, game = _game(
        [resp(msg(tool_calls=[_change("c1", 5), _submit("s1", "日常修炼")]))],
        critique=False,
    )
    state.current_node = None
    game._llm_round()

    assert state.stats["martial"] == 10.0


def test_second_draft_still_bad_keeps_second_draft_effects():
    """第二稿仍判劣也接受（既有纪律）：此时留的是**第二稿**的效果。"""
    pack, state, game = _game([
        resp(msg(tool_calls=[_change("c1", 5), _submit("s1", "第一稿：错")])),
        _verdict_bad(),
        _no_claims(),
        resp(msg(tool_calls=[_change("c2", 3), _submit("s2", "第二稿：还是错")])),
    ])
    view = game._llm_round()

    assert view.narration == "第二稿：还是错"
    assert state.stats["martial"] == 8.0, "应为第一稿作废后的第二稿效果（5+3）"


# ---------------------------------------------------------------------------
# 4. 无可交付叙事 = 本轮无内容
# ---------------------------------------------------------------------------


def test_narration_cleaned_to_empty_rolls_back():
    """narration 全是工具格式文本被 clean_narration 清空 → 无内容交付，效果不作数。

    submit_narration 的校验只看"非空"，`<invoke>…</invoke>` 能过校验，但清洗后为空——
    这是真实的边角：协议要求模型不得把工具格式写进 narration，兜底清洗会把它抹掉。
    """
    pack, state, game = _game([
        resp(msg(tool_calls=[
            _change("c1", 5),
            _submit("s1", "<invoke>change_stat</invoke>"),
        ])),
    ])
    view = game._llm_round()

    assert not view.narration
    assert state.stats["martial"] == 5.0, "没有交付任何叙事 → 效果不得生效"
    assert state.stat_log == []

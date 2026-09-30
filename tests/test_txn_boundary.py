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

"""回合事务边界守卫：结算必须落在快照覆盖之内。

**缺陷背景**：`_narrate` → `_llm_round` 的快照取在**生成之前**，但 `act` / `pick` /
`end_day` 在调用 `_narrate` **之前**就已经把结算落盘了（日程行动、关键选项效果、
日期推进、时间事件）。于是这些落盘**不在任何快照覆盖内**——一旦本轮熔断：

```
narration: None
行动点: 1 → 0        ← 玩家白付一次行动点
martial: 5.0 → 11.7  ← 数值涨了，但这一轮没有任何叙事
```

关键抉择更严重：选项效果 + `choice_log` + `pending_choice=None` 都已落盘，
熔断后**那个抉择永久无法重做**。

契约：**叙事被接受 ⟺ 效果生效**——这条不变量必须覆盖结算发生在生成之前的路径，
而不只是 agent 在生成期间经工具落盘的效果。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PROBE = Path(__file__).resolve().parent.parent / "world-packs" / "baseline_probe"
OTOME = Path(__file__).resolve().parent.parent / "world-packs" / "campus_otome"


def _always_meltdown_game(pack_path: Path, **kwargs):
    """每轮都返回"没有工具调用"的坏输出 → 协议熔断（LLMTurnError）。"""
    pack = load_worldpack(pack_path)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([resp(msg(content="这不是工具调用"))] * 12), "fake",
                    build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm, **kwargs)


# ---------------------------------------------------------------------------
# T1 · act：行动点与效果不得在白付的一轮里落盘
# ---------------------------------------------------------------------------


def test_act_rolls_back_settlement_on_meltdown():
    """行动熔断 → 行动点、数值、stat_log 全部原样（玩家不该为失败的一轮付费）。"""
    pack, state, game = _always_meltdown_game(PROBE)
    ap_before = state.action_points_left
    martial_before = state.stats["martial"]

    view = game.act("cultivate")

    assert "本轮生成失败" in (view.narration or ""), "应走熔断兜底视图"
    assert state.action_points_left == ap_before, (
        f"行动点被扣：{ap_before} → {state.action_points_left}"
    )
    assert state.stats["martial"] == martial_before, (
        f"数值被改：{martial_before} → {state.stats['martial']}"
    )
    assert state.stat_log == [], f"审计日志残留: {state.stat_log}"


def test_act_commits_settlement_on_success():
    """回归保护：正常回合必须照常结算（别把回滚做成"永远不生效"）。"""
    pack = load_worldpack(PROBE)
    state = GameState.from_pack(pack)
    submit = tool_call("s1", "submit_narration",
                       {"narration": "你在后山练了一日。", "choices": ["一", "二", "三"],
                        "plot_signal": "normal"})
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[submit]))]), "fake",
                    build_tools(pack.schedule))
    game = Game(pack, state, llm)
    ap_before, martial_before = state.action_points_left, state.stats["martial"]

    game.act("cultivate")

    assert state.action_points_left == ap_before - 1
    assert state.stats["martial"] > martial_before
    assert len(state.stat_log) >= 1


# ---------------------------------------------------------------------------
# T1 · end_day：日期不得在熔断的一轮里推进
# ---------------------------------------------------------------------------


def test_end_day_rolls_back_day_advance_on_meltdown():
    """跨天熔断 → 日期不得推进（否则玩家"点了一下就白过一天"）。"""
    pack, state, game = _always_meltdown_game(PROBE)
    day_before = state.day

    view = game.end_day()

    assert "本轮生成失败" in (view.narration or "")
    assert state.day == day_before, f"日期被推进：{day_before} → {state.day}"


def test_end_day_advances_on_success():
    """回归保护：正常跨天照常推进。"""
    pack = load_worldpack(PROBE)
    state = GameState.from_pack(pack)
    submit = tool_call("s1", "submit_narration",
                       {"narration": "夜色落下，你在客栈歇了。", "choices": ["一", "二", "三"],
                        "plot_signal": "normal"})
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[submit]))]), "fake",
                    build_tools(pack.schedule))
    game = Game(pack, state, llm)
    day_before = state.day

    game.end_day()

    assert state.day == day_before + 1


# ---------------------------------------------------------------------------
# T1 · pick：关键抉择不得被熔断"吃掉"
# ---------------------------------------------------------------------------


def test_pick_rolls_back_choice_on_meltdown(tmp_path=None):
    """关键抉择选项熔断 → 选项效果、choice_log、pending_choice 全部原样。

    这是最严重的一种：抉择被消费掉却永远无法重做（玩家永久失去一个分叉）。
    """
    pack = load_worldpack(OTOME)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([resp(msg(content="坏输出"))] * 12), "fake",
                    build_tools(pack.schedule))
    # critique_on_critical=True → 走缓冲+自校正路径（真实 CLI/Web 默认）
    game = Game(pack, state, llm, critique_on_critical=True)
    game.start()
    assert state.pending_choice is not None, "夹具前提：开局应有待决抉择"
    choice_before = state.pending_choice
    aff_before = dict(state.affections)
    flags_before = dict(state.flags)
    log_before = len(state.choice_log)

    view = game.pick(0)

    assert "本轮生成失败" in (view.narration or "")
    assert state.pending_choice == choice_before, "抉择被消费掉了——玩家永久失去这个分叉"
    assert state.affections == aff_before, f"好感被改：{aff_before} → {state.affections}"
    assert state.flags == flags_before
    assert len(state.choice_log) == log_before


# ---------------------------------------------------------------------------
# T2 · 空叙事：日常路径也必须回滚（此前只有关键节点路径有这道守卫）
# ---------------------------------------------------------------------------


def test_empty_narration_rolls_back_on_daily_path():
    """日常回合：叙事被洗成空 → 效果不得提交（与关键节点路径同口径）。"""
    pack = load_worldpack(PROBE)
    state = GameState.from_pack(pack)
    change = tool_call("c1", "change_stat",
                       {"target": "player", "stat": "martial", "delta": 5, "reason": "练剑"})
    # narration 只含工具格式文本 → clean_narration 会洗成空
    submit = tool_call("s1", "submit_narration",
                       {"narration": "<invoke>change_stat</invoke>",
                        "choices": ["一", "二", "三"], "plot_signal": "normal"})
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[change, submit]))]), "fake",
                    build_tools(pack.schedule))
    game = Game(pack, state, llm)
    martial_before = state.stats["martial"]

    view = game.say("我练剑")

    assert not view.narration, "空叙事应被识别"
    assert state.stats["martial"] == martial_before, (
        f"空叙事却提交了效果：{martial_before} → {state.stats['martial']}"
    )
    assert state.stat_log == []


# ---------------------------------------------------------------------------
# 随机流也属于事务边界
# ---------------------------------------------------------------------------


def test_rollback_restores_rng_stream():
    """回滚必须连随机流一起还原——否则重试的一轮会掷出不同的骰子。

    检定掷骰 / `chance` 事件 / `{base, spread}` 收益曲线都吃 rng，且可能在回合中途
    被消耗。只还原真值不还原 rng，"撤销"就不彻底：同一回合两次运行结果不同。
    """
    import random

    pack = load_worldpack(PROBE)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([resp(msg(content="坏输出"))] * 12), "fake",
                    build_tools(pack.schedule))
    game = Game(pack, state, llm, rng=random.Random(7))

    snapshot = game._txn_snapshot()
    consumed = [game.rng.random() for _ in range(5)]  # 模拟回合中途掷骰
    game._txn_rollback(snapshot, reason="test")

    replayed = [game.rng.random() for _ in range(5)]
    assert replayed == consumed, (
        f"随机流未回滚：重试会掷出不同结果 {replayed} != {consumed}"
    )


def test_duck_typed_rng_without_getstate_is_tolerated():
    """鸭子类型兜底：只实现 random()/uniform() 的 rng 不得让快照报错。"""
    class _Minimal:
        @staticmethod
        def random():
            return 0.5

        @staticmethod
        def uniform(a, b):
            return (a + b) / 2

    pack = load_worldpack(PROBE)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([resp(msg(content="坏输出"))] * 12), "fake",
                    build_tools(pack.schedule))
    game = Game(pack, state, llm, rng=_Minimal())

    view = game.act("cultivate")  # 熔断 → 走回滚路径

    assert "本轮生成失败" in (view.narration or "")
    assert state.action_points_left == pack.schedule.day_action_points

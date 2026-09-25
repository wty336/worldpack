"""对话发起的日程行动（do_action 工具）守卫：与日程按钮殊途同归。

- 结算核一致：行动点扣除、检定/效果、饱和与审计入 stat_log；
- 意图 checklist：reason 必填；未知行动 / 行动点不足 / 关键抉择期 → 结构化拒绝；
- 日程触发事件：脚本进历史（后续回合可见）且附在结果文本里（本轮即时织入）；
- 协议集成：模型 do_action → submit_narration 一轮闭环，效果恰好结算一次；
- 引擎规则第 10 条钉在静态前缀里。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.context import ENGINE_RULES
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _n1_done(s: GameState) -> None:
    s.completed_nodes.append("n1_first_meeting")
    s.flags["met_shen"] = True


def _game(responses, mutate=None):
    pack = _pack()
    state = GameState.from_pack(pack)
    if mutate is not None:
        mutate(state)
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm)


def _submit(narration: str, call_id: str = "s1"):
    return tool_call(
        call_id, "submit_narration",
        {"narration": narration, "choices": ["一", "二", "三"], "plot_signal": "normal"},
    )


# ---------------------------------------------------------------------------
# 处理器（registry.dispatch 直测）
# ---------------------------------------------------------------------------


def test_do_action_settles_and_audits():
    pack, state, game = _game([], mutate=_n1_done)
    before = state.stats["martial"]
    result = game.registry.dispatch(
        "do_action", {"action": "cultivate", "reason": "玩家说要去后山练剑"}
    )
    assert result.status == "ok"
    assert state.action_points_left == 0  # 该包每天 1 点
    assert state.stats["martial"] > before  # 收益曲线结算
    assert "行动检定" in result.message or "行动效果" in result.message  # 结果供叙事织入
    # 审计入 stat_log（与按钮路径同核）
    from game_agent.audit import audit_stats

    assert audit_stats(pack, state) == []


def test_do_action_rejections():
    pack, state, game = _game([], mutate=_n1_done)
    # reason 必填（意图 checklist）
    assert game.registry.dispatch("do_action", {"action": "cultivate"}).status == "rejected"
    # 行动点不足
    state.action_points_left = 0
    r = game.registry.dispatch("do_action", {"action": "cultivate", "reason": "想练剑"})
    assert r.status == "rejected" and "行动点不足" in r.message
    # 未知行动
    state.action_points_left = 1
    r = game.registry.dispatch("do_action", {"action": "nope", "reason": "x"})
    assert r.status == "rejected"


def test_do_action_locked_during_critical_choice():
    pack, state, game = _game([])
    game.start()  # N1 关键抉择待决
    r = game.registry.dispatch("do_action", {"action": "cultivate", "reason": "x"})
    assert r.status == "rejected" and "关键抉择" in r.message
    assert state.action_points_left == 1


def test_do_action_schedule_event_fires_once():
    """日程触发事件：结算后触发，脚本进历史且附在结果文本里（只触发一次）。

    ancient_jianghu 自带 cultivate 的日程事件（后山奇遇），rng 固定 0.1 使其命中。
    """
    from game_agent.events import EventSystem

    pack, state, game = _game([], mutate=_n1_done)
    game.events = EventSystem(pack, game.stats, type("R", (), {
        "random": staticmethod(lambda: 0.1), "uniform": staticmethod(lambda a, b: a),
    })())
    before = len(state.triggered_events)
    result = game.registry.dispatch(
        "do_action", {"action": "cultivate", "reason": "玩家说去修炼"}
    )
    assert "【日程事件】" in result.message  # 本轮即时织入
    assert len(state.triggered_events) == before + 1  # 只触发一次
    assert any("后山奇遇" in (m.get("content") or "") for m in game.history)  # 后续回合可见


# ---------------------------------------------------------------------------
# 协议集成：模型 do_action → submit_narration 一轮闭环
# ---------------------------------------------------------------------------


def test_do_action_full_round_via_registry():
    call_action = tool_call("t1", "do_action", {"action": "cultivate", "reason": "玩家说去修炼"})
    pack, state, game = _game(
        [resp(msg(tool_calls=[call_action, _submit("你在后山练了一日剑。")]))],
        mutate=_n1_done,
    )
    view = game.say("我去后山修炼一番")
    assert view.narration == "你在后山练了一日剑。"
    assert state.action_points_left == 0
    assert state.stats["martial"] > 5
    # 结算结果进了工具消息（模型本轮可见）
    tool_msgs = [m.get("content") or "" for m in game.history if m.get("role") == "tool"]
    assert any("玩家在对话中发起日程行动" in c for c in tool_msgs)


def test_do_action_schema_sent_to_api():
    pack, state, game = _game([resp(msg(tool_calls=[_submit("x")]))], mutate=_n1_done)
    game.say("任意")
    sent = game.llm._client.chat.completions.calls[0]["tools"]
    names = [t["function"]["name"] for t in sent]
    assert "do_action" in names
    schema = next(t for t in sent if t["function"]["name"] == "do_action")
    assert schema["function"]["parameters"]["properties"]["action"]["enum"] == [
        "cultivate", "gift_visit", "visit_shen", "work",
    ]


def test_engine_rule_10_in_system_prefix():
    assert "行动结算纪律" in ENGINE_RULES
    assert "do_action" in ENGINE_RULES
    pack = _pack()
    state = GameState.from_pack(pack)
    from game_agent.context import ContextBuilder

    sys_text = ContextBuilder._system_text(pack)
    assert "行动结算纪律" in sys_text

"""K 系列守卫测试：统一回合轴（`game_turn`）与"恢复过程可见"（`TurnView.recovered`）。

## 两个问题

1. **恢复过程对玩家不可见**：一个玩家回合内部可能发生判劣重写、溢出压缩重试、熔断兜底。
   此前上层只看到"这轮慢"或"本轮生成失败"，分不出"发生过恢复并成功"与"压根没试过"。
2. **三套互不相干的回合计数器**：`llm._turn_seq`（一次生成）、`state.turn_count`
   （一次叙事回合）、`record_run` 的本地动作计数器。于是"玩家第 N 个操作花了多少钱"
   没法直接算——trace 按生成级分组、usage 没有回合字段。

## 契约

- `TurnView.recovered` 如实列出**实际发生**的恢复（判过的不算；正常回合为空）；
- `TurnView.sub_turns` = 本回合实际执行了几次生成（含条件事件级联）；
- `state.turn_count` 是唯一时间轴：trace 每个事件带 `game_turn`，usage 每条带 `game_turn`；
- 未接入游戏层的用法（单测直接构造 LLMClient）**不得**被写入 `game_turn=None` 字段。
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.trace import TraceRecorder
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "baseline_probe"


def _submit(narration="她望着你。"):
    return tool_call(
        "c2", "submit_narration",
        {"narration": narration, "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )


def _change(tid="c1", delta=3):
    return tool_call(
        tid, "change_stat",
        {"target": "player", "stat": "charm", "delta": delta, "reason": "打扮"},
    )


class _OverflowError(Exception):
    pass


OVERFLOW_MESSAGE = (
    "Error code: 400 - This model's maximum context length is 65536 tokens. "
    "Please reduce the length of the messages."
)


_LLM_KWARGS = {"tracer", "tracker"}  # 只有 LLMClient 认识这两个


def _game(responses, **kwargs):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    client = FakeClient(responses)

    class _Raising(type(client.chat.completions)):
        def create(self, **kw):  # noqa: ANN001
            if self.responses and isinstance(self.responses[0], BaseException):
                raise self.responses.pop(0)
            return super().create(**kw)

    client.chat.completions.__class__ = _Raising
    llm = LLMClient(
        client, model="fake", tools=build_tools(pack.schedule),
        **{k: v for k, v in kwargs.items() if k in _LLM_KWARGS},
    )
    return Game(pack, state, llm, **{k: v for k, v in kwargs.items() if k not in _LLM_KWARGS})


def _events(path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# 1. TurnView：恢复痕迹
# ---------------------------------------------------------------------------


def test_normal_turn_has_no_recovery_marks():
    """正常回合：recovered 为空、sub_turns = 1——不得凭空报"恢复过"。"""
    game = _game([resp(msg(tool_calls=[_submit("正常一轮")]))])
    view = game.say("你好")

    assert view.narration == "正常一轮"
    assert view.recovered == []
    assert view.sub_turns == 1


def test_meltdown_marks_recovered_and_counts_subturn():
    """熔断兜底必须如实上报（玩家需要知道"效果没生效"）。"""
    bad = resp(msg(content="没有工具调用"))
    game = _game([bad, bad, bad])
    view = game.say("你好")

    assert view.recovered == ["meltdown"]
    assert view.sub_turns == 1  # 三次迭代属于同一次生成
    assert "已跳过" in (view.narration or "")


def test_critique_regeneration_marks_recovered():
    """内轮自校正：判劣 → 重写，recovered 含 "critique"、sub_turns ≥ 2。

    用 ancient_jianghu 的 n1_first_meeting（带 critical_choices）触发关键节点路径
    ——`critique_on_critical` 只在关键节点内生效。
    """
    crit_pack = (
        Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"
    )
    pack = load_worldpack(crit_pack)
    state = GameState.from_pack(pack)
    state.current_node = "n1_first_meeting"
    llm = LLMClient(
        FakeClient([
            resp(msg(tool_calls=[_submit("第一稿")])),
            resp(msg(content="问题类型：设定矛盾：测试判劣")),  # judge 判劣
            resp(msg(content="无")),  # 事实图抽取
            resp(msg(tool_calls=[_submit("第二稿")])),
        ]),
        model="fake", tools=build_tools(pack.schedule),
    )
    game = Game(pack, state, llm, critique_on_critical=True)
    view = game.say("推进")

    assert "critique" in view.recovered
    assert view.sub_turns >= 2
    assert view.narration == "第二稿"


def test_overflow_recovery_marks_recovered():
    """溢出压缩重试成功 → recovered 含 overflow_recovered。"""
    game = _game(
        [
            _OverflowError(OVERFLOW_MESSAGE),
            resp(msg(content="（增量摘要）压缩后的剧情。")),
            resp(msg(tool_calls=[_submit("溢出后重试成功")])),
        ],
        context_window=100_000,
        keep_turns=2,
    )
    for i in range(4):
        game.history.append({"role": "user", "origin": "player", "content": f"玩家第 {i} 轮"})
        game.history.append({"role": "assistant", "origin": "model", "content": f"叙事第 {i} 轮"})

    view = game._llm_round()

    assert view.recovered == ["overflow_recovered"]
    assert view.narration == "溢出后重试成功"


# ---------------------------------------------------------------------------
# 2. 统一回合轴：trace / usage 都带 game_turn
# ---------------------------------------------------------------------------


def test_trace_events_carry_game_turn(tmp_path):
    """trace 的每个事件都带 game_turn，且等于本回合的 state.turn_count。"""
    rec = TraceRecorder(tmp_path / "t.jsonl")
    game = _game([resp(msg(tool_calls=[_change(), _submit("一轮")]))], tracer=rec)
    game.say("你好")

    events = _events(tmp_path / "t.jsonl")
    assert events, "应当有事件落盘"
    missing = [e["event"] for e in events if "game_turn" not in e]
    assert not missing, f"缺 game_turn 的事件: {missing}"
    assert {e["game_turn"] for e in events} == {game.state.turn_count} == {1}


def test_overflow_retry_events_share_one_game_turn(tmp_path):
    """溢出恢复的多轮生成共用同一个 game_turn（成本才能按玩家回合加总）。

    但生成级序号 `turn_seq` 仍应递增——两个层级正交，报告可任选粒度。
    """
    rec = TraceRecorder(tmp_path / "t.jsonl")
    game = _game(
        [
            _OverflowError(OVERFLOW_MESSAGE),
            resp(msg(content="（增量摘要）……")),
            resp(msg(tool_calls=[_submit("重试成功")])),
        ],
        tracer=rec, context_window=100_000, keep_turns=2,
    )
    for i in range(4):
        game.history.append({"role": "user", "origin": "player", "content": f"玩家第 {i} 轮"})
        game.history.append({"role": "assistant", "origin": "model", "content": f"叙事第 {i} 轮"})
    game._llm_round()

    events = _events(tmp_path / "t.jsonl")
    begins = [e for e in events if e["event"] == "turn_begin"]
    assert len(begins) == 2, "溢出恢复应产生两次生成"
    assert len({e["game_turn"] for e in events}) == 1, "两次生成同属一个玩家回合"
    assert begins[0]["turn_seq"] != begins[1]["turn_seq"], "生成级序号应各自独立"
    # 第二次生成必须带上"本回合已发生溢出恢复"的痕迹
    assert "overflow_recovered" in (begins[1].get("recovered") or [])


def test_usage_entries_carry_game_turn(tmp_path):
    """usage 每条带 game_turn（成本按玩家回合归集的前提）。"""
    tracker = UsageTracker(tmp_path / "usage.jsonl")
    game = _game([resp(msg(tool_calls=[_submit("一轮")]))], tracker=tracker)
    game.say("你好")

    rows = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and all(r.get("game_turn") == 1 for r in rows)
    # 既有报告口径不受影响（多的字段不进 summarize 的键）
    assert all("purpose" in r for r in rows)


def test_standalone_client_omits_game_turn(tmp_path):
    """未接入游戏层（直接构造 LLMClient）→ 不写 game_turn，而不是写 None。"""
    rec = TraceRecorder(tmp_path / "t.jsonl")
    pack = load_worldpack(PACK_PATH)
    llm = LLMClient(
        FakeClient([resp(msg(tool_calls=[_submit("一轮")]))]),
        model="fake", tools=build_tools(pack.schedule), tracer=rec,
    )
    llm.run_turn([{"role": "user", "content": "hi"}], registry=None)

    for e in _events(tmp_path / "t.jsonl"):
        assert "game_turn" not in e, f"{e['event']} 不该带 game_turn"


# ---------------------------------------------------------------------------
# 3. 报告层：按 game_turn 聚合
# ---------------------------------------------------------------------------


def test_per_game_turn_aggregates_subturns_and_recovery(tmp_path):
    """聚合行应把级联/重试收拢成一个玩家回合，并带上恢复痕迹。"""
    from scripts.trace_report import per_game_turn, render_game_turns

    events = [
        {"event": "turn_begin", "turn_seq": 1, "game_turn": 1, "recovered": []},
        {"event": "call", "turn_seq": 1, "game_turn": 1, "latency_ms": 100,
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"event": "tool", "turn_seq": 1, "game_turn": 1, "name": "change_stat", "status": "ok"},
        {"event": "turn_end", "turn_seq": 1, "game_turn": 1, "outcome": "completed", "iterations": 1},
        {"event": "turn_begin", "turn_seq": 2, "game_turn": 1, "recovered": ["overflow_recovered"]},
        {"event": "call", "turn_seq": 2, "game_turn": 1, "latency_ms": 50,
         "usage": {"prompt_tokens": 8, "completion_tokens": 4}},
        {"event": "turn_end", "turn_seq": 2, "game_turn": 1, "outcome": "completed", "iterations": 2},
        {"event": "turn_begin", "turn_seq": 3, "game_turn": 2, "recovered": []},
        {"event": "turn_end", "turn_seq": 3, "game_turn": 2, "outcome": "completed", "iterations": 1},
    ]
    rows = per_game_turn(events)

    assert [r["game_turn"] for r in rows] == [1, 2]
    assert rows[0]["sub_turns"] == 2 and rows[0]["iterations"] == 3
    assert rows[0]["recovered"] == ["overflow_recovered"]
    assert rows[0]["latency_ms"] == 150
    assert rows[1]["sub_turns"] == 1 and rows[1]["recovered"] == []

    # 只列恢复路径：第 1 回合入选、第 2 回合被滤掉
    text = render_game_turns(rows, only_recovered=True)
    assert "#1" in text and "#2" not in text


def test_per_game_turn_joins_usage_cost():
    """传入 usage 行时，成本按 game_turn 归集到对应回合。"""
    from scripts.trace_report import per_game_turn

    events = [
        {"event": "turn_begin", "turn_seq": 1, "game_turn": 7, "recovered": []},
        {"event": "turn_end", "turn_seq": 1, "game_turn": 7, "outcome": "completed"},
    ]
    usage = [
        {"model": "deepseek-v4-flash", "purpose": "turn", "game_turn": 7,
         "prompt_tokens": 1000, "completion_tokens": 200},
        {"model": "deepseek-v4-flash", "purpose": "compress", "game_turn": 7,
         "prompt_tokens": 500, "completion_tokens": 100},
        {"model": "deepseek-v4-flash", "purpose": "judge", "game_turn": None},  # 无回合：跳过
    ]
    rows = per_game_turn(events, usage)

    assert len(rows) == 1
    assert rows[0]["cost"] > 0
    assert rows[0]["side_calls"] == {"turn": 1, "compress": 1}, "无 game_turn 的调用不归任何回合"


def test_runlog_entry_carries_both_turn_axes(tmp_path):
    """runlog 条目同时留 turn（检查点序号）与 game_turn（引擎轴）。"""
    from game_agent.runlog import RunRecorder

    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.turn_count = 5

    rec = RunRecorder.create(tmp_path, run_id="t-run")
    rec.checkpoint(3, state, [], {"kind": "say"}, {"narration": "x"})

    entry = json.loads((rec.runlog_path).read_text(encoding="utf-8").splitlines()[0])
    assert entry["turn"] == 3  # 检查点序号（文件名寻址）
    assert entry["game_turn"] == 5  # 引擎回合轴（与 trace/usage 对齐）

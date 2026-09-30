"""trace 与真值一致性守卫：被回滚的效果不得在 trace 里留下"已生效"的印象。

**缺陷背景**：工具事件是在**派发时**记录的（`llm.py` dispatch 处写
`tool ... status=ok`），而回滚发生在回合收尾（熔断 / 判劣重写 / 无可交付叙事）。
于是熔断轮会记下 `tool change_stat status=ok` 的数值变更明细，
而真实状态纹丝未动、`stat_log` 为 0——`scripts/trace_report.py` 会把这一轮
聚合成"改了 N 次"。观测层自相矛盾，会让"为什么这轮失败"的排查得出反向结论。

契约：**发生回滚时，trace 必须显式记一条 `txn_rollback`**，让读者知道
前面的 tool 事件不代表真值变更。

测试夹具用 `baseline_probe`（**0 个主线节点**）：否则回合会被恒真节点的
关键抉择接管而提前返回，根本不进 LLM 循环（本项目其它测试曾在此处假绿）。
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.trace import TraceRecorder
from game_agent.worldpack import load_worldpack

PACK = Path(__file__).resolve().parent.parent / "world-packs" / "baseline_probe"


def _change(delta=5):
    return tool_call("c1", "change_stat",
                     {"target": "player", "stat": "martial", "delta": delta, "reason": "练剑"})


def _submit(narration="你练了一日剑。"):
    return tool_call("s1", "submit_narration",
                     {"narration": narration, "choices": ["一", "二", "三"],
                      "plot_signal": "normal"})


def _events(trace_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _game(responses, trace_path: Path | None):
    pack = load_worldpack(PACK)
    state = GameState.from_pack(pack)
    llm = LLMClient(
        FakeClient(responses), "fake", build_tools(pack.schedule),
        tracer=TraceRecorder(trace_path) if trace_path is not None else None,
    )
    return pack, state, Game(pack, state, llm)


def test_meltdown_records_explicit_rollback_event(tmp_path):
    """熔断 → 效果作废 → trace 必须留下 txn_rollback。"""
    trace = tmp_path / "t.jsonl"
    pack, state, game = _game([resp(msg(content="这也能叫工具调用？"))] * 8, trace)

    game.say("我练剑")

    events = _events(trace)
    kinds = [e["event"] for e in events]
    assert "txn_rollback" in kinds, f"熔断回滚未记 trace；实际事件: {kinds}"
    rb = next(e for e in events if e["event"] == "txn_rollback")
    assert rb["reason"] == "meltdown"
    # 真值确实没变——证明这条 trace 是必要的（否则读者会以为效果生效了）
    assert state.stats["martial"] == 5.0
    assert state.stat_log == []


def test_meltdown_trace_previously_looked_successful(tmp_path):
    """对照：熔断轮的 call 事件仍在，只有 txn_rollback 能纠正"效果生效"的错觉。"""
    trace = tmp_path / "t.jsonl"
    pack, state, game = _game([resp(msg(content="坏输出"))] * 8, trace)

    game.say("我练剑")

    kinds = [e["event"] for e in _events(trace)]
    assert "call" in kinds, "熔断轮的 API 调用应被记录"
    assert "txn_rollback" in kinds, "必须有一条纠正性事件"


def test_no_rollback_event_on_successful_turn(tmp_path):
    """回归保护：正常回合不得产生 txn_rollback（否则这条信号会被噪声淹没）。"""
    trace = tmp_path / "t.jsonl"
    pack, state, game = _game([resp(msg(tool_calls=[_change(), _submit()]))], trace)

    game.say("我练剑")

    kinds = [e["event"] for e in _events(trace)]
    assert "txn_rollback" not in kinds, f"正常回合误报回滚: {kinds}"
    assert state.stats["martial"] == 10.0  # 效果确实生效了


def test_trace_is_optional_and_silent_without_recorder(tmp_path):
    """观测层纪律：没有 tracer 时必须零开销、零行为变化（不得因补 trace 而报错）。"""
    pack, state, game = _game([resp(msg(content="坏输出"))] * 8, None)

    view = game.say("我练剑")  # 熔断会走 _txn_rollback → 内部 _trace 是 no-op

    assert "本轮生成失败" in (view.narration or "")
    assert state.stats["martial"] == 5.0

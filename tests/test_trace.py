"""B1（Track B）守卫测试：trace 事件流（离线，FakeClient 替身）。

契约（`game_agent/trace.py`）：
- 事件三类 turn_begin/turn_end、call、tool，按 seq 全序、带 pid；
- 落盘失败静默（观测层不得影响游戏）；
- tracer 为 None 时零开销（既有 565 个测试即回归护栏）；
- 熔断必须留下 outcome=meltdown 的 turn_end（否则轨迹无法回放"为什么死"）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.config import Settings
from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.stats import StatChangeError
from game_agent.trace import TraceRecorder
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

CHANGE = tool_call(
    "c1", "change_stat",
    {"target": "player", "stat": "charm", "delta": 3, "reason": "打扮"},
)
SUBMIT = tool_call(
    "c2", "submit_narration",
    {"narration": "你登上诗台，满座皆惊。", "choices": ["继续", "离场", "与沈清秋说话"],
     "plot_signal": "normal"},
)


def _client(responses, tracer, apply_change=None):
    pack = load_worldpack(PACK_PATH)
    client = LLMClient(
        FakeClient(responses), model="fake",
        tools=build_tools(pack.schedule), tracer=tracer,
    )
    if apply_change is None:
        apply_change = lambda a: "[已执行]"  # noqa: E731
    return client, apply_change


def _events(path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# TraceRecorder 本体
# ---------------------------------------------------------------------------


def test_recorder_appends_seq_and_pid(tmp_path):
    rec = TraceRecorder(tmp_path / "trace.jsonl")
    rec.record("turn_begin", turn_seq=1)
    rec.record("call", purpose="judge")
    events = _events(tmp_path / "trace.jsonl")
    assert [e["event"] for e in events] == ["turn_begin", "call"]
    assert events[0]["seq"] < events[1]["seq"]  # 同进程全序
    assert all("pid" in e for e in events)
    assert events[1]["purpose"] == "judge"


def test_recorder_silent_when_path_unwritable(tmp_path):
    """观测层纪律：写不进就放弃，不得抛异常反过来打断游戏。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    rec = TraceRecorder(blocker / "trace.jsonl")  # 父路径是文件 → 建目录必失败
    rec.record("call")  # 不抛即通过


# ---------------------------------------------------------------------------
# run_turn 事件流
# ---------------------------------------------------------------------------


def test_turn_events_recorded_end_to_end(tmp_path):
    rec = TraceRecorder(tmp_path / "trace.jsonl")
    client, apply = _client([resp(msg(tool_calls=[CHANGE, SUBMIT]))], rec)
    result = client.run_turn([{"role": "user", "content": "hi"}], apply)
    assert result.narration

    events = _events(tmp_path / "trace.jsonl")
    by_event = [e["event"] for e in events]
    assert by_event == ["turn_begin", "call", "tool", "tool", "turn_end"]

    begin = events[0]
    end = events[-1]
    assert begin["turn_seq"] == end["turn_seq"] == 1
    assert end["outcome"] == "completed" and end["iterations"] == 1
    assert end["narration_chars"] == len("你登上诗台，满座皆惊。")
    assert end["choices"] == 3 and end["plot_signal"] == "normal"

    tools = [e for e in events if e["event"] == "tool"]
    assert [t["name"] for t in tools] == ["change_stat", "submit_narration"]
    assert [t["status"] for t in tools] == ["ok", "ok"]

    call = events[1]
    assert call["purpose"] == "turn" and call["model"] == "fake"
    assert call["latency_ms"] >= 0 and call["finish_reason"] is None


def test_tool_rejection_recorded_as_rejected(tmp_path):
    rec = TraceRecorder(tmp_path / "trace.jsonl")

    def rejecting(args):
        raise StatChangeError("数值越界")

    client, _ = _client(
        [resp(msg(tool_calls=[CHANGE])), resp(msg(tool_calls=[SUBMIT]))], rec
    )
    client.run_turn([{"role": "user", "content": "hi"}], rejecting)
    tools = [e for e in _events(tmp_path / "trace.jsonl") if e["event"] == "tool"]
    assert tools[0]["status"] == "rejected"
    assert "数值越界" in tools[0]["detail"]


def test_bad_json_tool_recorded(tmp_path):
    rec = TraceRecorder(tmp_path / "trace.jsonl")
    bad = tool_call("c9", "change_stat", "{not-json")
    client, apply = _client(
        [resp(msg(tool_calls=[bad])), resp(msg(tool_calls=[SUBMIT]))], rec
    )
    client.run_turn([{"role": "user", "content": "hi"}], apply)
    tools = [e for e in _events(tmp_path / "trace.jsonl") if e["event"] == "tool"]
    assert tools[0]["status"] == "bad_json"


def test_unknown_tool_recorded(tmp_path):
    rec = TraceRecorder(tmp_path / "trace.jsonl")
    ghost = tool_call("c8", "give_me_money", {"amount": 100})
    client, apply = _client(
        [resp(msg(tool_calls=[ghost])), resp(msg(tool_calls=[SUBMIT]))], rec
    )
    client.run_turn([{"role": "user", "content": "hi"}], apply)
    tools = [e for e in _events(tmp_path / "trace.jsonl") if e["event"] == "tool"]
    assert tools[0]["status"] == "unknown" and tools[0]["name"] == "give_me_money"


def test_meltdown_recorded(tmp_path):
    """熔断必须留下 outcome=meltdown——轨迹要能回放'为什么死'。"""
    rec = TraceRecorder(tmp_path / "trace.jsonl")
    bad = resp(msg(content="没有工具调用"))
    client, apply = _client([bad, bad, bad], rec)
    with pytest.raises(LLMTurnError, match="熔断"):
        client.run_turn([{"role": "user", "content": "hi"}], apply)
    events = _events(tmp_path / "trace.jsonl")
    assert events[-1]["event"] == "turn_end"
    assert events[-1]["outcome"] == "meltdown"
    assert events[-1]["iterations"] == 3


# ---------------------------------------------------------------------------
# complete_with_meta 事件流（侧信道）
# ---------------------------------------------------------------------------


class _GoodClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="通过"), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
        )


class _BoomClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        raise RuntimeError("api down")


def test_complete_records_call_with_usage(tmp_path):
    rec = TraceRecorder(tmp_path / "trace.jsonl")
    llm = LLMClient(_GoodClient(), "main", [], tracer=rec)
    out = llm.complete([{"role": "user", "content": "x"}], purpose="judge")
    assert out == "通过"
    call = _events(tmp_path / "trace.jsonl")[0]
    assert call["event"] == "call" and call["purpose"] == "judge"
    assert call["finish_reason"] == "stop"
    assert call["usage"] == {"prompt_tokens": 10, "completion_tokens": 2}
    assert call["latency_ms"] >= 0


def test_complete_error_recorded_and_reraised(tmp_path):
    """API 异常：记一条带 error 的 call 事件，然后原样抛出（行为不变）。"""
    rec = TraceRecorder(tmp_path / "trace.jsonl")
    llm = LLMClient(_BoomClient(), "main", [], tracer=rec)
    with pytest.raises(RuntimeError, match="api down"):
        llm.complete([{"role": "user", "content": "x"}])
    call = _events(tmp_path / "trace.jsonl")[0]
    assert call["event"] == "call" and call["error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# 开关与零开销
# ---------------------------------------------------------------------------


def test_from_settings_wires_tracer_from_trace_path(tmp_path):
    s = Settings(api_key="k", base_url="https://x", model="main",
                 trace_path=str(tmp_path / "t.jsonl"))
    llm = LLMClient.from_settings(s, [])
    assert llm.tracer is not None and llm.tracer.path == tmp_path / "t.jsonl"


def test_from_settings_no_tracer_by_default(tmp_path):
    s = Settings(api_key="k", base_url="https://x", model="main")
    assert LLMClient.from_settings(s, []).tracer is None


def test_no_tracer_means_no_overhead():
    """tracer=None 时 run_turn 行为与结果完全不变（既有 test_llm.py 即回归护栏）。"""
    client, apply = _client([resp(msg(tool_calls=[CHANGE, SUBMIT]))], tracer=None)
    result = client.run_turn([{"role": "user", "content": "hi"}], apply)
    assert result.narration == "你登上诗台，满座皆惊。"
    assert result.iterations == 1

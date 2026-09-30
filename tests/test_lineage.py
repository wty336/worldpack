"""血缘标记守卫测试（③）：history 与 trace 都必须能自描述"这条从哪来"。

背景（研究平台化的硬需求）：`game.py` 有三处会**重建** history（事务回滚、
判劣剥稿、压缩重建），engine 侧还有 15 处注入点。此前要判断一条消息是模型写的、
玩家打的还是引擎注入的，只能靠"有没有 `name` 字段"这类间接特征反推——
判官材料、评测导出、trace 回放都得各自实现一遍这套推断，且随注入点增加而漂移。

本文件钉住两条不变量：

1. **history 自描述**：模型消息带 `origin="model"`、玩家输入带 `origin="player"`、
   引擎注入带 `origin="engine"`；三者都不依赖间接特征；
2. **trace 可关联**：每条 `tool` 事件带 `call_id`（= `tool_call_id`），
   `origin` 与 history 同口径；截断作废记 `status="truncated"`。

同时钉住**不污染 API 载荷**：血缘字段是普通 JSON 键，不得改变请求里
每条消息的 role / tool_calls / tool_call_id 结构（DeepSeek 兼容端点的硬约束）。
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.config import Settings
from game_agent.llm import LLMClient, build_tools
from game_agent.stats import StatChangeError
from game_agent.trace import TraceRecorder
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "baseline_probe"

CHANGE = tool_call(
    "c1", "change_stat",
    {"target": "player", "stat": "charm", "delta": 3, "reason": "打扮"},
)
SUBMIT = tool_call(
    "c2", "submit_narration",
    {"narration": "你登上诗台，满座皆惊。", "choices": ["继续", "离场", "与沈清秋说话"],
     "plot_signal": "normal"},
)


def _truncated(tool_calls):
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="length", message=msg(tool_calls=tool_calls))]
    )


def _client(responses, tracer=None):
    pack = load_worldpack(PACK_PATH)
    return LLMClient(
        FakeClient(responses), model="fake",
        tools=build_tools(pack.schedule), tracer=tracer,
    )


def _events(path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# trace：call_id 与 origin
# ---------------------------------------------------------------------------


def test_tool_events_carry_call_id_and_model_origin(tmp_path):
    """每条 tool 事件必须带 call_id（= tool_call_id）与 origin="model"。"""
    rec = TraceRecorder(tmp_path / "t.jsonl")
    client = _client([resp(msg(tool_calls=[CHANGE, SUBMIT], response_id="resp_1"))], rec)
    client.run_turn([{"role": "user", "content": "hi"}], lambda a: "[已执行]")

    tools = [e for e in _events(tmp_path / "t.jsonl") if e["event"] == "tool"]
    assert [t["name"] for t in tools] == ["change_stat", "submit_narration"]
    assert [t["call_id"] for t in tools] == ["c1", "c2"]
    assert all(t["origin"] == "model" for t in tools)


def test_truncated_tool_calls_traced_as_truncated(tmp_path):
    """截断作废的工具调用同样进 trace，status=truncated（否则轨迹缺一环）。"""
    rec = TraceRecorder(tmp_path / "t.jsonl")
    client = _client([_truncated([CHANGE]), resp(msg(tool_calls=[SUBMIT]))], rec)
    client.run_turn([{"role": "user", "content": "hi"}], lambda a: "[已执行]")

    tools = [e for e in _events(tmp_path / "t.jsonl") if e["event"] == "tool"]
    assert tools[0]["status"] == "truncated" and tools[0]["call_id"] == "c1"
    assert tools[0]["origin"] == "model"


def test_rejected_tool_event_keeps_call_id(tmp_path):
    """引擎拒绝路径（rejected）同样带 call_id——关联不因失败而断。"""
    rec = TraceRecorder(tmp_path / "t.jsonl")

    def rejecting(args):
        raise StatChangeError("数值越界")

    client = _client([resp(msg(tool_calls=[CHANGE])), resp(msg(tool_calls=[SUBMIT]))], rec)
    client.run_turn([{"role": "user", "content": "hi"}], rejecting)
    rejected = [
        e for e in _events(tmp_path / "t.jsonl")
        if e["event"] == "tool" and e["status"] == "rejected"
    ]
    assert rejected and rejected[0]["call_id"] == "c1"


# ---------------------------------------------------------------------------
# history：origin 三态
# ---------------------------------------------------------------------------


def test_assistant_messages_marked_as_model(tmp_path):
    client = _client([resp(msg(tool_calls=[CHANGE, SUBMIT], response_id="resp_9"))])
    result = client.run_turn([{"role": "user", "content": "hi"}], lambda a: "[已执行]")
    assistants = [m for m in result.messages if m["role"] == "assistant"]
    assert assistants and all(m["origin"] == "model" for m in assistants)
    # 模型回复的调用 id 也落进 history（可回溯到哪一次 API 响应）
    assert assistants[-1]["call_id"] == "resp_9"


def test_engine_injected_messages_marked_as_engine():
    """引擎注入的元消息带 origin="engine"（与 name="engine" 同口径）。"""
    client = _client([resp(msg(content="没有工具调用")), resp(msg(tool_calls=[SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], lambda a: "[已执行]")
    injected = [m for m in result.messages if m.get("name") == "engine"]
    assert injected and all(m.get("origin") == "engine" for m in injected)


def test_payload_keys_stay_api_shaped():
    """血缘字段不得改变载荷形状：role/tool_calls/tool_call_id 结构原样。

    血缘是 **payload 里的普通 JSON 键**（随历史一起存活，压缩/回滚/存档都不失真），
    因此必须钉住它没有挤掉任何 API 必需字段——这是"加字段"相对"旁路索引"的代价。
    """
    client = _client([resp(msg(tool_calls=[CHANGE, SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], lambda a: "[已执行]")
    roles = [m["role"] for m in result.messages]
    assert set(roles) <= {"system", "user", "assistant", "tool"}
    assistant = next(m for m in result.messages if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["type"] == "function"
    assert assistant["tool_calls"][0]["function"]["name"] == "change_stat"
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert tool_msgs and all("tool_call_id" in m for m in tool_msgs)


def test_trace_and_history_agree_on_call_ids(tmp_path):
    """trace 的 call_id 集合 == history 的 tool_call_id 集合（可对拍回放）。"""
    rec = TraceRecorder(tmp_path / "t.jsonl")
    client = _client([resp(msg(tool_calls=[CHANGE, SUBMIT]))], rec)
    result = client.run_turn([{"role": "user", "content": "hi"}], lambda a: "[已执行]")

    traced = {
        e["call_id"] for e in _events(tmp_path / "t.jsonl") if e["event"] == "tool"
    }
    in_history = {
        tc["id"]
        for m in result.messages
        if m["role"] == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    }
    assert traced == in_history == {"c1", "c2"}


# ---------------------------------------------------------------------------
# Game 层：玩家输入与引擎注入
# ---------------------------------------------------------------------------


def _game(responses):
    """用 baseline_probe 包：首轮不锁关键抉择，玩家输入能真正走到 LLM。

    （ancient_jianghu / urban_neon 的首轮会先弹关键选择，那时 `_narrate` 直接返回
    固定选项、根本不发请求，玩家输入不落历史。）
    """
    from game_agent.game import Game
    from game_agent.state import GameState

    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient(responses), model="fake", tools=build_tools(pack.schedule))
    return Game(pack, state, llm)


def test_player_turn_marked_in_history():
    """玩家输入带 origin="player"；引擎注入的元消息带 origin="engine"；模型带 model。"""
    game = _game([resp(msg(tool_calls=[SUBMIT]))])
    view = game.say("我去看看诗会")
    assert view.narration, "夹具应当真正跑完一轮（否则下面的断言没有覆盖面）"

    player = [m for m in game.history if m.get("origin") == "player"]
    assert player and player[0]["content"] == "我去看看诗会"

    engine = [m for m in game.history if m.get("origin") == "engine"]
    assert engine, "状态栏/任务卡等引擎注入消息应带 origin=engine"
    assert all(m.get("name") == "engine" for m in engine)

    model = [m for m in game.history if m.get("origin") == "model"]
    assert model and all(m["role"] == "assistant" for m in model)


def test_every_history_message_is_attributed():
    """不变量：一回合跑完后，history 里**没有**来源不明的消息。

    这条是给研究用途兜底的——判官材料与评测导出直接吃 history，任何一条
    origin 缺失都意味着导出侧要重新发明一套推断规则。
    """
    game = _game([resp(msg(tool_calls=[SUBMIT]))])
    game.say("你好")

    unattributed = [
        m for m in game.history
        if m.get("role") in ("user", "assistant") and "origin" not in m
    ]
    assert not unattributed, f"缺血缘标记: {unattributed[:2]}"


def test_origin_marker_does_not_break_settings_wiring(tmp_path):
    """血缘标记与 tracer 开关正交：无 tracer 时行为完全不变（零开销口径）。"""
    s = Settings(api_key="k", base_url="https://x", model="main")
    assert LLMClient.from_settings(s, []).tracer is None

"""批次 C 守卫测试：MCP server（mcp_server.py，离线假客户端）。

- initialize 握手（协议版本回显 + serverInfo）；
- tools/list = 玩家侧六件套，**不含**叙述者内部工具（真值纪律在 MCP 边界的延伸）；
- tools/call：status / say（含 FakeClient 回合）/ act / 未知工具 -32602 /
  处理器业务错误 isError；
- 通知不回包；未知方法 -32601；坏 JSON -32700；
- serve() 端到端：StringIO 喂两行请求，逐行回包。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.mcp_server import build_player_registry, handle_request, serve
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _game(responses=None, mutate=None):
    pack = _pack()
    state = GameState.from_pack(pack)
    if mutate is not None:
        mutate(state)
    llm = LLMClient(FakeClient(responses or []), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm)


def _n1_done(s: GameState) -> None:
    s.completed_nodes.append("n1_first_meeting")
    s.flags["met_shen"] = True


def _rpc(registry, method, params=None, rid=1):
    return handle_request(registry, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def test_initialize_echoes_protocol_version():
    pack, state, game = _game()
    reg = build_player_registry(game)
    resp_ = _rpc(reg, "initialize", {"protocolVersion": "2025-06-18"})
    assert resp_["result"]["protocolVersion"] == "2025-06-18"
    assert resp_["result"]["serverInfo"]["name"] == "game-agent"
    assert "tools" in resp_["result"]["capabilities"]


def test_notification_and_ping():
    pack, state, game = _game()
    reg = build_player_registry(game)
    assert handle_request(reg, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _rpc(reg, "ping")["result"] == {}


def test_unknown_method_and_malformed():
    pack, state, game = _game()
    reg = build_player_registry(game)
    assert _rpc(reg, "resources/list")["error"]["code"] == -32601
    assert handle_request(
        reg, {"jsonrpc": "2.0", "method": "some/notification"}
    ) is None  # 无 id 的未知通知不回包


# ---------------------------------------------------------------------------
# 工具面
# ---------------------------------------------------------------------------


def test_tools_list_player_only():
    pack, state, game = _game()
    reg = build_player_registry(game)
    tools = _rpc(reg, "tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["status", "actions", "say", "pick", "act", "end_day"]
    # 叙述者内部工具不对外——玩家代理无权直改数值
    assert "change_stat" not in names and "remember" not in names
    assert all("inputSchema" in t for t in tools)


def test_call_status_and_actions():
    pack, state, game = _game(mutate=_n1_done)
    reg = build_player_registry(game)
    result = _rpc(reg, "tools/call", {"name": "status"})["result"]
    assert result.get("isError") is not True
    assert "属性" in result["content"][0]["text"]
    result = _rpc(reg, "tools/call", {"name": "actions"})["result"]
    assert "修炼" in result["content"][0]["text"]


def test_call_say_runs_turn():
    submit = tool_call(
        "s1", "submit_narration",
        {"narration": "MCP 叙事。", "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )
    pack, state, game = _game([resp(msg(tool_calls=[submit]))], mutate=_n1_done)
    reg = build_player_registry(game)
    result = _rpc(reg, "tools/call", {"name": "say", "arguments": {"text": "你好"}})["result"]
    text = result["content"][0]["text"]
    assert "MCP 叙事。" in text and "甲" in text


def test_call_act_runs_action():
    submit = tool_call(
        "s1", "submit_narration",
        {"narration": "练剑叙事。", "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )
    pack, state, game = _game([resp(msg(tool_calls=[submit]))], mutate=_n1_done)
    reg = build_player_registry(game)
    result = _rpc(reg, "tools/call", {"name": "act", "arguments": {"action_id": "cultivate"}})["result"]
    assert "练剑叙事。" in result["content"][0]["text"]


def test_call_unknown_tool_is_jsonrpc_error():
    pack, state, game = _game()
    reg = build_player_registry(game)
    resp_ = _rpc(reg, "tools/call", {"name": "change_stat", "arguments": {}})
    assert resp_["error"]["code"] == -32602  # 内部工具同样不可经 MCP 调用


def test_call_business_error_is_error_result():
    """业务错误（GameError 等）→ isError 结果而非连接级失败。"""
    pack, state, game = _game()  # 未跳过 N1：处于关键抉择锁定，say 应被拒
    game.start()
    reg = build_player_registry(game)
    result = _rpc(reg, "tools/call", {"name": "say", "arguments": {"text": "自由输入"}})["result"]
    assert result["isError"] is True
    assert "关键抉择" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# serve() 端到端（StringIO）
# ---------------------------------------------------------------------------


def test_serve_end_to_end():
    pack, state, game = _game(mutate=_n1_done)
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + "{bad json}\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    out = io.StringIO()
    serve(game, stdin=stdin, stdout=out)
    lines = [json.loads(l) for l in out.getvalue().strip().splitlines()]
    assert len(lines) == 3  # 通知不回包
    assert lines[0]["result"]["serverInfo"]["name"] == "game-agent"
    assert lines[1]["error"]["code"] == -32700
    assert [t["name"] for t in lines[2]["result"]["tools"]][0] == "status"

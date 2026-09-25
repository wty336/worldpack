"""MCP server（批次 C，docs/plan-design-hardening.md §3）：把游戏暴露为 MCP 工具。

协议：JSON-RPC 2.0 over stdio（newline-delimited），实现 MCP 生命周期最小子集：
initialize / notifications/initialized / ping / tools/list / tools/call。
零依赖手写（与 BM25 同一取舍：协议薄、可离线测试，不引入 SDK 与向量库）。

工具面 = **玩家侧**操作（与 CLI 同权）：status / actions / say / pick / act / end_day。
叙述者侧工具（change_stat / remember / …）是回合协议的内部契约，不对外暴露——
玩家代理无权直接改数值，这正是引擎真值纪律在 MCP 边界上的延伸。
玩家工具与叙述者工具复用同一套 ToolSpec / ToolRegistry 派发机制——
同一机制、两个消费面。

用法：python -m game_agent mcp [世界包路径]
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from .game import Game, GameError
from .llm import LLMTurnError
from .registry import ToolRegistry, ToolSpec
from .schedule import ScheduleError
from .storyline import StorylineError

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "game-agent", "version": "1.0"}

# MCP 工具错误统一映射的异常（与 Web 前端 dispatch 的口径一致）
TOOL_ERRORS = (GameError, StorylineError, ScheduleError, ValueError, LLMTurnError)


def _actions_text(game: Game) -> str:
    """批次审查修复：先算列表再判空（原表达式因优先级成为死代码）。"""
    actions = game.actions_available()
    if not actions:
        return f"剩余行动点 {game.state.action_points_left}（无可用行动，可用 end_day 进入下一天）"
    return (
        f"剩余行动点 {game.state.action_points_left}：\n"
        + "\n".join(f"- {a.id}: {a.label}（{a.cost} 行动点）" for a in actions)
    )


def _view_text(view) -> str:
    """TurnView → 给外部客户端的文本（叙述 + 选项 / 关键抉择 / 结局）。"""
    parts: list[str] = []
    if view.briefing:
        parts.append(f"【剧情】{view.briefing}")
    if view.choice_prompt is not None:
        parts.append(f"【关键抉择】{view.choice_prompt.prompt}")
        parts.append(
            "选项（用 pick 提交序号）：\n"
            + "\n".join(f"{i}. {o.text}" for i, o in enumerate(view.choices))
        )
    else:
        if view.narration:
            parts.append(view.narration)
        if view.ending is not None:
            parts.append(f"『{view.ending.title}』\n{view.ending.text}")
        elif view.choices:
            parts.append(
                "你可以（用 say 提交选项文本或自由输入）：\n"
                + "\n".join(f"- {c}" for c in view.choices)
            )
    return "\n\n".join(parts) or "（无输出）"


def build_player_registry(game: Game) -> ToolRegistry:
    """玩家侧注册表：与 CLI 相同的操作面，挂到同一套 ToolRegistry 机制上。"""
    reg = ToolRegistry()
    no_args: dict[str, Any] = {"type": "object", "properties": {}}

    def _spec(name: str, description: str, parameters: dict, handler: Callable[[dict], str]):
        reg.register(
            ToolSpec(name=name, description=description, parameters=parameters, handler=handler)
        )

    _spec(
        "status", "查看当前游戏状态（场景/天数/属性/好感/主线目标）", no_args,
        lambda args: game.status_text(),
    )
    _spec(
        "actions", "列出今日可选日程行动与剩余行动点", no_args,
        lambda args: _actions_text(game),
    )
    _spec(
        "say", "推进剧情：提交玩家的话（自由输入或日常选项的文本）",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "玩家说的话"}},
            "required": ["text"],
        },
        lambda args: _view_text(game.say(str(args.get("text", "")))),
    )
    _spec(
        "pick", "关键抉择：提交固定选项序号（0 起）",
        {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "选项序号（0 起）"}},
            "required": ["index"],
        },
        lambda args: _view_text(game.pick(int(args.get("index", 0)))),
    )
    _spec(
        "act", "执行日程行动（消耗行动点；行动 id 见 actions）",
        {
            "type": "object",
            "properties": {"action_id": {"type": "string", "description": "行动 id"}},
            "required": ["action_id"],
        },
        lambda args: _view_text(game.act(str(args.get("action_id", "")))),
    )
    _spec(
        "end_day", "结束今天，进入下一天（叙事化跨天：时序过渡场景 + 时间事件结算）", no_args,
        lambda args: _view_text(game.end_day()),
    )
    return reg


# ---------------------------------------------------------------------------
# JSON-RPC 处理（纯函数，可离线测试）
# ---------------------------------------------------------------------------


def _result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle_request(registry: ToolRegistry, request: dict) -> dict | None:
    """处理一条 JSON-RPC 请求。通知（无 id）返回 None（不回包）。"""
    method = str(request.get("method") or "")
    rid = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        if rid is None:  # 批次审查修复：通知形态的 initialize 不回包
            return None
        return _result(
            rid,
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return _result(rid, {}) if rid is not None else None
    if method == "tools/list":
        tools = []
        for name in registry.names():
            t = registry.get(name)
            if t is not None:
                tools.append(
                    {"name": t.name, "description": t.description, "inputSchema": t.parameters}
                )
        return _result(rid, {"tools": tools})
    if method == "tools/call":
        name = str(params.get("name") or "")
        spec = registry.get(name)
        if spec is None or spec.handler is None:
            return _error(rid, -32602, f"未知工具: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _result(
                rid,
                {"content": [{"type": "text", "text": "arguments 必须是对象"}], "isError": True},
            )
        try:
            text = spec.handler(arguments)
        except TOOL_ERRORS as e:
            return _result(
                rid, {"content": [{"type": "text", "text": str(e)}], "isError": True}
            )
        except Exception as e:  # noqa: BLE001 — 批次审查修复：畸形参数不得杀死 serve 循环
            return _result(
                rid,
                {
                    "content": [{"type": "text", "text": f"内部错误: {type(e).__name__}"}],
                    "isError": True,
                },
            )
        return _result(rid, {"content": [{"type": "text", "text": text}]})
    if rid is not None:
        return _error(rid, -32601, f"未知方法: {method}")
    return None


def serve(game: Game, stdin=None, stdout=None) -> None:
    """stdio 主循环：逐行读 JSON-RPC 请求，回包（通知不回）。"""
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    # 批次审查修复：Windows 控制台管道默认 ANSI 代码页，中文会乱码——显式钉 UTF-8
    for stream in (inp, out):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass  # 已重定向/不可重配的流保持原样
    registry = build_player_registry(game)
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            response = _error(None, -32700, f"JSON 解析失败: {e}")
        else:
            response = handle_request(registry, request)
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()

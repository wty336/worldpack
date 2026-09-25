"""批次 C 守卫测试：工具注册表（registry.py）。

- golden：build_tools 输出与迁移前逐字段一致（引擎四件套 schema 单点真源迁移）；
- dispatch：ok / rejected / protocol_error(未启用) / unknown 四态；
- schemas(include_internal=False) 剔除协议收尾工具（MCP 工具面用）；
- 重复注册 / 绑定未知工具 → ToolRegistryError；
- run_turn 的 registry 路径与旧回调路径行为一致（同一条派发代码）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.llm import LLMClient, build_tools
from game_agent.registry import (
    ToolRegistry,
    ToolRegistryError,
    ToolSpec,
    from_callbacks,
    from_schedule,
)
from game_agent.stats import StatChangeError
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


# ---------------------------------------------------------------------------
# golden：schema 与迁移前逐字段一致
# ---------------------------------------------------------------------------


def test_build_tools_golden_shape():
    """四件套顺序 + change_stat schema 逐字段（枚举来自世界包 schedule）。"""
    pack = _pack()
    tools = build_tools(pack.schedule)
    assert [t["function"]["name"] for t in tools] == [
        "change_stat", "submit_narration", "remember", "query_world", "do_action",
    ]
    change_stat = tools[0]["function"]
    assert change_stat["parameters"]["properties"]["target"]["enum"] == [
        "player", "shen_qingqiu",
    ]
    assert change_stat["parameters"]["properties"]["stat"]["enum"] == [
        "charm", "martial", "silver", "affection",
    ]
    assert change_stat["parameters"]["required"] == ["target", "stat", "delta", "reason"]
    submit = tools[1]["function"]
    assert submit["parameters"]["properties"]["plot_signal"]["enum"] == [
        "normal", "node_complete",
    ]
    assert all(t["type"] == "function" for t in tools)


def test_from_schedule_same_as_build_tools():
    pack = _pack()
    assert from_schedule(pack.schedule).schemas() == build_tools(pack.schedule)


def test_schemas_exclude_internal():
    reg = from_schedule(_pack().schedule)
    names = [t["function"]["name"] for t in reg.schemas(include_internal=False)]
    assert "submit_narration" not in names
    assert "change_stat" in names


# ---------------------------------------------------------------------------
# dispatch 四态
# ---------------------------------------------------------------------------


def _dispatch_registry():
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="echo",
            description="测试工具",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: f"回声:{args.get('x')}",
        )
    )
    reg.register(
        ToolSpec(
            name="strict",
            description="总是拒绝",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: (_ for _ in ()).throw(StatChangeError("越界")),
            rejects=(StatChangeError,),
        )
    )
    reg.register(
        ToolSpec(name="off", description="未启用", parameters={"type": "object", "properties": {}})
    )
    return reg


def test_dispatch_four_statuses():
    reg = _dispatch_registry()
    assert reg.dispatch("echo", {"x": 1}).message == "回声:1"
    assert reg.dispatch("echo", {"x": 1}).status == "ok"
    assert reg.dispatch("strict", {}).status == "rejected"
    assert "引擎拒绝" in reg.dispatch("strict", {}).message
    off = reg.dispatch("off", {})
    assert off.status == "protocol_error" and "未启用" in off.message
    unknown = reg.dispatch("nope", {})
    assert unknown.status == "unknown" and "未知工具" in unknown.message


def test_dispatch_never_raises_unlisted_exception():
    """handler 抛出未声明类型 → 异常穿透（注册表只捕获声明的拒绝类型）。"""

    def _boom(args):
        raise RuntimeError("bug")

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="boom", description="", parameters={},
            handler=_boom, rejects=(StatChangeError,),
        )
    )
    with pytest.raises(RuntimeError):
        reg.dispatch("boom", {})


def test_register_duplicate_and_bind_unknown():
    reg = ToolRegistry()
    spec = ToolSpec(name="x", description="", parameters={})
    reg.register(spec)
    with pytest.raises(ToolRegistryError, match="重复"):
        reg.register(spec)
    with pytest.raises(ToolRegistryError, match="未知"):
        reg.bind_handler("nope", lambda a: "")


def test_bind_handler_replaces_schema_only_spec():
    """Game 装配路径：schema-only 注册 → 绑定 handler → 派发生效。"""
    reg = from_schedule(_pack().schedule)
    assert reg.get("change_stat").handler is None
    reg.bind_handler("change_stat", lambda args: "已执行")
    assert reg.dispatch("change_stat", {}).status == "ok"


# ---------------------------------------------------------------------------
# from_callbacks 兼容 shim 与 run_turn registry 路径
# ---------------------------------------------------------------------------


def test_from_callbacks_dispatch_matches_legacy():
    reg = from_callbacks(lambda a: "changed", lambda a: "remembered", None)
    assert reg.dispatch("change_stat", {}).status == "ok"
    assert reg.dispatch("remember", {}).status == "ok"
    disabled = reg.dispatch("query_world", {})
    assert disabled.status == "protocol_error" and "世界查询" in disabled.message


def test_run_turn_registry_path_custom_tool_visible():
    """registry 路径：API 收到的 tools= 含自定义工具（schemas 来自注册表）。"""
    pack = _pack()
    from game_agent.worldpack import CustomToolSpec

    pack.schedule = pack.schedule.model_copy(
        update={"tools": [CustomToolSpec(id="wp_test", label="测试", description="测试用")]}
    )
    reg = from_schedule(pack.schedule)
    submit = tool_call(
        "s1", "submit_narration",
        {"narration": "叙事", "choices": ["一", "二", "三"], "plot_signal": "normal"},
    )
    llm = LLMClient(FakeClient([resp(msg(tool_calls=[submit]))]), "fake", [])
    result = llm.run_turn([{"role": "user", "content": "hi"}], registry=reg)
    assert result.narration == "叙事"
    sent_tools = llm._client.chat.completions.calls[0]["tools"]
    names = [t["function"]["name"] for t in sent_tools]
    assert "wp_test" in names  # registry 路径 schema 含自定义工具

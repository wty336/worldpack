"""W4 验收测试（离线假客户端）：输出协议闭环、失败重试、熔断。"""

from __future__ import annotations

import pytest

from fakes import FakeClient, msg, resp, tool_call

from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.stats import StatChangeError
from game_agent.worldpack import load_worldpack
from pathlib import Path

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"

_tool_call = tool_call
_msg = msg
_resp = resp


def _client(responses, apply_change=None):
    pack = load_worldpack(PACK_PATH)
    client = LLMClient(
        FakeClient(responses),
        model="fake",
        tools=build_tools(pack.schedule),
    )
    if apply_change is None:
        apply_change = lambda a: f"[已执行] {a}"
    return client, apply_change


CHANGE = _tool_call("c1", "change_stat", {"target": "player", "stat": "charm", "delta": 3, "reason": "打扮"})
SUBMIT = _tool_call(
    "c2",
    "submit_narration",
    {
        "narration": "你登上诗台，满座皆惊。",
        "choices": ["继续", "离场", "与沈清秋说话"],
        "plot_signal": "normal",
    },
)


def test_happy_path_two_iterations():
    """一次响应内 change_stat + submit_narration 同时出现：完整解析。"""
    client, apply_change = _client([_resp(_msg(tool_calls=[CHANGE, SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], apply_change)
    assert result.narration == "你登上诗台，满座皆惊。"
    assert result.choices == ["继续", "离场", "与沈清秋说话"]
    assert result.plot_signal == "normal"
    assert len(result.stat_changes) == 1
    assert result.iterations == 1
    assert "change_stat" in result.messages[1]["tool_calls"][0]["function"]["name"]


def test_change_stat_across_iterations():
    """先 change_stat，下一轮再 submit：工具结果正确回传、回调收到参数。"""
    seen = {}
    def apply(args):
        seen.update(args)
        return "ok"

    client = LLMClient(
        FakeClient([_resp(_msg(tool_calls=[CHANGE])), _resp(_msg(tool_calls=[SUBMIT]))]),
        model="fake",
        tools=build_tools(load_worldpack(PACK_PATH).schedule),
    )
    result = client.run_turn([{"role": "user", "content": "hi"}], apply)
    assert seen["target"] == "player" and seen["stat"] == "charm" and seen["delta"] == 3
    assert result.iterations == 2
    # 回传的 tool 结果消息存在且被模型看到（第二轮调用消息数 = 3 + 1 tool + 1 assistant...）
    assert any(m["role"] == "tool" for m in result.messages)


def test_retry_after_no_tool_call():
    """无工具调用 = 协议失败 → 结构化反馈重试一次后成功。"""
    client, apply = _client([_resp(_msg(content="直接输出了文本")), _resp(_msg(tool_calls=[SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], apply)
    assert result.iterations == 2
    assert result.narration


def test_meltdown_after_consecutive_failures():
    """连续 3 次协议失败 → 熔断抛 LLMTurnError。"""
    bad = _resp(_msg(content="没有工具调用"))
    client, apply = _client([bad, bad, bad])
    with pytest.raises(LLMTurnError, match="熔断"):
        client.run_turn([{"role": "user", "content": "hi"}], apply)


def test_bad_json_tool_args_survived():
    """工具参数不是合法 JSON：转为协议错误，不崩溃，下一轮仍可成功。"""
    bad_change = _tool_call("c9", "change_stat", "{not-json")
    client, apply = _client([_resp(_msg(tool_calls=[bad_change])), _resp(_msg(tool_calls=[SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], apply)
    assert result.narration
    assert result.stat_changes == []


def test_stat_rejection_fed_back():
    """引擎拒绝（StatChangeError）→ 结构化错误回传，模型仍能收尾。"""
    def rejecting(args):
        raise StatChangeError("数值越界：银两 当前 50")

    client, _ = _client([_resp(_msg(tool_calls=[CHANGE])), _resp(_msg(tool_calls=[SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], rejecting)
    # 拒绝原因以结构化 tool 错误回传（任意位置），模型据此收尾
    assert any(
        m["role"] == "tool" and "数值越界" in m["content"] for m in result.messages
    )
    assert result.stat_changes[0]["result"].startswith("[引擎拒绝]")


def test_invalid_choices_count_retried():
    """choices 不足 3 个 → 协议错误重试。"""
    bad_submit = _tool_call(
        "c7",
        "submit_narration",
        {"narration": "……", "choices": ["只有两个"], "plot_signal": "normal"},
    )
    client, apply = _client([_resp(_msg(tool_calls=[bad_submit])), _resp(_msg(tool_calls=[SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], apply)
    assert result.iterations == 2
    assert len(result.choices) == 3


def test_unknown_tool_survived():
    """幻觉工具调用 → 协议错误，不崩溃。"""
    ghost = _tool_call("c8", "give_me_money", {"amount": 100})
    client, apply = _client([_resp(_msg(tool_calls=[ghost])), _resp(_msg(tool_calls=[SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], apply)
    assert result.narration
    assert any("未知工具" in m["content"] for m in result.messages if m["role"] == "tool")


def test_tool_schema_enums_injected_from_pack():
    """工具 schema 的枚举值来自世界包（属性/NPC 精确注入，章 4 ACI）。"""
    pack = load_worldpack(PACK_PATH)
    tools = build_tools(pack.schedule)
    change = tools[0]["function"]["parameters"]["properties"]
    assert set(change["target"]["enum"]) == {"player", "shen_qingqiu"}
    assert set(change["stat"]["enum"]) == {"charm", "martial", "silver", "affection"}


def test_tool_message_pairing_invariant():
    """协议不变量：历史中每条带 tool_calls 的 assistant 消息，
    其后必须紧跟覆盖全部 tool_call_id 的 tool 结果（否则下一次请求被 API 拒绝）。"""
    client, apply = _client([_resp(_msg(tool_calls=[CHANGE, SUBMIT]))])
    result = client.run_turn([{"role": "user", "content": "hi"}], apply)
    msgs = result.messages
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            j = i + 1
            while j < len(msgs) and msgs[j]["role"] == "tool":
                ids.discard(msgs[j]["tool_call_id"])
                j += 1
            assert not ids, f"tool_call_id 缺配对结果: {ids}"
        i += 1

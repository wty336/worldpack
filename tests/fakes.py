"""测试共用：假 OpenAI 客户端（离线协议测试）。"""

from __future__ import annotations

import json
from types import SimpleNamespace


def tool_call(call_id: str, name: str, arguments: dict | str):
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def msg(content=None, tool_calls=None, reasoning_content=None):
    return SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=reasoning_content
    )


def resp(message, finish_reason=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("假客户端响应耗尽——模型调用次数超出预期")
        r = self.responses.pop(0)
        if kwargs.get("stream"):
            return _iter_stream(r)  # Web 分发路径走流式；聚合语义按真实 SDK 增量模拟
        return r


def _iter_stream(resp):
    """把一条假响应展开为流式 chunk 序列（content / reasoning / tool_calls 分片）。"""
    choice = resp.choices[0]
    msg = choice.message
    usage = getattr(resp, "usage", None)
    delta0 = SimpleNamespace(content=None, reasoning_content=None, tool_calls=None)
    if getattr(msg, "reasoning_content", None):
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None, reasoning_content=msg.reasoning_content, tool_calls=None),
                finish_reason=None)], usage=None)
    if msg.content:
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=msg.content, reasoning_content=None, tool_calls=None),
                finish_reason=None)], usage=None)
    for i, tc in enumerate(msg.tool_calls or []):
        # id+name 一片、arguments 一片：验证 llm.py 的流式聚合真的在拼装
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None, reasoning_content=None,
                tool_calls=[SimpleNamespace(index=i, id=tc.id,
                                            function=SimpleNamespace(name=tc.function.name,
                                                                     arguments=None))]),
                finish_reason=None)], usage=None)
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None, reasoning_content=None,
                tool_calls=[SimpleNamespace(index=i, id=None,
                                            function=SimpleNamespace(name=None,
                                                                     arguments=tc.function.arguments))]),
                finish_reason=None)], usage=None)
    yield SimpleNamespace(
        choices=[SimpleNamespace(delta=delta0, finish_reason=choice.finish_reason)],
        usage=usage)


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))

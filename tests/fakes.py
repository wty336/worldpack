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


def resp(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("假客户端响应耗尽——模型调用次数超出预期")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))

"""LLM 客户端与输出协议（W4）：核心循环的心脏（design.md §3.2）。

协议设计：所有输出都走工具调用，不依赖 response_format（跨提供商最稳）。
- change_stat：数值变更提议 → 引擎回调执行（StatsSystem 契约校验）；
- submit_narration：本轮叙事输出（narration / choices / plot_signal）。

失败处理（章 5 故障恢复）：
- API 层瞬时错误（限流/超时）：OpenAI SDK max_retries 静默重试；
- 协议层格式失败：以结构化 user 消息反馈原因并重试，最多 MAX_TURN_ITERATIONS 轮；
- 连续失败 → LLMTurnError（引擎级熔断信号）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from openai import OpenAI

from .config import Settings
from .stats import StatChangeError
from .worldpack import ScheduleSpec

MAX_OUTPUT_TOKENS = 2048  # 含思考链预算：重回合（抉择后叙事）需要余量，截断会导致无工具调用
MAX_TURN_ITERATIONS = 3  # 初始 1 次 + 协议失败重试 2 次（design.md §10.1 C6）


class LLMTurnError(Exception):
    """连续多轮未能产出合法协议输出（引擎级熔断信号）。"""


@dataclass
class TurnResult:
    narration: str
    choices: list[str]
    plot_signal: str
    stat_changes: list[dict] = field(default_factory=list)  # 已执行的变更提议（含结果）
    iterations: int = 1
    messages: list[dict] = field(default_factory=list)  # 更新后的轨迹，供引擎续用


def build_tools(schedule: ScheduleSpec) -> list[dict]:
    """按世界包动态构建工具 schema：枚举值注入，参数精确（章 4 ACI）。"""
    player_stats = sorted(schedule.stats)
    npc_ids = sorted(schedule.affections)
    return [
        {
            "type": "function",
            "function": {
                "name": "change_stat",
                "description": (
                    "提议一次数值变化，由引擎校验后执行。玩家属性单次变化幅度 ≤±10，"
                    "好感单次变化幅度 ≤±5；reason 必须说明剧情原因。引擎拒绝时按返回的错误修正或放弃。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "enum": ["player", *npc_ids],
                            "description": "变化对象：player 或 NPC id",
                        },
                        "stat": {
                            "type": "string",
                            "enum": [*player_stats, "affection"],
                            "description": "玩家属性名；对 NPC 恒为 'affection'",
                        },
                        "delta": {"type": "number", "description": "变化量，可正可负"},
                        "reason": {"type": "string", "description": "这次变化的剧情原因"},
                    },
                    "required": ["target", "stat", "delta", "reason"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_narration",
                "description": "提交本轮叙事输出。每一轮必须以它结束。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "narration": {
                            "type": "string",
                            "description": "面向玩家的旁白与 NPC 对话（Markdown）",
                        },
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 3,
                            "maxItems": 5,
                            "description": "3~5 个玩家可选行动（自然衔接剧情，不含世界观外元素）",
                        },
                        "plot_signal": {
                            "type": "string",
                            "enum": ["normal", "node_complete"],
                            "description": (
                                "node_complete 表示你认为当前主线节点目标已达成"
                                "（引擎会复核 flag，不采信自报）"
                            ),
                        },
                    },
                    "required": ["narration", "choices", "plot_signal"],
                },
            },
        },
    ]


def make_client(settings: Settings) -> OpenAI:
    """OpenAI 兼容客户端（DeepSeek）。"""
    return OpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        max_retries=5,  # API 层瞬时错误（限流/超时/连接抖动）静默重试，指数退避
        timeout=180.0,
    )


def _assistant_to_dict(msg: Any) -> dict:
    """把模型回复转成可回传的 assistant 消息。

    保留 reasoning_content：DeepSeek V4 在携带 tools 时要求回传思考内容（章 2），
    缺失会导致 400 错误。
    """
    d: dict[str, Any] = {
        "role": "assistant",
        "content": msg.content if msg.content is not None else "",
    }
    rc = getattr(msg, "reasoning_content", None)
    if rc:
        d["reasoning_content"] = rc
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ]
    return d


def _tool_result(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _protocol_fail(reason: str) -> dict:
    return {
        "role": "user",
        "content": f"[引擎提示] 你上一轮输出不符合协议：{reason}\n请重新生成本轮叙事。",
    }


def clean_narration(text: str) -> str:
    """兜底清洗：模型偶尔会把工具调用格式文本写进 narration（玩家会看到脏文本）。

    移除 <invoke>...</invoke> 整块，以及残留的裸 XML 标签行。提示词已禁止该行为，此函数是纠正层（章 1）。
    """
    text = re.sub(r"<invoke\b[^>]*>.*?</invoke>", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*</?[a-zA-Z_][\w-]*(\s[^>]*)?/?>\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def _validate_narration(args: Any) -> str | None:
    """校验 submit_narration 参数。合法返回 None，否则返回错误描述。"""
    if not isinstance(args, dict):
        return "submit_narration 参数必须是 JSON 对象"
    narration = args.get("narration")
    if not isinstance(narration, str) or not narration.strip():
        return "narration 必须是非空字符串"
    choices = args.get("choices")
    if not isinstance(choices, list) or not (3 <= len(choices) <= 5):
        return f"choices 必须是 3~5 个选项的列表，当前 {choices!r}"
    if not all(isinstance(c, str) and c.strip() for c in choices):
        return "choices 中每个选项必须是非空字符串"
    plot = args.get("plot_signal")
    if plot not in ("normal", "node_complete"):
        return f"plot_signal 必须是 'normal' 或 'node_complete'，当前 {plot!r}"
    return None


class LLMClient:
    """协议客户端：run_turn 跑一轮完整闭环（change_stat 循环 + submit_narration 收尾）。"""

    def __init__(self, client: OpenAI, model: str, tools: list[dict]):
        self._client = client
        self.model = model
        self.tools = tools

    def run_turn(
        self,
        messages: list[dict],
        apply_change: Callable[[dict], str],
        max_iters: int = MAX_TURN_ITERATIONS,
        on_text: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """执行一轮：组装 → 生成 → 执行工具 → 校验 → 返回 TurnResult。

        apply_change(args) 由引擎注入：内部走 StatsSystem 契约；抛 StatChangeError
        时这里转为结构化 tool 错误回传。
        on_text：提供时启用流式输出，内容增量实时回调（玩家边等边看）。
        """
        msgs = [dict(m) for m in messages]
        stat_changes: list[dict] = []

        for i in range(1, max_iters + 1):
            kwargs: dict[str, Any] = dict(
                model=self.model,
                messages=msgs,
                tools=self.tools,
                tool_choice="auto",
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            if on_text is not None:
                # openai SDK v3 的流式对象不聚合，需手动累积各 delta
                stream = self._client.chat.completions.create(**kwargs, stream=True)
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                tool_call_parts: dict[int, dict] = {}
                finish: str | None = None
                for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        continue
                    choice = chunk.choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish = choice.finish_reason
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    piece = getattr(delta, "content", None)
                    if piece:
                        content_parts.append(piece)
                        on_text(piece)
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        reasoning_parts.append(rc)
                    for tc_delta in getattr(delta, "tool_calls", None) or []:
                        idx = tc_delta.index
                        slot = tool_call_parts.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if getattr(tc_delta, "id", None):
                            slot["id"] = tc_delta.id
                        fn = getattr(tc_delta, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                slot["name"] += fn.name
                            if getattr(fn, "arguments", None):
                                slot["arguments"] += fn.arguments
                # 聚合为与 SDK 消息对象同形状的响应
                tool_calls_obj = None
                if tool_call_parts:
                    tool_calls_obj = [
                        SimpleNamespace(
                            id=tool_call_parts[i]["id"],
                            function=SimpleNamespace(
                                name=tool_call_parts[i]["name"],
                                arguments=tool_call_parts[i]["arguments"],
                            ),
                        )
                        for i in sorted(tool_call_parts)
                    ]
                resp = SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="".join(content_parts) or None,
                                tool_calls=tool_calls_obj,
                                reasoning_content="".join(reasoning_parts) or None,
                            ),
                            finish_reason=finish,
                        )
                    ]
                )
            else:
                resp = self._client.chat.completions.create(**kwargs, stream=False)
            msg = resp.choices[0].message
            finish = getattr(resp.choices[0], "finish_reason", None)
            msgs.append(_assistant_to_dict(msg))

            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                reason = (
                    f"未调用任何工具（finish_reason={finish}）。"
                    if finish == "length"
                    else "未调用任何工具。"
                )
                msgs.append(
                    _protocol_fail(
                        reason + "必须调用 submit_narration 结束本轮（数值变化用 change_stat）。"
                    )
                )
                continue

            narration_args: dict | None = None
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as e:
                    msgs.append(_tool_result(tc.id, f"[协议错误] 工具参数不是合法 JSON：{e}"))
                    continue

                if name == "change_stat":
                    try:
                        result_msg = apply_change(args)
                    except StatChangeError as e:
                        result_msg = f"[引擎拒绝] {e}"
                    msgs.append(_tool_result(tc.id, result_msg))
                    stat_changes.append({**args, "result": result_msg})
                elif name == "submit_narration":
                    err = _validate_narration(args)
                    if err is not None:
                        msgs.append(_tool_result(tc.id, f"[协议错误] {err}"))
                    else:
                        # 必须回配对的 tool 结果（否则带 tool_calls 的 assistant 消息
                        # 缺配对结果，下一次请求会被 API 拒绝）
                        msgs.append(_tool_result(tc.id, "已接收本轮叙事。"))
                        if narration_args is None:
                            narration_args = args
                else:
                    msgs.append(_tool_result(tc.id, f"[协议错误] 未知工具 '{name}'"))

            if narration_args is not None:
                return TurnResult(
                    narration=clean_narration(narration_args["narration"]),
                    choices=list(narration_args["choices"]),
                    plot_signal=narration_args["plot_signal"],
                    stat_changes=stat_changes,
                    iterations=i,
                    messages=msgs,
                )

        raise LLMTurnError(f"连续 {max_iters} 次未能产出合法协议输出（熔断）")

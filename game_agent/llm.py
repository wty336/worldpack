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
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from openai import OpenAI

from .budgets import TURN_MAX_TOKENS as MAX_OUTPUT_TOKENS
from .config import Settings
from .registry import ToolRegistry, from_callbacks, from_schedule
from .trace import TraceRecorder
from .usage import TokenCalibrator, UsageTracker, usage_fields
from .worldpack import ScheduleSpec

MAX_TURN_ITERATIONS = 3  # 初始 1 次 + 协议失败重试 2 次（design.md §10.1 C6）


class LLMTurnError(Exception):
    """连续多轮未能产出合法协议输出（引擎级熔断信号）。"""


@dataclass
class CompletionResult:
    """无工具补全的结果：正文 + 结束原因。

    `finish_reason == "length"` 表示推理链/正文吃光了 max_tokens——输出被截断，
    不可信（反例见 retro §8.2：flash 200 预算 6 连空；实测 5/6 次 reflect 调用顶满 500）。
    侧信道据此决定是否升级预算重试（见 budgets.complete_with_empty_retry）。
    """

    text: str
    finish_reason: str | None = None


@dataclass
class TurnResult:
    narration: str
    choices: list[str]
    plot_signal: str
    stat_changes: list[dict] = field(default_factory=list)  # 已执行的变更提议（含结果）
    memories: list[dict] = field(default_factory=list)  # 已写入的记忆提议（含结果，M2a）
    iterations: int = 1
    messages: list[dict] = field(default_factory=list)  # 更新后的轨迹，供引擎续用


def build_tools(schedule: ScheduleSpec) -> list[dict]:
    """按世界包动态构建工具 schema（批次 C：声明式注册表实现，输出与迁移前一致）。

    schema 单点真源在 registry.py（含世界包自定义工具）；本函数是兼容入口。
    """
    return from_schedule(schedule).schemas()


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
        "name": "engine",  # A-2：协议重试提示是引擎元消息
        "content": f"[引擎提示] 你上一轮输出不符合协议：{reason}\n请重新生成本轮叙事。",
    }


def _estimate_input_tokens(messages: list[dict]) -> int:
    """估算一次调用的输入 token（与 compression.est_tokens 同口径：1 字 ≈ 1 token）。"""
    est = 0
    for m in messages:
        est += len(str(m.get("content") or ""))
        calls = m.get("tool_calls") or []
        if calls:
            est += len(json.dumps(calls, ensure_ascii=False))
    return est


def clean_narration(text: str) -> str:
    """兜底清洗：模型偶尔会把工具调用格式文本写进 narration（玩家会看到脏文本）。

    移除 <invoke>...</invoke> 整块，以及残留的裸 XML 标签行。提示词已禁止该行为，此函数是纠正层（章 1）。
    """
    text = re.sub(r"<invoke\b[^>]*>.*?</invoke>", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*</?[a-zA-Z_][\w-]*(\s[^>]*)?/?>\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


class LLMClient:
    """协议客户端：run_turn 跑一轮完整闭环（change_stat 循环 + submit_narration 收尾）。

    C1/C2（P1）：
    - purpose（turn/judge/compress/extract/reflect/dedup/aux）同时决定
      ① 使用哪个模型（models 映射，缺省回退 self.model）与 ② usage 记账的用途标签；
    - tracker 非 None 时每次 API 调用落盘 usage（含缓存命中/未命中）。
    """

    def __init__(
        self,
        client: OpenAI,
        model: str,
        tools: list[dict],
        models: dict[str, str] | None = None,  # C1：purpose → 模型名
        tracker: UsageTracker | None = None,  # C2：usage 落盘
        no_thinking_side_channel: bool = False,  # 侧信道关思考（见 complete_with_meta）
        tracer: TraceRecorder | None = None,  # B1（Track B）：trace 落盘（None = 关闭）
        calibrator: TokenCalibrator | None = None,  # 批次 F：token 估算校正（None = 不校准）
    ):
        self._client = client
        self.model = model
        self.tools = tools
        self.models = models or {}
        self.tracker = tracker
        self.no_thinking_side_channel = no_thinking_side_channel
        self.tracer = tracer
        self.calibrator = calibrator
        self._turn_seq = 0  # B1：本进程内的叙事回合序号（trace 关联键）

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        tools: list[dict],
        tracker: UsageTracker | None = None,
    ) -> "LLMClient":
        """按 Settings 构造：主模型 + 侧信道专属模型路由（C1 + B3/Track B）。"""
        tracer = None
        if settings.trace_path:  # B1：GAME_AGENT_TRACE 非空才开 trace（默认零开销）
            tracer = TraceRecorder(settings.trace_path)
        return cls(
            make_client(settings),
            settings.model,
            tools,
            models={
                purpose: settings.model_for(purpose)
                for purpose in ("judge", "compress", "extract", "reflect", "dedup")
            },
            tracker=tracker,
            no_thinking_side_channel=settings.no_thinking_side_channel,
            tracer=tracer,
            calibrator=TokenCalibrator(),  # 批次 F：真实运行默认开启校准
        )

    def model_for(self, purpose: str) -> str:
        return self.models.get(purpose) or self.model

    def token_factor(self, purpose: str) -> float:
        """批次 F：该用途的估算校正因子（未校准 = 1.0，即原行为）。"""
        return self.calibrator.factor(purpose) if self.calibrator is not None else 1.0

    def _trace(self, event: str, **fields: Any) -> None:
        """B1：tracer 为 None 时零开销 no-op；落盘失败由 recorder 内部静默。"""
        if self.tracer is not None:
            self.tracer.record(event, **fields)

    def _record_usage(self, model: str, purpose: str, resp: Any) -> None:
        if self.tracker is not None:
            self.tracker.record(model, purpose, usage_fields(resp))

    def _calibrate(self, purpose: str, est_input: int, resp: Any) -> None:
        """批次 F：用真实 prompt_tokens 回填估算校正因子（无 usage/校准器则跳过）。"""
        if self.calibrator is None:
            return
        usage = usage_fields(resp) or {}
        self.calibrator.update(purpose, est_input, usage.get("prompt_tokens", 0))

    def complete(
        self,
        messages: list[dict],
        max_tokens: int = 400,
        temperature: float | None = None,
        purpose: str = "aux",
    ) -> str:
        """无工具纯文本补全（事实提取/压缩/Judge/去重/反思等侧信道用）。

        temperature 缺省走提供商默认值；判定类调用（如 Judge）传 0 以获得稳定结论。
        purpose 决定模型路由（C1）与 usage 标签（C2）。
        """
        return self.complete_with_meta(
            messages, max_tokens=max_tokens, temperature=temperature, purpose=purpose
        ).text

    def complete_with_meta(
        self,
        messages: list[dict],
        max_tokens: int = 400,
        temperature: float | None = None,
        purpose: str = "aux",
        no_thinking: bool | None = None,
    ) -> CompletionResult:
        """同 complete()，但额外返回 finish_reason（侧信道据此识别截断）。

        ``no_thinking``：关闭供应商的思考模式（默认读 ``self.no_thinking_side_channel``）。
        2026-09-12 实测：判官在**真轨迹材料**（材料 374~795 字）上思考停不下来——
        预算 500 → reasoning 800 字；预算 2000 → reasoning 7152 字，content 恒为 0（熔断），
        且截断会改变判词（4000 预算时草率输出"通过"，8000 时判"虚构"）。
        关思考后 reasoning=0、同一判词、tokens 1122（比放大预算省 2~3 倍）。
        """
        model = self.model_for(purpose)
        kwargs: dict[str, Any] = dict(
            model=model, messages=messages, max_tokens=max_tokens, stream=False
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        use_no_thinking = self.no_thinking_side_channel if no_thinking is None else no_thinking
        if use_no_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        est_input = _estimate_input_tokens(messages)  # 批次 F：校准观测量
        t0 = time.monotonic()
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            self._trace(
                "call", purpose=purpose, model=model, max_tokens=max_tokens,
                latency_ms=round((time.monotonic() - t0) * 1000),
                error=type(e).__name__,
            )
            raise
        latency_ms = round((time.monotonic() - t0) * 1000)
        self._record_usage(model, purpose, resp)
        self._calibrate(purpose, est_input, resp)
        choice = resp.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        self._trace(
            "call", purpose=purpose, model=model, max_tokens=max_tokens,
            latency_ms=latency_ms, finish_reason=finish_reason,
            tok_factor=round(self.token_factor(purpose), 3),
            usage=usage_fields(resp),
        )
        return CompletionResult(
            text=choice.message.content or "",
            finish_reason=finish_reason,
        )

    def run_turn(
        self,
        messages: list[dict],
        apply_change: Callable[[dict], str] | None = None,
        max_iters: int = MAX_TURN_ITERATIONS,
        on_text: Callable[[str], None] | None = None,
        remember: Callable[[dict], str] | None = None,
        query_world: Callable[[dict], str] | None = None,
        registry: ToolRegistry | None = None,
    ) -> TurnResult:
        """执行一轮：组装 → 生成 → 执行工具 → 校验 → 返回 TurnResult。

        批次 C：工具派发统一走 ToolRegistry（声明式注册表，registry.py）。两种传法：
        - registry=：完整注册表（Game 装配，含世界包自定义工具）——推荐路径；
        - 旧签名 apply_change/remember/query_world 回调：兼容路径，内部转即席注册表
          （from_callbacks），派发代码同一条；API 的 tools= 仍用 self.tools（保住枚举）。
        registry 路径的 API tools= 用 registry.schemas()（含世界包自定义工具）。

        apply_change(args)：内部走 StatsSystem 契约，抛 StatChangeError → 结构化拒绝；
        remember(args)：内部走 MemorySystem 契约，抛 MemoryError → 结构化拒绝；
        query_world(args)：**只读**查询世界状态，抛 ValueError → 结构化拒绝。
        on_text：提供时启用流式输出，内容增量实时回调（玩家边等边看）。
        """
        msgs = [dict(m) for m in messages]
        if registry is None:
            registry = from_callbacks(apply_change, remember, query_world)
            tools_schema = self.tools
        else:
            tools_schema = registry.schemas()
        buckets: dict[str, list[dict]] = {}  # tag → 调用记录（stat_changes / memories）

        model = self.model_for("turn")
        self._turn_seq += 1  # B1：回合序号（trace 关联键）
        turn_seq = self._turn_seq
        self._trace(
            "turn_begin", turn_seq=turn_seq, model=model,
            messages=len(msgs), max_iters=max_iters,
        )
        for i in range(1, max_iters + 1):
            kwargs: dict[str, Any] = dict(
                model=model,
                messages=msgs,
                tools=tools_schema,
                tool_choice="auto",
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            stream_usage = None  # C2：流式时 usage 在末尾 chunk（stream_options 开启后）
            est_input = _estimate_input_tokens(msgs)  # 批次 F：校准观测量（按本次请求计）
            t0 = time.monotonic()  # B1：本轮 API 调用时延（流式/非流式同源计时）
            if on_text is not None:
                # openai SDK v3 的流式对象不聚合，需手动累积各 delta
                stream = self._client.chat.completions.create(
                    **kwargs, stream=True, stream_options={"include_usage": True}
                )
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                tool_call_parts: dict[int, dict] = {}
                finish: str | None = None
                for chunk in stream:
                    usage_chunk = getattr(chunk, "usage", None)
                    if usage_chunk is not None:
                        stream_usage = usage_chunk
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
                    ],
                    usage=stream_usage,  # C2：可能为 None（provider 未回传）
                )
            else:
                resp = self._client.chat.completions.create(**kwargs, stream=False)
            latency_ms = round((time.monotonic() - t0) * 1000)
            self._record_usage(model, "turn", resp)
            self._calibrate("turn", est_input, resp)  # 批次 F
            self._trace(
                "call", turn_seq=turn_seq, purpose="turn", model=model,
                max_tokens=MAX_OUTPUT_TOKENS, latency_ms=latency_ms,
                finish_reason=getattr(resp.choices[0], "finish_reason", None),
                tok_factor=round(self.token_factor("turn"), 3),
                usage=usage_fields(resp),
            )
            msg = resp.choices[0].message
            finish = getattr(resp.choices[0], "finish_reason", None)
            msgs.append(_assistant_to_dict(msg))

            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                reason = (
                    f"未调用任何工具（finish_reason={finish}：输出因超长被截断）。"
                    if finish == "length"
                    else "未调用任何工具。"
                )
                # M3 世界包验证发现：长叙事场景下模型先写长文再调工具，输出触及上限被截断，
                # 若重试提示不点明"缩短"，模型会重复同样行为直到熔断。
                tip = (
                    "请大幅缩短叙事：先以 submit_narration 收尾"
                    "（narration 两三句即可，choices 照常 3~5 个），详细展开放到下一轮。"
                    if finish == "length"
                    else ""
                )
                msgs.append(
                    _protocol_fail(
                        reason + "必须调用 submit_narration 结束本轮"
                        "（数值变化用 change_stat，记忆用 remember）。" + tip
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
                    self._trace("tool", turn_seq=turn_seq, name=name, status="bad_json",
                                detail=str(e)[:80])
                    continue

                spec = registry.get(name)
                if spec is None:
                    result_msg = f"[协议错误] 未知工具 '{name}'"
                    msgs.append(_tool_result(tc.id, result_msg))
                    self._trace("tool", turn_seq=turn_seq, name=name, status="unknown",
                                detail=result_msg[:80])
                    continue
                if spec.terminator:
                    # submit_narration：协议收尾工具——校验后终止本轮，不走 handler
                    err = spec.validator(args) if spec.validator is not None else None
                    if err is not None:
                        result_msg = f"[协议错误] {err}"
                        status = "protocol_error"
                    else:
                        # 必须回配对的 tool 结果（否则带 tool_calls 的 assistant 消息
                        # 缺配对结果，下一次请求会被 API 拒绝）
                        result_msg = "已接收本轮叙事。"
                        status = "ok"
                        if narration_args is None:
                            narration_args = args
                    msgs.append(_tool_result(tc.id, result_msg))
                    self._trace("tool", turn_seq=turn_seq, name=name, status=status,
                                detail=result_msg[:80])
                    continue

                dr = registry.dispatch(name, args)
                msgs.append(_tool_result(tc.id, dr.message))
                if spec.tag is not None and dr.status in ("ok", "rejected"):
                    buckets.setdefault(spec.tag, []).append({**args, "result": dr.message})
                self._trace("tool", turn_seq=turn_seq, name=name, status=dr.status,
                            detail=dr.message[:80])

            if narration_args is not None:
                narration = clean_narration(narration_args["narration"])
                self._trace(
                    "turn_end", turn_seq=turn_seq, outcome="completed", iterations=i,
                    narration_chars=len(narration),
                    choices=len(narration_args["choices"]),
                    plot_signal=narration_args["plot_signal"],
                    stat_changes=len(buckets.get("stat_changes", [])),
                    memories=len(buckets.get("memories", [])),
                )
                return TurnResult(
                    narration=narration,
                    choices=list(narration_args["choices"]),
                    plot_signal=narration_args["plot_signal"],
                    stat_changes=buckets.get("stat_changes", []),
                    memories=buckets.get("memories", []),
                    iterations=i,
                    messages=msgs,
                )

        self._trace("turn_end", turn_seq=turn_seq, outcome="meltdown", iterations=max_iters)
        raise LLMTurnError(f"连续 {max_iters} 次未能产出合法协议输出（熔断）")

"""工具注册表（设计加固批次 C，docs/plan-design-hardening.md §3）：工具层声明式改造。

此前四个引擎工具写死在 llm.build_tools()（schema）+ run_turn 的 if/elif 分发——
每加一个工具改三处，世界包无法扩展。本模块把工具变成一等公民：

- ToolSpec：name / schema / handler / rejects / 记账标签的声明式描述；
- ToolRegistry：register / schemas / dispatch 单点派发（llm.run_turn 经此路由）；
- 引擎四件套（change_stat / submit_narration / remember / query_world）在此注册，
  schema 与迁移前逐字段一致（golden 测试钉住），行为不变；
- 世界包自定义工具（schedule.yaml 的 tools: 段）经 from_schedule 注册——
  效果型领域动作（修炼包 breakthrough / 都市包 hack）不再硬编码；
- MCP server（game_agent/mcp_server.py）用同一机制注册**玩家侧**工具——
  同一派发机制、两个消费面；叙述者工具不对外（玩家无权直改数值）。

导入方向：worldpack（schema）← registry ← llm / game / mcp_server，无环。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .memory import MemoryError
from .stats import StatChangeError
from .worldpack import ENGINE_TOOL_NAMES, ScheduleSpec, WorldPack, WorldPackError


class ToolRegistryError(Exception):
    """注册表使用错误（重复注册 / 绑定未知工具）。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的声明式描述。

    - handler：args -> 结果字符串（引擎拒绝时抛 rejects 中的异常类型）；
      None = 未启用（返回 disabled_msg，trace 记 protocol_error）；
    - terminator：submit_narration 这类协议收尾工具——不走 handler，由
      run_turn 用 validator 校验后终止本轮，且不进 MCP 工具面；
    - tag：TurnResult 记账桶名（stat_changes / memories）——run_turn 把
      ok/rejected 的调用按 tag 归集，替代迁移前的硬编码分支。
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: Callable[[dict], str] | None = None
    rejects: tuple[type[Exception], ...] = ()
    disabled_msg: str = ""
    terminator: bool = False
    validator: Callable[[dict], str | None] | None = None
    tag: str | None = None


@dataclass(frozen=True)
class DispatchResult:
    """一次派发的结果：回传给模型的文本 + trace 状态（与迁移前口径一致）。"""

    message: str
    status: str  # ok / rejected / protocol_error / unknown


class ToolRegistry:
    """工具注册表：schema 输出与派发的单一真源。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ToolRegistryError(f"工具重复注册: '{spec.name}'")
        self._tools[spec.name] = spec

    def bind_handler(self, name: str, handler: Callable[[dict], str]) -> None:
        """给已注册的工具绑定 handler（Game 装配引擎回调 / 自定义工具时用）。"""
        if name not in self._tools:
            raise ToolRegistryError(f"绑定未知工具: '{name}'")
        self._tools[name] = replace(self._tools[name], handler=handler)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, include_internal: bool = True) -> list[dict]:
        """OpenAI tools 参数格式；include_internal=False 供外部工具面（MCP）剔除协议工具。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
            if include_internal or not t.terminator
        ]

    def dispatch(self, name: str, args: dict) -> DispatchResult:
        """单点派发。异常 → 结构化拒绝文本（绝不向调用方抛异常）。"""
        spec = self._tools.get(name)
        if spec is None:
            return DispatchResult(f"[协议错误] 未知工具 '{name}'", "unknown")
        if spec.handler is None:
            msg = spec.disabled_msg or f"[协议错误] 工具 '{name}' 未启用"
            return DispatchResult(msg, "protocol_error")
        try:
            return DispatchResult(spec.handler(args), "ok")
        except spec.rejects as e:  # noqa: BLE001 — 只捕获声明的拒绝类型
            return DispatchResult(f"[引擎拒绝] {e}", "rejected")


# ---------------------------------------------------------------------------
# 引擎四件套（schema 与迁移前的 llm.build_tools 逐字段一致）
# ---------------------------------------------------------------------------


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


def _change_stat_spec(schedule: ScheduleSpec) -> ToolSpec:
    player_stats = sorted(schedule.stats)
    npc_ids = sorted(schedule.affections)
    return ToolSpec(
        name="change_stat",
        description=(
            "提议一次数值变化，由引擎校验后执行。玩家属性单次变化幅度 ≤±10，"
            "好感单次变化幅度 ≤±5；reason 必须说明剧情原因。引擎拒绝时按返回的错误修正或放弃。"
        ),
        parameters={
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
        rejects=(StatChangeError,),
        tag="stat_changes",
    )


def _submit_narration_spec() -> ToolSpec:
    return ToolSpec(
        name="submit_narration",
        description="提交本轮叙事输出。每一轮必须以它结束。",
        parameters={
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
        terminator=True,
        validator=_validate_narration,
    )


def _remember_spec(schedule: ScheduleSpec) -> ToolSpec:
    npc_ids = sorted(schedule.affections)
    return ToolSpec(
        name="remember",
        description=(
            "记录一条值得长期记住的关键事实（可选，只在出现重要事实时调用）。"
            "玩家的长期信息（身世/剑名/师承/喜好/承诺/约定等）记到 target='player'；"
            "某个 NPC 对玩家的关键记忆记到 target=该 NPC 的 id。"
            "例：『玩家的剑名是听雨』『玩家答应帮老樵夫送柴』"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["player", *npc_ids],
                    "description": "记忆归属：player 或 NPC id",
                },
                "fact": {
                    "type": "string",
                    "description": "一句话事实，≤120 字，如「玩家承诺中秋前备齐聘银五十两」",
                },
                "importance": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": (
                        "重要性 1~10（缺省 5）：8-10 身份身世/生死承诺/命运级；"
                        "5-7 重要关系进展与关键事件；1-4 日常喜好琐事"
                    ),
                },
            },
            "required": ["target", "fact"],
        },
        rejects=(MemoryError,),
        disabled_msg="[协议错误] 引擎未启用记忆功能",
        tag="memories",
    )


def _query_world_spec() -> ToolSpec:
    return ToolSpec(
        name="query_world",
        description=(
            "只读查询当前世界状态：地点/时间/在场人物、属性与好感、主线目标、"
            "与查询相关的长期记忆与世界观设定、当前可选行动。"
            "对状态不确定时**先查询再叙事**，查不到的不得编造；"
            "查询结果只作叙事依据，不得原样复述给玩家，也不得泄露引擎机制。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "想查的问题或关键词，如「沈清秋喜欢什么」「现在在哪」",
                },
            },
            "required": ["query"],
        },
        rejects=(ValueError,),
        disabled_msg="[协议错误] 引擎未启用世界查询功能",
    )


# ---------------------------------------------------------------------------
# 世界包自定义工具（schedule.yaml 的 tools: 段，批次 C）
# ---------------------------------------------------------------------------


def _custom_tool_spec(tool) -> ToolSpec:
    """schedule.tools 条目 → ToolSpec（schema；handler 由 Game 绑定）。"""
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": dict(tool.parameters),
    }
    if tool.required:
        parameters["required"] = list(tool.required)
    return ToolSpec(
        name=tool.id,
        description=f"{tool.label}：{tool.description}",
        parameters=parameters,
        rejects=(ValueError, StatChangeError),
    )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def from_schedule(schedule: ScheduleSpec) -> ToolRegistry:
    """引擎四件套 + 世界包自定义工具（schema-only；handler 待 Game 绑定）。"""
    reg = ToolRegistry()
    reg.register(_change_stat_spec(schedule))
    reg.register(_submit_narration_spec())
    reg.register(_remember_spec(schedule))
    reg.register(_query_world_spec())
    seen = set(ENGINE_TOOL_NAMES)
    for tool in schedule.tools:
        if tool.id in seen:
            raise WorldPackError(f"自定义工具 '{tool.id}' 与已注册工具重名")
        seen.add(tool.id)
        reg.register(_custom_tool_spec(tool))
    return reg


def from_callbacks(
    apply_change: Callable[[dict], str] | None,
    remember: Callable[[dict], str] | None,
    query_world: Callable[[dict], str] | None,
) -> ToolRegistry:
    """旧回调签名兼容：apply_change/remember/query_world → 即席注册表。

    派发与 registry 路径走**同一条代码**（run_turn 内无分支）。schema 枚举为空
    ——兼容路径的 API schema 仍用调用方传入的 self.tools（llm.run_turn 处理），
    此处只承担派发职责（handler/rejects/disabled_msg/tag）。
    """
    reg = ToolRegistry()
    empty = ScheduleSpec(stats={}, affections={})

    def _bound(spec: ToolSpec, fn: Callable[[dict], str] | None) -> ToolSpec:
        return replace(spec, handler=(lambda args, f=fn: f(args)) if fn else None)

    reg.register(_bound(_change_stat_spec(empty), apply_change))
    reg.register(_submit_narration_spec())
    reg.register(_bound(_remember_spec(empty), remember))
    reg.register(_bound(_query_world_spec(), query_world))
    return reg


def build_registry(pack: WorldPack) -> ToolRegistry:
    """完整注册表 = from_schedule + 地点声明的 change_scene（批次 D）。"""
    reg = from_schedule(pack.schedule)
    if pack.world.locations:
        reg.register(_change_scene_spec(pack.world))
    return reg


def _change_scene_spec(world) -> ToolSpec:
    """批次 D：场景移动提议（仅在 world.locations 声明时注册）。"""
    loc_ids = [l.id for l in world.locations]
    return ToolSpec(
        name="change_scene",
        description=(
            "提议一次场景移动（由引擎校验后执行）。location 必须是地点表中的 id；"
            "reason 必须说明移动的剧情原因。叙事中玩家位置发生变化时必须调用本工具，"
            "引擎会在状态栏同步更新场景；关键抉择期间不可移动。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "enum": loc_ids,
                    "description": "目标地点 id（只能从声明表中选）",
                },
                "reason": {"type": "string", "description": "移动的剧情原因"},
            },
            "required": ["location", "reason"],
        },
        rejects=(ValueError,),
    )

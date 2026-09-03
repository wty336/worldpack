"""世界包 schema 与加载器（W2）。

世界包 = 一个文件夹内的纯 YAML 内容（见 docs/design.md §12）：
  world.yaml / schedule.yaml / mainline.yaml / events.yaml / endings.yaml / npcs/*.yaml

加载器做两类校验：
  1. pydantic schema 校验（字段缺失/类型错误直接拒绝——约束编码化）；
  2. 跨文件交叉校验（引用了未声明的 flag/好感/NPC/行动 → 拒绝）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from .conditions import ConditionError, validate_condition


class WorldPackError(Exception):
    """世界包加载/校验错误（面向世界包作者的友好报错）。"""


# ---------------------------------------------------------------------------
# world.yaml
# ---------------------------------------------------------------------------


class WorldSpec(BaseModel):
    name: str
    era: str
    start_scene: str = ""  # 开局场景（引擎启动时写入 state.scene）
    core_rules: list[str] = Field(default_factory=list)
    style_guide: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    opening: str = ""


# ---------------------------------------------------------------------------
# schedule.yaml
# ---------------------------------------------------------------------------


class StatSpec(BaseModel):
    label: str
    min: float = 0
    max: float = 100
    initial: float


class AffectionSpec(BaseModel):
    label: str
    min: float = 0
    max: float = 100
    initial: float


class ActionEffects(BaseModel):
    stats: dict[str, float] = Field(default_factory=dict)
    affections: dict[str, float] = Field(default_factory=dict)


class ActionSpec(BaseModel):
    id: str
    label: str
    cost: int = 1
    effects: ActionEffects = Field(default_factory=ActionEffects)
    scene: str = ""  # 行动发生的地点（执行后写入 state.scene）
    present: list[str] = Field(default_factory=list)  # 行动时在场的 NPC id


class ScheduleSpec(BaseModel):
    day_action_points: int = 1
    stats: dict[str, StatSpec]
    affections: dict[str, AffectionSpec]
    flags: dict[str, bool] = Field(default_factory=dict)
    actions: list[ActionSpec] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# npcs/*.yaml
# ---------------------------------------------------------------------------


class AffectionStage(BaseModel):
    range: tuple[float, float]  # YAML 写 [min, max]
    tone: str


class NpcSpec(BaseModel):
    id: str
    name: str
    identity: str
    personality: str
    speech_style: str
    secrets: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    affection_stages: list[AffectionStage] = Field(default_factory=list)
    memory_limit: int = 20


# ---------------------------------------------------------------------------
# mainline.yaml
# ---------------------------------------------------------------------------


class ChoiceOption(BaseModel):
    text: str
    effects: dict[str, Any] = Field(default_factory=dict)


class CriticalChoice(BaseModel):
    id: str
    prompt: str
    options: list[ChoiceOption]


class OnEnter(BaseModel):
    scene: str
    briefing: str = ""
    present: list[str] = Field(default_factory=list)  # 节点开场在场的 NPC id


class NodeSpec(BaseModel):
    id: str
    title: str
    when: dict[str, Any] = Field(default_factory=dict)  # 条件表达式，W3 实现求值
    goal: str
    completion: dict[str, Any] = Field(default_factory=dict)
    on_enter: OnEnter
    critical_choices: list[CriticalChoice] = Field(default_factory=list)
    free_scope: str = ""


class MainlineSpec(BaseModel):
    nodes: list[NodeSpec]


# ---------------------------------------------------------------------------
# events.yaml
# ---------------------------------------------------------------------------


class EventTrigger(BaseModel):
    kind: Literal["condition", "schedule"]
    when: dict[str, Any] | None = None  # kind=condition 时必填（加载器交叉校验）
    action: str | None = None  # kind=schedule 时必填：对应日程行动 id
    chance: float | None = None  # kind=schedule 时可选：触发概率 0~1，缺省 1.0


class EventSpec(BaseModel):
    id: str
    title: str
    trigger: EventTrigger
    priority: Literal["high", "normal", "low"] = "normal"
    script: str = ""
    effects: dict[str, Any] = Field(default_factory=dict)
    once: bool = True


class EventsSpec(BaseModel):
    events: list[EventSpec]


# ---------------------------------------------------------------------------
# endings.yaml
# ---------------------------------------------------------------------------


class EndingSpec(BaseModel):
    id: str
    title: str
    kind: Literal["auto", "choice"] = "auto"
    when: dict[str, Any] = Field(default_factory=dict)
    text: str = ""


class EndingsSpec(BaseModel):
    endings: list[EndingSpec]


# ---------------------------------------------------------------------------
# 世界包整体
# ---------------------------------------------------------------------------


@dataclass
class WorldPack:
    root: Path
    world: WorldSpec
    schedule: ScheduleSpec
    mainline: MainlineSpec
    events: EventsSpec
    endings: EndingsSpec
    npcs: dict[str, NpcSpec]


# ---------------------------------------------------------------------------
# 加载与交叉校验
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise WorldPackError(f"缺少文件: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise WorldPackError(f"YAML 解析失败: {path}\n{e}") from e
    if not isinstance(data, dict):
        raise WorldPackError(f"文件根节点必须是映射（key: value），当前不是: {path}")
    return data


def _validate(model_cls: type[BaseModel], data: dict[str, Any], path: Path) -> BaseModel:
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        raise WorldPackError(f"schema 校验失败: {path}\n{e}") from e


def _collect_refs(node: Any, key_name: str, out: set[str]) -> None:
    """递归收集条件/效果字典中 key_name 键（如 flags / affection / affections）的子键名。

    例：{"flags": {"met_shen": true}} → 收集 "met_shen"
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key_name and isinstance(v, dict):
                out.update(v.keys())
            else:
                _collect_refs(v, key_name, out)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, key_name, out)


def _cross_check(pack_parts: dict[str, Any]) -> None:
    """跨文件交叉校验：引用的 flag/好感/NPC/行动必须已在 schedule 中声明。"""
    schedule: ScheduleSpec = pack_parts["schedule"]
    mainline: MainlineSpec = pack_parts["mainline"]
    events: EventsSpec = pack_parts["events"]
    endings: EndingsSpec = pack_parts["endings"]
    npcs: dict[str, NpcSpec] = pack_parts["npcs"]

    declared_flags = set(schedule.flags)
    declared_affections = set(schedule.affections)
    declared_stats = set(schedule.stats)
    action_ids = {a.id for a in schedule.actions}

    # 1) 收集全部引用 + 校验条件结构（when/completion 语法错误在加载期暴露）
    def _check_cond(cond: dict[str, Any], where: str) -> None:
        try:
            validate_condition(cond, where)
        except ConditionError as e:
            raise WorldPackError(str(e)) from e

    flag_refs: set[str] = set()
    aff_refs: set[str] = set()
    stat_refs: set[str] = set()
    for node in mainline.nodes:
        dumped = node.model_dump()
        _collect_refs(dumped, "flags", flag_refs)
        _collect_refs(dumped, "stat", stat_refs)
        _check_cond(node.when, f"主线节点 '{node.id}' 的 when")
        _check_cond(node.completion, f"主线节点 '{node.id}' 的 completion")
        missing_npcs = set(node.on_enter.present) - set(npcs)
        if missing_npcs:
            raise WorldPackError(
                f"主线节点 '{node.id}' 的 on_enter.present 引用了不存在的 NPC: "
                f"{sorted(missing_npcs)}"
            )
    for ev in events.events:
        dumped = ev.model_dump()
        _collect_refs(dumped, "flags", flag_refs)
        _collect_refs(dumped, "affection", aff_refs)
        _collect_refs(dumped, "affections", aff_refs)
        _collect_refs(dumped, "stat", stat_refs)
        if ev.trigger.when is not None:
            _check_cond(ev.trigger.when, f"事件 '{ev.id}' 的 trigger.when")
    for ending in endings.endings:
        dumped = ending.model_dump()
        _collect_refs(dumped, "flags", flag_refs)
        _collect_refs(dumped, "affection", aff_refs)
        _collect_refs(dumped, "affections", aff_refs)
        _collect_refs(dumped, "stat", stat_refs)
        _check_cond(ending.when, f"结局 '{ending.id}' 的 when")

    # 2) 逐项比对，报错带具体名字与出处文件
    missing_flags = flag_refs - declared_flags
    if missing_flags:
        raise WorldPackError(
            f"引用了未声明的 flag: {sorted(missing_flags)}——请在 schedule.yaml 的 flags 中声明"
        )
    missing_aff = aff_refs - declared_affections
    if missing_aff:
        raise WorldPackError(
            f"引用了未声明的好感对象: {sorted(missing_aff)}——请在 schedule.yaml 的 affections 中声明"
        )
    missing_stats = stat_refs - declared_stats
    if missing_stats:
        raise WorldPackError(
            f"引用了未声明的属性: {sorted(missing_stats)}——请在 schedule.yaml 的 stats 中声明"
        )
    for aff_id in declared_affections:
        if aff_id not in npcs:
            raise WorldPackError(
                f"好感对象 '{aff_id}' 缺少对应角色卡 npcs/{aff_id}.yaml"
            )

    # 3) 日程行动的 effects 只能引用已声明属性/好感；present 只能引用已声明 NPC
    for action in schedule.actions:
        bad_stats = set(action.effects.stats) - declared_stats
        if bad_stats:
            raise WorldPackError(
                f"行动 '{action.id}' 的效果引用了未声明的属性: {sorted(bad_stats)}"
            )
        bad_aff = set(action.effects.affections) - declared_affections
        if bad_aff:
            raise WorldPackError(
                f"行动 '{action.id}' 的效果引用了未声明的好感对象: {sorted(bad_aff)}"
            )
        bad_npcs = set(action.present) - set(npcs)
        if bad_npcs:
            raise WorldPackError(
                f"行动 '{action.id}' 的 present 引用了不存在的 NPC: {sorted(bad_npcs)}"
            )

    # 4) 事件触发校验：condition 必须有 when；schedule 必须有 action 且存在于行动表
    for ev in events.events:
        if ev.trigger.kind == "condition" and ev.trigger.when is None:
            raise WorldPackError(f"事件 '{ev.id}' 是条件触发，但缺少 trigger.when")
        if ev.trigger.kind == "schedule":
            if ev.trigger.action is None:
                raise WorldPackError(f"事件 '{ev.id}' 是日程触发，但缺少 trigger.action")
            if ev.trigger.action not in action_ids:
                raise WorldPackError(
                    f"事件 '{ev.id}' 引用了不存在的日程行动 '{ev.trigger.action}'"
                )
            chance = ev.trigger.chance
            if chance is not None and not (0.0 <= chance <= 1.0):
                raise WorldPackError(
                    f"事件 '{ev.id}' 的 chance 必须在 0~1 之间，当前为 {chance}"
                )

    # 5) 节点 completion 可达性：要求的 flag 必须有代码路径可写
    #    （关键选择选项效果 / 事件效果 / 日程行动效果），否则节点永远无法完成。
    writable_flags: set[str] = set()
    for node in mainline.nodes:
        for choice in node.critical_choices:
            for opt in choice.options:
                _collect_refs(opt.effects, "flags", writable_flags)
    for ev in events.events:
        _collect_refs(ev.effects, "flags", writable_flags)
    for action in schedule.actions:
        _collect_refs(action.effects.model_dump(), "flags", writable_flags)
    for node in mainline.nodes:
        completion_flags: set[str] = set()
        _collect_refs(node.completion, "flags", completion_flags)
        unreachable = completion_flags - writable_flags
        if unreachable:
            raise WorldPackError(
                f"主线节点 '{node.id}' 的 completion 要求 flag {sorted(unreachable)}，"
                f"但没有任何代码路径（关键选择/事件/日程行动的效果）能写入它们——"
                f"该节点将永远无法完成"
            )


def load_worldpack(root: str | Path) -> WorldPack:
    """加载并校验一个世界包文件夹。失败抛 WorldPackError（带可读信息）。"""
    root = Path(root)
    world = _validate(WorldSpec, _read_yaml(root / "world.yaml"), root / "world.yaml")
    schedule = _validate(
        ScheduleSpec, _read_yaml(root / "schedule.yaml"), root / "schedule.yaml"
    )
    mainline = _validate(
        MainlineSpec, _read_yaml(root / "mainline.yaml"), root / "mainline.yaml"
    )
    events = _validate(
        EventsSpec, _read_yaml(root / "events.yaml"), root / "events.yaml"
    )
    endings = _validate(
        EndingsSpec, _read_yaml(root / "endings.yaml"), root / "endings.yaml"
    )

    npcs_dir = root / "npcs"
    if not npcs_dir.is_dir():
        raise WorldPackError(f"缺少 npcs/ 目录: {npcs_dir}")
    npcs: dict[str, NpcSpec] = {}
    for f in sorted(npcs_dir.glob("*.yaml")):
        npc = _validate(NpcSpec, _read_yaml(f), f)
        if npc.id in npcs:
            raise WorldPackError(f"NPC id 重复: '{npc.id}'（文件 {f}）")
        npcs[npc.id] = npc
    if not npcs:
        raise WorldPackError(f"npcs/ 目录下没有任何角色卡: {npcs_dir}")

    parts = {
        "world": world,
        "schedule": schedule,
        "mainline": mainline,
        "events": events,
        "endings": endings,
        "npcs": npcs,
    }
    _cross_check(parts)

    return WorldPack(root=root, **parts)

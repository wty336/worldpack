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
    player_role: str = ""  # 玩家的身份（常驻状态栏，给玩家与模型稳定的"我是谁"）
    player_goal: str = ""  # 玩家的长期目标（常驻状态栏，防剧情漂移）
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


class EffectSpec(BaseModel):
    """D3（P2）：带随机/衰减的收益曲线（design.md §5.3）。

    - spread：均匀随机 ±spread（0 = 固定值）；
    - decay_every/decay_step：按执行前属性值边际递减——每 decay_every 点属性，
      收益幅度减 decay_step，最低 0（无负循环）。0 表示关闭。
    最终收益 = clamp(base - floor(当前属性/decay_every)*decay_step, 0, ∞) ± spread，
    并按符号截断（正收益不小于 0）。
    """

    base: float
    spread: float = 0.0
    decay_every: float = 0.0
    decay_step: float = 0.0


EffectValue = float | EffectSpec  # 普通数字 = 确定性作者定义；dict = 随机收益曲线


class ActionEffects(BaseModel):
    stats: dict[str, EffectValue] = Field(default_factory=dict)
    affections: dict[str, EffectValue] = Field(default_factory=dict)


class ActionCheck(BaseModel):
    """D1（P2）：日程行动检定（design.md §5.3）。

    roll = stat + uniform(-noise, +noise)：
      roll ≥ difficulty + margin → 大成功（critical_effects）
      roll ≥ difficulty        → 成功（effects）
      否则                     → 失败（failure_effects）
    """

    stat: str
    difficulty: float = 0
    margin: float = 10
    noise: float = 10


class ActionSpec(BaseModel):
    id: str
    label: str
    cost: int = 1
    requires: dict[str, Any] | None = None  # D2（P2）：执行门槛，复用条件 DSL（evaluate）
    check: ActionCheck | None = None  # D1（P2）：可选检定
    effects: ActionEffects = Field(default_factory=ActionEffects)  # 成功档（无检定时的基准）
    critical_effects: ActionEffects | None = None  # 大成功档（缺省回落 effects）
    failure_effects: ActionEffects | None = None  # 失败档（缺省回落 effects）
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
    kind: Literal["condition", "schedule", "time"]
    when: dict[str, Any] | None = None  # kind=condition 时必填（任意条件）；kind=time 时必填（仅 day 条件）
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


def _validate_effect_values(action_id: str, label: str, effects: ActionEffects) -> None:
    """校验收益曲线参数（spread/decay 非负）。"""
    for where, values in (("stats", effects.stats), ("affections", effects.affections)):
        for name, value in values.items():
            if not isinstance(value, EffectSpec):
                continue
            for field_name in ("spread", "decay_every", "decay_step"):
                if getattr(value, field_name) < 0:
                    raise WorldPackError(
                        f"行动 '{action_id}' 的 {label}.{where}.{name} 的 "
                        f"'{field_name}' 不能为负（当前 {getattr(value, field_name):g}）"
                    )


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

    # 3) 日程行动：检定属性声明、requires 条件与引用、效果引用、present NPC
    for action in schedule.actions:
        if action.check is not None and action.check.stat not in declared_stats:
            raise WorldPackError(
                f"行动 '{action.id}' 的检定 check.stat 引用了未声明的属性 '{action.check.stat}'"
            )
        if action.requires is not None:
            try:
                validate_condition(action.requires, f"行动 '{action.id}' 的 requires")
            except ConditionError as e:
                raise WorldPackError(str(e)) from e
            req_flags, req_stats, req_affs = set(), set(), set()
            _collect_refs(action.requires, "flags", req_flags)
            _collect_refs(action.requires, "stat", req_stats)
            _collect_refs(action.requires, "affection", req_affs)
            if req_flags - declared_flags:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 requires 引用了未声明的 flag: "
                    f"{sorted(req_flags - declared_flags)}"
                )
            if req_stats - declared_stats:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 requires 引用了未声明的属性: "
                    f"{sorted(req_stats - declared_stats)}"
                )
            if req_affs - declared_affections:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 requires 引用了未声明的好感对象: "
                    f"{sorted(req_affs - declared_affections)}"
                )
        for label, effects in (
            ("effects", action.effects),
            ("critical_effects", action.critical_effects),
            ("failure_effects", action.failure_effects),
        ):
            if effects is None:
                continue
            _validate_effect_values(action.id, label, effects)
            bad_stats = set(effects.stats) - declared_stats
            if bad_stats:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 {label} 引用了未声明的属性: {sorted(bad_stats)}"
                )
            bad_aff = set(effects.affections) - declared_affections
            if bad_aff:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 {label} 引用了未声明的好感对象: {sorted(bad_aff)}"
                )
        bad_npcs = set(action.present) - set(npcs)
        if bad_npcs:
            raise WorldPackError(
                f"行动 '{action.id}' 的 present 引用了不存在的 NPC: {sorted(bad_npcs)}"
            )

    # 4) 事件触发校验：condition/time 必须有 when；schedule 必须有 action 且存在于行动表
    for ev in events.events:
        if ev.trigger.kind == "condition" and ev.trigger.when is None:
            raise WorldPackError(f"事件 '{ev.id}' 是条件触发，但缺少 trigger.when")
        if ev.trigger.kind == "time":
            if ev.trigger.when is None:
                raise WorldPackError(f"事件 '{ev.id}' 是时间触发，但缺少 trigger.when")
            if set(ev.trigger.when) != {"day"}:
                raise WorldPackError(
                    f"事件 '{ev.id}' 是时间触发，trigger.when 只能包含 day 条件，"
                    f"当前为 {sorted(ev.trigger.when)}"
                )
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
        for label, effects in (
            ("effects", action.effects),
            ("critical_effects", action.critical_effects),
            ("failure_effects", action.failure_effects),
        ):
            if effects is not None:
                _collect_refs(effects.model_dump(), "flags", writable_flags)
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

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


# 引擎内置工具名（批次 C）：自定义工具不得与之重名（registry 与交叉校验共用此清单）
ENGINE_TOOL_NAMES = (
    "change_stat", "submit_narration", "remember", "query_world", "do_action",
)


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
    lore: list["LoreSpec"] = Field(default_factory=list)  # B1（P3）：Lorebook 条目（按需注入）
    locations: list["LocationSpec"] = Field(default_factory=list)  # 批次 D：地点表（声明后 scene 受校验）


class LoreSpec(BaseModel):
    """B1（P3）：一条 Lorebook 条目（地点/势力/物品/传闻）。

    keys：触发关键词（命中任一即候选）；text：注入文本。lore 不进静态前缀，
    由上下文组装器按「场景 + 节点目标 + 近对话」动态注入（字符预算内）。
    """

    id: str
    keys: list[str]
    text: str


class LocationSpec(BaseModel):
    """批次 D：地点表条目——把场景从自由字符串升级为一等公民。

    - id：引擎与 change_scene 工具使用的标识（世界包内唯一）；
    - name：状态栏/场景卡的显示名（写入 state.scene）；
    - keys：该地点的 lore 触发关键词（在场时恒参与 lore 命中）；
    - description：给 change_scene 提议参考的地点说明（不注入状态栏）。
    未声明 locations 时引擎行为与旧版完全一致（scene 为自由字符串）。
    """

    id: str
    name: str
    keys: list[str] = Field(default_factory=list)
    description: str = ""


def resolve_scene(world: "WorldSpec", scene: str) -> tuple[str, str]:
    """把 scene 字段解析为 (显示名, 地点 id)（批次 D）。

    - 匹配 location id → (location.name, location.id)；
    - 匹配 location name → (location.name, location.id)；
    - 都不匹配（或未声明地点表）→ 原样透传 (scene, "")——未声明地点表的世界包
      行为与旧版完全一致。
    """
    if not scene:
        return scene, ""
    for loc in world.locations:
        if scene == loc.id:
            return loc.name, loc.id
    for loc in world.locations:
        if scene == loc.name:
            return loc.name, loc.id
    return scene, ""


def find_location(world: "WorldSpec", loc_id: str):
    """按 id 查地点表条目；未命中返回 None（change_scene 白名单校验用）。"""
    for loc in world.locations:
        if loc.id == loc_id:
            return loc
    return None


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


class CounterSpec(BaseModel):
    """批次 E：计数器——int 真值（计数型机制状态，如"赠礼次数""突破层数"）。

    只能由代码路径（行动/事件/关键选择/自定义工具的效果）写入，LLM 不可写
    （与 flags 同纪律）；状态栏展示、事实图接地、条件 DSL 可引用。
    """

    label: str
    initial: int = 0
    min: int = 0
    max: int = 999999


class ItemSpec(BaseModel):
    """批次 E：物品——集合真值（持有/失去，如"听雨剑""密码本"）。"""

    id: str
    label: str
    initial: bool = False  # 开局是否已持有


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
    counters: dict[str, int] = Field(default_factory=dict)  # 批次 E：计数器增减
    items: dict[str, list[str]] = Field(default_factory=dict)  # 批次 E：{"gain": [...], "lose": [...]}


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


class CustomToolSpec(BaseModel):
    """批次 C：世界包自定义效果型工具（LLM 在叙事中提议，引擎校验结算）。

    - id：工具名（不得与引擎四件套重名，世界包内唯一）；
    - label / description：给模型的名称与用途说明；
    - parameters / required：可选参数的 JSON schema properties（缺省无参）；
    - requires：执行门槛（复用条件 DSL，如「已结识 + 银两 ≥20」）；
    - cost：消耗行动点（缺省 0 = 纯叙事动作）；
    - effects：结算效果（stats/affections/flags，同事件效果形状；支持收益曲线）；
    - once：整局只能成功执行一次（消耗型剧情动作，如「打开暗门」）。
    """

    id: str
    label: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    requires: dict[str, Any] | None = None
    cost: int = 0
    effects: dict[str, Any] = Field(default_factory=dict)
    once: bool = False


class ScheduleSpec(BaseModel):
    day_action_points: int = 1
    stats: dict[str, StatSpec]
    affections: dict[str, AffectionSpec]
    flags: dict[str, bool] = Field(default_factory=dict)
    actions: list[ActionSpec] = Field(default_factory=list)
    tools: list[CustomToolSpec] = Field(default_factory=list)  # 批次 C：自定义效果型工具
    counters: dict[str, CounterSpec] = Field(default_factory=dict)  # 批次 E：计数器
    items: list[ItemSpec] = Field(default_factory=list)  # 批次 E：物品


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
    steps: list[str] = Field(default_factory=list)  # agent-first 第 4 件：作者手写的
    #   顺序子步骤（1~4 条，每条 ≤40 字；空 = 由引擎侧信道生成，见 docs/design-planning.md）


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


def _collect_item_effect_refs(node: Any, gain: set[str], lose: set[str]) -> None:
    """收集效果字典里 items 效果的 gain/lose 物品 id（批次 E）。

    效果键为复数 "items"（{"gain": [...], "lose": [...]}），与条件键单数
    "item" 不重叠——两套收集互不干扰。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "items" and isinstance(v, dict):
                gain.update(v.get("gain") or [])
                lose.update(v.get("lose") or [])
            else:
                _collect_item_effect_refs(v, gain, lose)
    elif isinstance(node, list):
        for x in node:
            _collect_item_effect_refs(x, gain, lose)


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

    # 0) lore 条目校验（B1：id 唯一、keys/text 非空）
    lore_ids = [l.id for l in pack_parts["world"].lore]
    if len(lore_ids) != len(set(lore_ids)):
        raise WorldPackError(f"lore 条目 id 重复: {lore_ids}")
    for lore in pack_parts["world"].lore:
        if not lore.keys or any(not k.strip() for k in lore.keys):
            raise WorldPackError(f"lore '{lore.id}' 的 keys 不能为空且每项非空")
        if not lore.text.strip():
            raise WorldPackError(f"lore '{lore.id}' 的 text 不能为空")

    # 0.5) 地点表校验（批次 D）：id/name 唯一；声明后 scene 引用必须落在表内
    locations = pack_parts["world"].locations
    if locations:
        loc_ids = [l.id for l in locations]
        if len(loc_ids) != len(set(loc_ids)):
            raise WorldPackError(f"地点 id 重复: {loc_ids}")
        loc_names = [l.name for l in locations]
        if len(loc_names) != len(set(loc_names)):
            raise WorldPackError(f"地点显示名重复: {loc_names}")
        for loc in locations:
            if not loc.name.strip():
                raise WorldPackError(f"地点 '{loc.id}' 的 name 不能为空")
            if any(not k.strip() for k in loc.keys):
                raise WorldPackError(f"地点 '{loc.id}' 的 keys 每项必须非空")
        loc_refs = set(loc_ids) | set(loc_names)
        for node in mainline.nodes:
            scene = node.on_enter.scene
            if scene and scene not in loc_refs:
                raise WorldPackError(
                    f"主线节点 '{node.id}' 的 on_enter.scene '{scene}' 不在地点表中"
                    f"（可用 id/名称: {sorted(loc_refs)}）"
                )
        for action in schedule.actions:
            if action.scene and action.scene not in loc_refs:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 scene '{action.scene}' 不在地点表中"
                    f"（可用 id/名称: {sorted(loc_refs)}）"
                )
        start_scene = pack_parts["world"].start_scene
        if start_scene and start_scene not in loc_refs:
            raise WorldPackError(
                f"world.yaml 的 start_scene '{start_scene}' 不在地点表中"
                f"（可用 id/名称: {sorted(loc_refs)}）"
            )

    # 0.6) counters/items schema 校验（批次 E）
    for name, counter in schedule.counters.items():
        if not counter.label.strip():
            raise WorldPackError(f"计数器 '{name}' 的 label 不能为空")
        if counter.min > counter.max:
            raise WorldPackError(f"计数器 '{name}' 的 min > max")
        if not counter.min <= counter.initial <= counter.max:
            raise WorldPackError(
                f"计数器 '{name}' 的 initial {counter.initial} 在范围 "
                f"[{counter.min}, {counter.max}] 之外"
            )
    item_ids = [i.id for i in schedule.items]
    if len(item_ids) != len(set(item_ids)):
        raise WorldPackError(f"物品 id 重复: {item_ids}")
    for item in schedule.items:
        if not item.label.strip():
            raise WorldPackError(f"物品 '{item.id}' 的 label 不能为空")

    # 1) 收集全部引用 + 校验条件结构（when/completion 语法错误在加载期暴露）
    def _check_cond(cond: dict[str, Any], where: str) -> None:
        try:
            validate_condition(cond, where)
        except ConditionError as e:
            raise WorldPackError(str(e)) from e

    flag_refs: set[str] = set()
    aff_refs: set[str] = set()
    stat_refs: set[str] = set()
    counter_refs: set[str] = set()  # 批次 E：条件里引用的计数器
    item_refs: set[str] = set()  # 批次 E：条件里引用的物品
    effect_counter_refs: set[str] = set()  # 批次 E：效果里引用的计数器（事件/关键选择）
    effect_gain_items: set[str] = set()
    effect_lose_items: set[str] = set()
    for node in mainline.nodes:
        dumped = node.model_dump()
        _collect_refs(dumped, "flags", flag_refs)
        _collect_refs(dumped, "stat", stat_refs)
        _collect_refs(dumped, "counter", counter_refs)
        _collect_refs(dumped, "item", item_refs)
        for choice in node.critical_choices:  # 批次 E：选项效果的 counters/items 引用
            for opt in choice.options:
                _collect_refs(opt.effects, "counters", effect_counter_refs)
                _collect_item_effect_refs(opt.effects, effect_gain_items, effect_lose_items)
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
        _collect_refs(dumped, "counter", counter_refs)
        _collect_refs(dumped, "item", item_refs)
        _collect_refs(dumped, "counters", effect_counter_refs)  # 批次 E
        _collect_item_effect_refs(dumped, effect_gain_items, effect_lose_items)
        if ev.trigger.when is not None:
            _check_cond(ev.trigger.when, f"事件 '{ev.id}' 的 trigger.when")
    for ending in endings.endings:
        dumped = ending.model_dump()
        _collect_refs(dumped, "flags", flag_refs)
        _collect_refs(dumped, "affection", aff_refs)
        _collect_refs(dumped, "affections", aff_refs)
        _collect_refs(dumped, "stat", stat_refs)
        _collect_refs(dumped, "counter", counter_refs)
        _collect_refs(dumped, "item", item_refs)
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
    missing_counters = counter_refs - set(schedule.counters)  # 批次 E
    if missing_counters:
        raise WorldPackError(
            f"引用了未声明的计数器: {sorted(missing_counters)}——"
            f"请在 schedule.yaml 的 counters 中声明"
        )
    missing_items = item_refs - set(item_ids)  # 批次 E
    if missing_items:
        raise WorldPackError(
            f"引用了未声明的物品: {sorted(missing_items)}——请在 schedule.yaml 的 items 中声明"
        )
    # 批次 E：事件/关键选择**效果**侧的 counters/items 引用（效果键复数，与条件侧分开校验）
    bad_effect_counters = effect_counter_refs - set(schedule.counters)
    if bad_effect_counters:
        raise WorldPackError(
            f"效果引用了未声明的计数器: {sorted(bad_effect_counters)}——"
            f"请在 schedule.yaml 的 counters 中声明"
        )
    bad_effect_items = (effect_gain_items | effect_lose_items) - set(item_ids)
    if bad_effect_items:
        raise WorldPackError(
            f"效果引用了未声明的物品: {sorted(bad_effect_items)}——"
            f"请在 schedule.yaml 的 items 中声明"
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
            req_counters: set[str] = set()  # 批次 E
            req_items: set[str] = set()  # 批次 E
            _collect_refs(action.requires, "flags", req_flags)
            _collect_refs(action.requires, "stat", req_stats)
            _collect_refs(action.requires, "affection", req_affs)
            _collect_refs(action.requires, "counter", req_counters)
            _collect_refs(action.requires, "item", req_items)
            if req_flags - declared_flags:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 requires 引用了未声明的 flag: "
                    f"{sorted(req_flags - declared_flags)}"
                )
            if req_counters - set(schedule.counters):
                raise WorldPackError(
                    f"行动 '{action.id}' 的 requires 引用了未声明的计数器: "
                    f"{sorted(req_counters - set(schedule.counters))}"
                )
            if req_items - set(item_ids):
                raise WorldPackError(
                    f"行动 '{action.id}' 的 requires 引用了未声明的物品: "
                    f"{sorted(req_items - set(item_ids))}"
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
            bad_counters = set(effects.counters) - set(schedule.counters)  # 批次 E
            if bad_counters:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 {label} 引用了未声明的计数器: {sorted(bad_counters)}"
                )
            bad_items = set()  # 批次 E：gain/lose 的物品引用
            for kind, ids in effects.items.items():
                bad_items |= set(ids) - set(item_ids)
            if bad_items:
                raise WorldPackError(
                    f"行动 '{action.id}' 的 {label} 引用了未声明的物品: {sorted(bad_items)}"
                )
        bad_npcs = set(action.present) - set(npcs)
        if bad_npcs:
            raise WorldPackError(
                f"行动 '{action.id}' 的 present 引用了不存在的 NPC: {sorted(bad_npcs)}"
            )

    # 3.5) 自定义工具（批次 C）：重名 / 参数 / 门槛 / 效果引用
    tool_ids = [t.id for t in schedule.tools]
    if len(tool_ids) != len(set(tool_ids)):
        raise WorldPackError(f"自定义工具 id 重复: {tool_ids}")
    for tool in schedule.tools:
        if tool.id in ENGINE_TOOL_NAMES:
            raise WorldPackError(f"自定义工具 '{tool.id}' 与引擎内置工具重名")
        if not tool.description.strip():
            raise WorldPackError(f"自定义工具 '{tool.id}' 的 description 不能为空")
        if tool.cost < 0:
            raise WorldPackError(f"自定义工具 '{tool.id}' 的 cost 不能为负")
        missing_params = set(tool.required) - set(tool.parameters)
        if missing_params:
            raise WorldPackError(
                f"自定义工具 '{tool.id}' 的 required 引用了未声明的参数: "
                f"{sorted(missing_params)}"
            )
        if tool.requires is not None:
            try:
                validate_condition(tool.requires, f"自定义工具 '{tool.id}' 的 requires")
            except ConditionError as e:
                raise WorldPackError(str(e)) from e
            req_flags, req_stats, req_affs = set(), set(), set()
            req_counters: set[str] = set()  # 批次 E
            req_items: set[str] = set()  # 批次 E
            _collect_refs(tool.requires, "flags", req_flags)
            _collect_refs(tool.requires, "stat", req_stats)
            _collect_refs(tool.requires, "affection", req_affs)
            _collect_refs(tool.requires, "counter", req_counters)
            _collect_refs(tool.requires, "item", req_items)
            if req_flags - declared_flags:
                raise WorldPackError(
                    f"自定义工具 '{tool.id}' 的 requires 引用了未声明的 flag: "
                    f"{sorted(req_flags - declared_flags)}"
                )
            if req_counters - set(schedule.counters):
                raise WorldPackError(
                    f"自定义工具 '{tool.id}' 的 requires 引用了未声明的计数器: "
                    f"{sorted(req_counters - set(schedule.counters))}"
                )
            if req_items - set(item_ids):
                raise WorldPackError(
                    f"自定义工具 '{tool.id}' 的 requires 引用了未声明的物品: "
                    f"{sorted(req_items - set(item_ids))}"
                )
            if req_stats - declared_stats:
                raise WorldPackError(
                    f"自定义工具 '{tool.id}' 的 requires 引用了未声明的属性: "
                    f"{sorted(req_stats - declared_stats)}"
                )
            if req_affs - declared_affections:
                raise WorldPackError(
                    f"自定义工具 '{tool.id}' 的 requires 引用了未声明的好感对象: "
                    f"{sorted(req_affs - declared_affections)}"
                )
        for key in ("stats", "affections"):
            for name, value in (tool.effects.get(key) or {}).items():
                if isinstance(value, dict):
                    try:
                        curve = EffectSpec(**value)
                    except Exception as e:  # noqa: BLE001
                        raise WorldPackError(
                            f"自定义工具 '{tool.id}' 的 effects.{key}.{name} "
                            f"不是合法的收益曲线: {e}"
                        ) from e
                    for field_name in ("spread", "decay_every", "decay_step"):
                        if getattr(curve, field_name) < 0:
                            raise WorldPackError(
                                f"自定义工具 '{tool.id}' 的 effects.{key}.{name} 的 "
                                f"'{field_name}' 不能为负"
                            )
        bad_tool_stats = set(tool.effects.get("stats") or {}) - declared_stats
        if bad_tool_stats:
            raise WorldPackError(
                f"自定义工具 '{tool.id}' 的 effects 引用了未声明的属性: {sorted(bad_tool_stats)}"
            )
        bad_tool_affs = set(tool.effects.get("affections") or {}) - declared_affections
        if bad_tool_affs:
            raise WorldPackError(
                f"自定义工具 '{tool.id}' 的 effects 引用了未声明的好感对象: "
                f"{sorted(bad_tool_affs)}"
            )
        bad_tool_flags = set(tool.effects.get("flags") or {}) - declared_flags
        if bad_tool_flags:
            raise WorldPackError(
                f"自定义工具 '{tool.id}' 的 effects 引用了未声明的 flag: "
                f"{sorted(bad_tool_flags)}"
            )
        bad_tool_counters = set(tool.effects.get("counters") or {}) - set(schedule.counters)
        if bad_tool_counters:
            raise WorldPackError(
                f"自定义工具 '{tool.id}' 的 effects 引用了未声明的计数器: "
                f"{sorted(bad_tool_counters)}"
            )
        for cname, cval in (tool.effects.get("counters") or {}).items():  # 批次 E：值必须可加
            if isinstance(cval, bool) or not isinstance(cval, (int, float)):
                raise WorldPackError(
                    f"自定义工具 '{tool.id}' 的 effects.counters.{cname} 必须是数字，"
                    f"当前为 {cval!r}"
                )
        tool_item_refs: set[str] = set()
        tool_items = tool.effects.get("items") or {}
        tool_item_refs |= set(tool_items.get("gain") or []) | set(tool_items.get("lose") or [])
        if tool_item_refs - set(item_ids):
            raise WorldPackError(
                f"自定义工具 '{tool.id}' 的 effects 引用了未声明的物品: "
                f"{sorted(tool_item_refs - set(item_ids))}"
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
    for tool in schedule.tools:  # 批次 C：自定义工具效果也是合法的 flag 写入路径
        _collect_refs(tool.effects, "flags", writable_flags)

    # 5.5) 批次 E：counters/items 的可达性（复用 flag 可达性同一模式）
    def _collect_item_conditions(node: Any, out: dict[str, bool]) -> None:
        """收集条件字典里 item 条件的 (物品id → want)。"""
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "item" and isinstance(v, dict):
                    for iid, want in v.items():
                        out[iid] = bool(want)
                else:
                    _collect_item_conditions(v, out)
        elif isinstance(node, list):
            for x in node:
                _collect_item_conditions(x, out)

    writable_counters: set[str] = set()
    gainable_items: set[str] = set()
    losable_items: set[str] = set()
    for node in mainline.nodes:
        for choice in node.critical_choices:
            for opt in choice.options:
                _collect_refs(opt.effects, "counters", writable_counters)
                _collect_item_effect_refs(opt.effects, gainable_items, losable_items)
    for ev in events.events:
        _collect_refs(ev.effects, "counters", writable_counters)
        _collect_item_effect_refs(ev.effects, gainable_items, losable_items)
    for tool in schedule.tools:
        _collect_refs(tool.effects, "counters", writable_counters)
        _collect_item_effect_refs(tool.effects, gainable_items, losable_items)
    for action in schedule.actions:
        for effects in (
            action.effects,
            action.critical_effects,
            action.failure_effects,
        ):
            if effects is None:
                continue
            writable_counters |= set(effects.counters)
            gainable_items |= set(effects.items.get("gain") or [])
            losable_items |= set(effects.items.get("lose") or [])

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
        # 批次 E：completion 引用的计数器必须有增减路径；物品必须可得/可失
        completion_counters: set[str] = set()
        _collect_refs(node.completion, "counter", completion_counters)
        unreachable_counters = completion_counters - writable_counters
        if unreachable_counters:
            raise WorldPackError(
                f"主线节点 '{node.id}' 的 completion 要求计数器 {sorted(unreachable_counters)}，"
                f"但没有任何代码路径能增减它们——该节点将永远无法完成"
            )
        completion_items: dict[str, bool] = {}
        _collect_item_conditions(node.completion, completion_items)
        for iid, want in completion_items.items():
            if want and iid not in gainable_items:
                raise WorldPackError(
                    f"主线节点 '{node.id}' 的 completion 要求持有物品 '{iid}'，"
                    f"但没有任何代码路径能获得它——该节点将永远无法完成"
                )
            if not want and iid not in losable_items:
                raise WorldPackError(
                    f"主线节点 '{node.id}' 的 completion 要求失去物品 '{iid}'，"
                    f"但没有任何代码路径能失去它——该节点将永远无法完成"
                )
        # agent-first 第 4 件：作者手写子步骤的格式校验（1~4 条、每条 ≤40 字）
        if not 0 <= len(node.steps) <= 4:
            raise WorldPackError(
                f"主线节点 '{node.id}' 的 steps 必须在 0~4 条之间（当前 {len(node.steps)} 条）"
            )
        for i, step in enumerate(node.steps, start=1):
            if not step.strip():
                raise WorldPackError(f"主线节点 '{node.id}' 的 steps 第 {i} 条为空")
            if len(step) > 40:
                raise WorldPackError(
                    f"主线节点 '{node.id}' 的 steps 第 {i} 条超过 40 字（{len(step)} 字）"
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

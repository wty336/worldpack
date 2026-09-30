"""世界包 schema 与加载器（W2）。

世界包 = 一个文件夹内的纯 YAML 内容（见 docs/design.md §12）：
  world.yaml / schedule.yaml / mainline.yaml / events.yaml / endings.yaml / npcs/*.yaml

加载器做两类校验：
  1. pydantic schema 校验（字段缺失/类型错误直接拒绝——约束编码化）；
  2. 跨文件交叉校验（引用了未声明的 flag/好感/NPC/行动 → 拒绝）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from .conditions import ConditionError, validate_condition
from .evalmeta import DIGEST_LEN, normalized_bytes
from .lore import LORE_LOGIC_MODES, LoreKeyError, validate_key


class WorldPackError(Exception):
    """世界包加载/校验错误（面向世界包作者的友好报错）。"""


# 引擎内置工具名（批次 C）：自定义工具不得与之重名（registry 与交叉校验共用此清单）
ENGINE_TOOL_NAMES = (
    "change_stat", "submit_narration", "remember", "query_world", "do_action",
    "make_appointment",  # 约定真值（玩家实测缺陷修复）
    "change_scene",  # 校验批：地点表声明时 registry 会注册它，此前漏在本表外
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
    # A-4：递归扫描层数（0 = 关闭，默认）。命中条目的正文可作为下一层扫描输入，
    # 用于表达「提到 A 才需要知道 B」的二级知识。默认 0 而非无限——与旧行为一致。
    max_recursion: int = 0
    locations: list["LocationSpec"] = Field(default_factory=list)  # 批次 D：地点表（声明后 scene 受校验）


class LoreSpec(BaseModel):
    """B1（P3）：一条 Lorebook 条目（地点/势力/物品/传闻）。

    keys：触发关键词（命中任一即候选）；text：注入文本。lore 不进静态前缀，
    由上下文组装器按「场景 + 节点目标 + 近对话」动态注入（预算内）。

    A-2（对照 SillyTavern `selectiveLogic`）：
    - secondary_keys：次级关键词。**不声明则退化为纯主键命中**；
    - logic：主键命中后如何用次键收窄，四值（见 LORE_LOGIC_MODES）。
      **声明了 secondary_keys 就必须显式给 logic**——否则默认值会把某一种
      语义静默强加给作者（加载期拒绝，见 worldpack `_cross_check`）。
      `NOT_ANY` 是 lorebook 里最有表达力的一档：命中 A **但没提到** B 才注入。
    """

    id: str
    keys: list[str]
    text: str
    secondary_keys: list[str] = Field(default_factory=list)
    logic: str = "AND_ANY"
    # A-3：大小写敏感（默认 True——中文场景下更安全；仅在匹配英文/拼音变体时关闭）
    case_sensitive: bool = True
    # A-1：预算豁免（引擎强制接管的设定）。与 A-4 的 `constant` 是**两个独立维度**：
    # `ignore_budget` 管"是否占预算额度"，`constant` 管"是否需要关键词命中"。
    ignore_budget: bool = False
    # A-4：常驻注入——跳过关键词判定、总是候选（世界通则类设定：货币/历法/忌讳）。
    # **仍受预算约束**（常驻 ≠ 无限）。
    constant: bool = False
    # A-4：递归闸——本条目**不被**递归层激活（只能靠直接关键词命中）。
    exclude_from_recursion: bool = False
    # A-4：本条目正文**不作为**下一层扫描输入（不传染）。
    no_recursion_trigger: bool = False


# A-2：次级关键词四值逻辑语义见 `game_agent/lore.py`（此处直接复用其常量）

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
    # 日程行动写 flag（手册 §3.2/§5 守则 3 声明的合法写入路径之一：关键选择选项 /
    # 事件 / 日程行动）。此前缺该字段 → pydantic 默认 extra="ignore" 静默丢弃，
    # 校验器的 writable_flags 收集（effects.model_dump()）因此对行动永远为空：
    # 作者按手册写"行动效果写 flag"会得到静默 no-op（2026-09-25 由 campus_otome 包发现）。
    flags: dict[str, bool] = Field(default_factory=dict)


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
# 包名（它同时是目录名）
# ---------------------------------------------------------------------------

NAME_PATTERN = re.compile(r"[A-Za-z0-9_\-]+")
"""包目录名的白名单。

放在 `worldpack` 而不是 `worldgen`：**目录层（catalog）、生成管线（worldgen）与 CLI
都要用同一把尺子**，而 `worldpack` 是它们共同的下游依赖。尺子放在其中任何一个上层
模块里，都会逼另一个去 import 一个它本不需要的重模块（`catalog` 为了一个正则去拉
`llm`/`judge_corpus` 是荒谬的）。
"""


def validate_name(name: str) -> str | None:
    """包名合法性（它同时是目录名）；不合法返回原因，合法返回 None。"""
    if not NAME_PATTERN.fullmatch(name or ""):
        return f"非法包名（仅允许字母/数字/_/-）: {name!r}"
    return None


# ---------------------------------------------------------------------------
# 剧本身份戳（G2）：存档必须记住"这一局玩的是哪份内容"
# ---------------------------------------------------------------------------

PACK_CONTENT_FILES = (
    "world.yaml",
    "schedule.yaml",
    "mainline.yaml",
    "events.yaml",
    "endings.yaml",
)  # npcs/*.yaml 另按目录枚举
"""参与 `pack_digest` 的**玩法内容**文件。

为什么**不**把 `judge_corpus*.yaml` 算进去：那是门禁语料，不属于玩法。作者重跑
`build_judge_corpus.py` 会让它变化；若参与摘要，旧存档会被判成"剧本已改"而误拒——
把"质量门的尺子"当成"游戏规则"是口径混淆。同理 `smoke_profile.yaml` 也不参与。
摘要只回答一个问题：**这一局玩的规则有没有变。**
"""


def pack_digest(root: str | Path) -> str:
    """世界包玩法内容的**内容摘要**（换行归一化，跨机可对账）。

    口径与 `evalmeta.file_digest` 完全一致（共用 `normalized_bytes`）：读字节 →
    换行归一 → sha256。逐文件喂入「相对路径 + NUL + 内容 + NUL」，于是
    **改内容会变、改名会变、与枚举顺序无关**。

    为什么必须换行归一：同一份包在 Windows 检出是 CRLF、Linux 是 LF。
    不做归一，跨机/跨 CI 的存档会被判成"剧本不匹配"——正是 `file_digest`
    在 2026-09-12 踩过的那个坑（当时让 `test_eval_frozen` 在 Linux 上必红）。

    缺失的文件直接跳过：`load_worldpack` 已经负责报"必需文件缺失"，
    本函数不重复报错，只做摘要。
    """
    base = Path(root)
    rels: list[Path] = [Path(n) for n in PACK_CONTENT_FILES if (base / n).is_file()]
    npcs_dir = base / "npcs"
    if npcs_dir.is_dir():
        rels += [p.relative_to(base) for p in npcs_dir.glob("*.yaml")]
    h = hashlib.sha256()
    for rel in sorted(rels, key=lambda p: p.as_posix()):
        h.update(rel.as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(normalized_bytes(base / rel))
        h.update(b"\0")
    return h.hexdigest()[:DIGEST_LEN]


def pack_meta(pack: WorldPack) -> dict[str, str]:
    """存档用的剧本身份戳：`id`（目录名，人可读）+ `digest`（内容指纹，可对账）。

    两个字段各司其职：`id` 让人一眼看出"这是哪个包"，`digest` 才能发现
    **同名但改过版**的情况（改 NPC id、改旗标名——正是旧存档静默穿帮的成因）。
    """
    return {"id": pack.root.name, "digest": pack_digest(pack.root)}


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
    world: WorldSpec = pack_parts["world"]

    declared_flags = set(schedule.flags)
    declared_affections = set(schedule.affections)
    declared_stats = set(schedule.stats)
    action_ids = {a.id for a in schedule.actions}

    # 0) lore 条目校验（B1：id 唯一、keys/text 非空；A-2：次键与 logic；A-3：正则键）
    if pack_parts["world"].max_recursion < 0:
        raise WorldPackError(
            f"max_recursion 必须 >= 0（当前 {pack_parts['world'].max_recursion}）；0 = 关闭递归"
        )
    lore_ids = [l.id for l in pack_parts["world"].lore]
    if len(lore_ids) != len(set(lore_ids)):
        raise WorldPackError(f"lore 条目 id 重复: {lore_ids}")
    for lore in pack_parts["world"].lore:
        try:
            if not lore.keys:
                raise LoreKeyError("keys 不能为空")
            for k in lore.keys:
                validate_key(k, kind="keys", owner=f"lore '{lore.id}'",
                             case_sensitive=lore.case_sensitive)
            if any(not k.strip() for k in lore.secondary_keys):
                raise LoreKeyError("secondary_keys 每项必须非空")
            for k in lore.secondary_keys:
                validate_key(k, kind="secondary_keys", owner=f"lore '{lore.id}'",
                             case_sensitive=lore.case_sensitive)
        except LoreKeyError as exc:
            raise WorldPackError(f"lore '{lore.id}': {exc}") from exc
        if not lore.text.strip():
            raise WorldPackError(f"lore '{lore.id}' 的 text 不能为空")
        if lore.secondary_keys and "logic" not in lore.model_fields_set:
            raise WorldPackError(
                f"lore '{lore.id}' 声明了 secondary_keys 就必须显式声明 logic"
                f"（可选：{'/'.join(LORE_LOGIC_MODES)}）——默认值不得静默替你决定语义"
            )
        if "logic" in lore.model_fields_set and lore.logic not in LORE_LOGIC_MODES:
            raise WorldPackError(
                f"lore '{lore.id}' 的 logic 非法: {lore.logic!r}"
                f"（可选：{'/'.join(LORE_LOGIC_MODES)}）"
            )

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

    # 0.7) 数值范围约束（校验批）：initial 越界会让首次收益"饱和"回边界
    #      （实测：initial=5000 + effect +1 → 实际 -4900），min>max 同理不可玩。
    for name, stat in schedule.stats.items():
        if stat.min > stat.max:
            raise WorldPackError(f"属性 '{name}' 的 min > max")
        if not stat.min <= stat.initial <= stat.max:
            raise WorldPackError(
                f"属性 '{name}' 的 initial {stat.initial:g} 在范围 "
                f"[{stat.min:g}, {stat.max:g}] 之外"
            )
    for name, aff in schedule.affections.items():
        if aff.min > aff.max:
            raise WorldPackError(f"好感对象 '{name}' 的 min > max")
        if not aff.min <= aff.initial <= aff.max:
            raise WorldPackError(
                f"好感对象 '{name}' 的 initial {aff.initial:g} 在范围 "
                f"[{aff.min:g}, {aff.max:g}] 之外"
            )

    # 0.8) memory_limit 下界（校验批）：0 会让该 NPC 的记忆"写一条淘汰一条"却回报
    #      已写入 —— 功能静默全灭，无任何报错。
    for npc_id, npc in npcs.items():
        if npc.memory_limit < 1:
            raise WorldPackError(
                f"角色卡 '{npc_id}' 的 memory_limit 必须 ≥1，当前为 {npc.memory_limit}"
                f"（0 会让该 NPC 的记忆每次写入后立即被淘汰，功能静默失效）"
            )

    # 0.9) 禁用表有效性（校验批）：`_forbidden_tokens` 只从「（括号）内示例词 + 、顿号
    #      分隔项」里取 2~6 字短词。一条整句规则（无分隔符且 >6 字）会切出 0 个 token
    #      → 世界观边界防线**静默失效**（模型照写不误，过滤不拦）。宁可加载期报错，
    #      也不要一条看起来生效、实际不存在的规则。
    from .storyline import _forbidden_tokens  # 局部导入避免模块级环

    dead_rules = [
        entry
        for entry in world.forbidden
        if not _forbidden_tokens(WorldSpec(name="probe", era="probe", forbidden=[entry]))
    ]
    if dead_rules:
        raise WorldPackError(
            "以下禁用规则切不出任何可匹配词，这些防线会静默失效：\n"
            + "\n".join(f"  - {e!r}" for e in dead_rules)
            + "\n  修法：把具体禁用词放进括号或用顿号分隔，例如"
            "『现代事物（手机、微信、汽车等）』；确需概括性表述时，"
            "请把概括词压缩到 6 字以内（如『现代品牌』）。"
        )

    # 0.10) 实体 id 唯一性（校验批）：此前只校验 lore/地点/物品/自定义工具/角色卡，
    #       节点/抉择/行动/事件/结局五类漏网。重复 id 不会崩，但会**语义错位**：
    #       节点重复 → 第二个永久不可达；行动重复 → 点 B 执行 A；抉择重复 →
    #       pending_choice 取到另一个；事件/结局重复 → 第二个永不触发。
    node_ids = [n.id for n in mainline.nodes]
    if len(node_ids) != len(set(node_ids)):
        dup = sorted({i for i in node_ids if node_ids.count(i) > 1})
        raise WorldPackError(
            f"主线节点 id 重复: {dup}——重复会让后一个节点永久不可达（作者拿到绿灯却丢内容）"
        )
    for node in mainline.nodes:
        choice_ids = [c.id for c in node.critical_choices]
        if len(choice_ids) != len(set(choice_ids)):
            raise WorldPackError(
                f"主线节点 '{node.id}' 内关键抉择 id 重复: "
                f"{sorted({i for i in choice_ids if choice_ids.count(i) > 1})}"
            )
        for choice in node.critical_choices:
            if not choice.options:
                raise WorldPackError(
                    f"关键抉择 '{choice.id}'（节点 '{node.id}'）的 options 为空——"
                    "引擎会进入「有抉择但无可选项」的死锁态：玩家无法选择、"
                    "say/act/end_day 全被抉择守卫拦下，CLI 的选项提问会无限循环。"
                )
    action_ids = [a.id for a in schedule.actions]
    if len(action_ids) != len(set(action_ids)):
        raise WorldPackError(
            f"日程行动 id 重复: {sorted({i for i in action_ids if action_ids.count(i) > 1})}"
            "——按 id 派发会执行到另一个同名行动"
        )
    event_ids = [e.id for e in events.events]
    if len(event_ids) != len(set(event_ids)):
        raise WorldPackError(
            f"事件 id 重复: {sorted({i for i in event_ids if event_ids.count(i) > 1})}"
            "——triggered_events 按 id 去重，后一个永不触发"
        )
    ending_ids = [e.id for e in endings.endings]
    if len(ending_ids) != len(set(ending_ids)):
        raise WorldPackError(
            f"结局 id 重复: {sorted({i for i in ending_ids if ending_ids.count(i) > 1})}"
        )

    # 0.11) 好感阶段连续性（校验批）：好感是 float 且可由 0.5 增量驱动，而作者习惯写
    #       整数区间 [0,20] [21,50]……。若两段之间留出真空隙，落在空隙里的好感值会让
    #       语气退化成字面量「（无阶段定义）」——恰好出现在好感最高的剧情高潮段落。
    #       规则：升序不重叠、相邻间隙 ≤1（容纳整数区间写法）、整体覆盖 [min, max]。
    for npc_id, npc in npcs.items():
        spec = schedule.affections.get(npc_id)
        lo_bound = spec.min if spec is not None else 0.0
        hi_bound = spec.max if spec is not None else 100.0
        stages = sorted((float(s.range[0]), float(s.range[1])) for s in npc.affection_stages)
        if not stages:
            continue  # 无阶段 = 语气恒为「（无阶段定义）」，由作者自行决定是否写
        if stages[0][0] > lo_bound:
            raise WorldPackError(
                f"角色卡 '{npc_id}' 的好感阶段未覆盖下界："
                f"[{lo_bound:g}, {stages[0][0]:g}) 无阶段定义"
            )
        for i in range(len(stages) - 1):
            if stages[i][1] >= stages[i + 1][0]:
                raise WorldPackError(
                    f"角色卡 '{npc_id}' 的好感阶段重叠: {stages[i]} 与 {stages[i + 1]}"
                )
            if stages[i + 1][0] - stages[i][1] > 1.0:
                raise WorldPackError(
                    f"角色卡 '{npc_id}' 的好感阶段留有空隙: "
                    f"({stages[i][1]:g}, {stages[i + 1][0]:g}) 内的好感值语气会退化为"
                    "「（无阶段定义）」"
                )
        if stages[-1][1] < hi_bound:
            raise WorldPackError(
                f"角色卡 '{npc_id}' 的好感阶段未覆盖上界："
                f"({stages[-1][1]:g}, {hi_bound:g}] 无阶段定义"
            )


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
        # C1（校验批，Critical）：关键抉择的**选项效果**用复数键 `stats`/`affections`
        # 声明（ActionEffects 形状），而上面两行只收条件侧的单数键 `stat`/`affection`。
        # 漏收的后果：`options[].effects.stats: {ghost_stat: 5}` 通过加载，
        # 玩家点下该选项时抛未捕获的 StatChangeError（CLI 崩溃存档退出），
        # 且排在坏键之前的效果已经落盘。事件路径（下方 :547）本来就收复数键，此处补齐。
        _collect_refs(dumped, "stats", stat_refs)
        _collect_refs(dumped, "affections", aff_refs)
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

    # 6) 数值可达性（校验批）：结局条件里的**数值阈值**必须在行动点预算内够得着。
    #    此前只校验 flag/counter/item 的写入路径（步骤 5/5.5），数值阈值完全不管——
    #    实测 8 个包里有 4 个存在"永远无法达成的结局"（要求好感 ≥50，上限仅 5~25），
    #    作者拿到绿灯、玩家却永远刷不出该结局。判据只取**保守上界**：
    #    "上限 < 阈值"才算不可达（不误杀有随机/门槛/检定的合法剧本）。
    #    预算天数 = 全部结局 day 门槛的最大值（没有 day 门槛则无法界定预算，跳过）。
    _day_hits: list[int] = []

    def _collect_day_thresholds(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "day" and isinstance(v, dict) and "gte" in v:
                    _day_hits.append(int(v["gte"]))
                else:
                    _collect_day_thresholds(v)
        elif isinstance(node, list):
            for item in node:
                _collect_day_thresholds(item)

    for ending in endings.endings:
        _collect_day_thresholds(ending.when)
    if _day_hits:
        stat_caps, aff_caps = _numeric_caps(schedule, max(_day_hits))
        # 收集**全部**不可达项后一次性报出（与 0.9 禁用表同一策略）：作者拿到的
        # 是一张待修清单，而不是修一个跑一次、下次再冒一个。
        unreachable: list[str] = []
        for ending in endings.endings:
            needs_stats: dict[str, list[tuple[str, float]]] = {}
            needs_affs: dict[str, list[tuple[str, float]]] = {}
            _numeric_conditions(ending.when, "stat", needs_stats)
            _numeric_conditions(ending.when, "affection", needs_affs)
            for label, needs, caps in (
                ("属性", needs_stats, stat_caps),
                ("好感", needs_affs, aff_caps),
            ):
                for name, conditions in needs.items():
                    if name not in caps:
                        continue  # 未声明的引用已由步骤 2 报错，此处不重复
                    for op, threshold in conditions:
                        if op not in ("gte", "gt"):
                            continue  # lte/lt 是上限约束，不构成"够不着"
                        cap = caps[name]
                        if cap + 1e-9 < threshold:
                            display = (
                                schedule.stats[name].label
                                if label == "属性" and name in schedule.stats
                                else schedule.affections[name].label
                                if name in schedule.affections
                                else name
                            )
                            unreachable.append(
                                f"  结局 '{ending.id}'（{ending.title}）要求{label}"
                                f"『{display}』{op} {threshold:g}，"
                                f"但全投上限仅 {cap:.1f}（差 {threshold - cap:.1f}）"
                                f" → 把阈值改为 ≤{cap:.0f}，或给 '{name}' 增加收益路径"
                            )
        if unreachable:
            raise WorldPackError(
                f"以下结局的数值阈值在行动点预算内永远够不着"
                f"（预算 = {max(_day_hits)} 天 × {schedule.day_action_points} 点）：\n"
                + "\n".join(unreachable)
                + "\n  说明：判据取**保守上界**（只算成功档、忽略门槛未解锁），"
                "所以这里的每一条都是「确定不可达」，不是估算误差。"
            )

    # 6.5) 数值**零可重复增益路径**（N12）：不需要预算也能判死的那一类。
    #
    # 上面步骤 6 被 `if _day_hits:` 包着——**只有某个结局带 day 门槛时才跑**。
    # 而生成器写出来的结局常常没有 day 门槛（`when: {all: [{affection: {lin: {gte: 50}}}]}`），
    # 于是那道门禁**整体跳过**：真机实测就是"离线生成的卡，好感只能到 8，结局要 50，
    # 门禁放行，玩家永远打不出这个结局"（docs/roadmap.md §2.5 ③）。
    #
    # 判据（不需要天数预算）：
    #   若某维度**没有任何可重复的增益来源**（日程行动 / 自定义工具都不涨它），
    #   那么它的上界就是「初始值 + 一次性来源加满」（关键抉择每个只选一个、事件累加）。
    #   这个上界 < 阈值 → **机制上够不着**。
    #
    # **为什么必须区分"可重复"与"一次性"**：关键抉择的 +3 是确定性来源，但用一次就没了。
    # 只看"有没有来源"会让判据形同虚设——生成的那张卡正是这样漏过去的（5 → 8，要 50）。
    #
    # **口径要诚实**：这里判的是"**机制上够不着**"，不是"绝对不可达"——叙事者仍可逐轮用
    # `change_stat` 调整（每次 ≤±5，见 `stats.AFFECTION_DELTA_MAX`），但那是模型的自由裁量，
    # 不是"玩家照着玩法走就能到"。结局阈值应当由机制兜住，所以这里按错误处理。
    _rep_stats, _rep_affs = _repeatable_gain_paths(schedule)
    _once_stats, _once_affs = _one_shot_gains(mainline, events)
    no_path: list[str] = []
    for ending in endings.endings:
        needs_stats: dict[str, list[tuple[str, float]]] = {}
        needs_affs: dict[str, list[tuple[str, float]]] = {}
        _hard_numeric_conditions(ending.when, "stat", needs_stats)
        _hard_numeric_conditions(ending.when, "affection", needs_affs)
        for label, table, repeatable, once in (
            ("属性", schedule.stats, _rep_stats, _once_stats),
            ("好感", schedule.affections, _rep_affs, _once_affs),
        ):
            for name, conds in (
                (needs_stats if label == "属性" else needs_affs)
            ).items():
                if name not in table or name in repeatable:
                    continue  # 未声明引用由步骤 2 报；有重复来源 → 天数够就能到
                initial = float(table[name].initial)
                cap = initial + float(once.get(name, 0.0))
                for op, threshold in conds:
                    if op not in ("gte", "gt"):
                        continue
                    if (initial > threshold) if op == "gt" else (initial >= threshold):
                        continue  # 开局就满足，不构成"够不着"
                    if cap + 1e-9 < threshold:
                        no_path.append(
                            f"  结局 '{ending.id}'（{ending.title}）要求{label}『{name}』"
                            f"{op} {threshold:g}，但**没有任何可重复的增益路径**：\n"
                            f"    · 日程行动与自定义工具都不提升它（初始 {initial:g}）\n"
                            f"    · 一次性来源（关键抉择/事件）全部加满也只到 {cap:g}\n"
                            f"    改法二选一：① 给某个 action / 自定义工具加 '{name}' 的增益"
                            f"（推荐——玩家就有明确的推进路径了）；② 把阈值降到 ≤{cap:g}"
                        )
    if no_path:
        raise WorldPackError(
            "以下结局的数值阈值**机制上够不着**（没有可重复的增益路径）：\n"
            + "\n".join(no_path)
            + "\n  说明：这里判的不是「绝对不可达」——叙事者仍可逐轮用 change_stat 调整"
            "（每次 ≤±5），但那是模型的自由裁量，不是「玩家照着玩法走就能到」。"
            "结局阈值应当由机制兜住。"
        )


def _gain_of(value: Any) -> float:
    """一个效果值的**名义正增益**（非正/看不懂 → 0）。

    两种形状都要认：`EffectValue` 可能是 `{base, spread, decay_*}` 也可能是裸数字
    （`_numeric_caps` 里同一处置）。`decay_*` 递减曲线这里**刻意忽略**——
    本函数只回答"这个来源**能不能**涨它"，判据保守（少算 → 少报"够不着"）。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) > 0 else 0.0
    if isinstance(value, dict):
        base = value.get("base")
        spread = value.get("spread")
        total = 0.0
        if isinstance(base, (int, float)):
            total += float(base)
        if isinstance(spread, (int, float)) and float(spread) > 0:
            total += float(spread)  # 取上界：判"能不能涨"，不是算期望
        return total if total > 0 else 0.0
    base = getattr(value, "base", None)
    spread = getattr(value, "spread", None)
    total = float(base or 0)
    if isinstance(spread, (int, float)) and float(spread) > 0:
        total += float(spread)
    return total if total > 0 else 0.0


def _repeatable_gain_paths(schedule: ScheduleSpec) -> tuple[set[str], set[str]]:
    """**可重复**的增益来源（花行动点就能再做一次）：日程行动 + 自定义工具。

    特意**不含**关键抉择与事件：那些一次性。两者的区分是 N12 判据的核心——
    只看"有没有来源"会让判据形同虚设（生成的那张卡唯一的好感来源是关键抉择 +3）。
    """
    stats: set[str] = set()
    affs: set[str] = set()
    for action in schedule.actions:
        for eff in (action.effects, action.critical_effects, action.failure_effects):
            if eff is None:
                continue
            stats |= {k for k, v in eff.stats.items() if _gain_of(v) > 0}
            affs |= {k for k, v in eff.affections.items() if _gain_of(v) > 0}
    for tool in schedule.tools:
        stats |= {k for k, v in (tool.effects.get("stats") or {}).items() if _gain_of(v) > 0}
        affs |= {k for k, v in (tool.effects.get("affections") or {}).items() if _gain_of(v) > 0}
    return stats, affs


def _one_shot_gains(
    mainline: MainlineSpec, events: EventsSpec
) -> tuple[dict[str, float], dict[str, float]]:
    """**一次性**来源加满时的总增益：关键抉择每个只选一个（取最大），事件累加。"""
    stats: dict[str, float] = {}
    affs: dict[str, float] = {}
    for node in mainline.nodes:
        for choice in node.critical_choices:
            best_stats: dict[str, float] = {}
            best_affs: dict[str, float] = {}
            for opt in choice.options:  # 一个抉择只能选一个 → 逐维度取最大
                for k, v in (opt.effects.get("stats") or {}).items():
                    best_stats[k] = max(best_stats.get(k, 0.0), _gain_of(v))
                for k, v in (opt.effects.get("affections") or {}).items():
                    best_affs[k] = max(best_affs.get(k, 0.0), _gain_of(v))
            for k, v in best_stats.items():
                stats[k] = stats.get(k, 0.0) + v
            for k, v in best_affs.items():
                affs[k] = affs.get(k, 0.0) + v
    for ev in events.events:  # 事件各自触发 → 累加
        for k, v in (ev.effects.get("stats") or {}).items():
            stats[k] = stats.get(k, 0.0) + _gain_of(v)
        for k, v in (ev.effects.get("affections") or {}).items():
            affs[k] = affs.get(k, 0.0) + _gain_of(v)
    return stats, affs


def _hard_numeric_conditions(
    node: Any, kind: str, out: dict[str, list[tuple[str, float]]]
) -> None:
    """只收集**无条件**路径上的数值要求：走 `all` / 根 / 列表，**跳过 `any` 与 `not`**。

    为什么不能直接用 `_numeric_conditions`：那个为了判"门槛够不够得着"把所有分支
    （含 `any`）都当要求收——对**要求**是保守的，但对"够不着"**会误报**：
    `any: [affection ≥50, day ≥3]` 里好感那一支够不着并不妨碍结局达成。
    本判据只对"无论满足哪个分支都必须成立"的条件下结论。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("not", "any"):
                continue
            if key == kind and isinstance(value, dict):
                for name, spec in value.items():
                    if isinstance(spec, dict):
                        for op, threshold in spec.items():
                            if op in ("gte", "gt", "lte", "lt") and isinstance(
                                threshold, (int, float)
                            ):
                                out.setdefault(name, []).append((op, float(threshold)))
            else:
                _hard_numeric_conditions(value, kind, out)
    elif isinstance(node, list):
        for item in node:
            _hard_numeric_conditions(item, kind, out)


def _numeric_caps(schedule: ScheduleSpec, days: int) -> tuple[dict[str, float], dict[str, float]]:
    """数值可达上限（校验批步骤 6 的核心）：该维度**独占全部行动点**时的理论上界。

    刻意取上界而非精确预测——门禁只回答"**有没有可能**够得着"：
    - 只取成功档（忽略检定失败/大成功、忽略 requires 门槛未解锁）→ 结果偏乐观，
      所以"上限 < 阈值 → 确定不可达"这个判据不会误杀合法剧本；
    - 收益曲线按 `decay_every/decay_step` 逐次递减累加（与运行时同口径）；
    - spread 对称，取期望值（不加不减）。

    返回 (属性上限, 好感上限)。
    """
    budget = max(0, days) * max(0, schedule.day_action_points)

    def cap_for(kind: str, key: str) -> float:
        table = schedule.stats if kind == "stats" else schedule.affections
        spec = table[key]
        cur = float(spec.initial)
        top = float(spec.max)
        for _ in range(budget):
            best = 0.0
            for action in schedule.actions:
                eff = action.effects
                src = eff.stats if kind == "stats" else eff.affections
                if key not in src:
                    continue
                value = src[key]
                if isinstance(value, EffectSpec):
                    gain = value.base
                    if value.decay_every and value.decay_every > 0:
                        gain = max(0.0, gain - int(cur // value.decay_every) * value.decay_step)
                else:
                    gain = float(value)
                best = max(best, gain)
            if best <= 0:
                break
            cur = min(top, cur + best)
        return cur

    return (
        {k: cap_for("stats", k) for k in schedule.stats},
        {k: cap_for("affections", k) for k in schedule.affections},
    )


def _numeric_conditions(node: Any, kind: str, out: dict[str, list[tuple[str, float]]]) -> None:
    """收集条件里的数值要求 → {名字: [(op, 阈值)]}。

    **只走"触发因果路径"**（校验批修正）：`all` 下每个分支都是达成条件的一部分；
    `any` 下必须所有分支一起算（只满足其一不算）；而 `not` 下的条件取反后
    **不是达成要求**——例如 ancient_jianghu 的 `ending_wanderer` 写作
    `not: {all: [{affection: {shen_qingqiu: {gte: 60}}}, ...]}`，
    含义是"好感 <60 **或** 未进前三"，该结局**很容易达成**。
    若把取反分支里的 60 当成硬要求，就会把合法剧本误判为不可达（本函数的由来）。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "not":
                continue  # 取反分支不构成达成要求
            if key == kind and isinstance(value, dict):
                for name, spec in value.items():
                    if isinstance(spec, dict):
                        for op, threshold in spec.items():
                            if op in ("gte", "gt", "lte", "lt") and isinstance(
                                threshold, (int, float)
                            ):
                                out.setdefault(name, []).append((op, float(threshold)))
            else:
                _numeric_conditions(value, kind, out)
    elif isinstance(node, list):
        for item in node:
            _numeric_conditions(item, kind, out)


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

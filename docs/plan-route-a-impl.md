# 路线 A 场景卡数据工厂实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 存放位置遵循项目 `docs/plan-*.md` 约定（覆盖技能默认的 `docs/superpowers/plans/`）。

**Goal:** 按 `docs/plan-route-a-factory.md`（已封板）落地场景卡数据工厂 M1~M4：卡 schema+生成器、演绎器、材料装配器、rubric 评委、装配出库，以及决策 15 的 EXTRACT_SYSTEM 提示词收紧实验。

**Architecture:** 程序管标签、LLM 管形态。场景卡（pydantic，卡即标签）→ 演绎器（LLM temp 0.9，anchors 程序校验）→ 窄格式直接成对 / 开放输出拒绝采样选优 → 装配（质量门+配额+去重+sha256）。评测双轨：轨道 1 规则继承既有脚本，轨道 2 rubric 评委带探针校准。真值永不从文本反推（真实切片走 spec §5 受限通道，不在本计划）。

**Tech Stack:** Python 3.11 / pydantic 2.8（卡校验）/ pytest / 既有 `game_agent.llm.LLMClient`、`game_agent.budgets.complete_checked`、`game_agent.usage.UsageTracker`、`scripts/card_hook_check.py`（复用）。

**上游依赖（开工前确认）：**
- judge 卡物化依赖真实世界包（spec §4.1）：现有可映射包 = `ancient_jianghu`（古代武侠）/ `urban_neon`（现代都市）/ `xianxia_wendao`（仙侠）；**G1（`world-packs/G1_republic_spy`，民国谍战）待造**——G1 未就绪时 judge 只出 schema 与合成语料，材料装配排后（spec §10.3）。`scripts/import_story.py` 已在库可用于造 G1。
- 决策 15 实验需 GPU 机跑 14B（零 API 成本），与工厂任务并行，不阻塞 M1~M4。

---

## 文件结构（分解锁定）

| 文件 | 职责 |
| --- | --- |
| `scripts/scenario_factory/__init__.py` | 包标记（空 docstring） |
| `scripts/scenario_factory/cards.py` | ScenarioCard pydantic 模型 + §3.3 校验 + 种子空间/轴空间 + 确定性生成器 |
| `scripts/scenario_factory/materialize.py` | 卡 → `GameState` → `ContextBuilder.status_text`（生产同形材料）+ 校验①②③ |
| `scripts/scenario_factory/verbalize.py` | 卡 → 自然文本（LLM temp 0.9）+ anchors 在位/反向校验 + 重演一次丢弃 |
| `scripts/scenario_factory/assemble.py` | 质量门 + 配额 + 前缀指纹去重 + sha256 manifest 出库 + card_hook 门禁 |
| `scripts/rubric_judge.py` | 评委四模式：score / pairwise / select / probe（temp=0，位置交换×3 重复） |
| `tests/test_scenario_factory.py` | 全部守卫测试（离线，StubLLM） |
| `game_agent/memory.py` | **仅 Task 10 改**：EXTRACT_SYSTEM 加判定式+去偏置（决策 15） |

**分层纪律**：`game_agent/`（引擎包）只被 import，除 Task 10 外零改动；工厂全部住 `scripts/`。`card_hook_check` 不是包成员，由 `assemble.py` 用 `sys.path` 注入 `scripts/` 后 import（脚本层先例见 `scripts/diag_turn.py:17`；**tests 层不用此法**——测试里"导入非包脚本"的既有惯例是 `importlib.util.spec_from_file_location`，见 `tests/test_card_hook_check.py:17-19`；本计划的测试只 import `scripts.scenario_factory.*`，故不涉及）。

---

### Task 1: 包骨架 + 场景卡 Schema（pydantic 模型 + §3.3 校验规则）

**Files:**
- Create: `scripts/scenario_factory/__init__.py`
- Create: `scripts/scenario_factory/cards.py`
- Test: `tests/test_scenario_factory.py`

- [ ] **Step 1: 写失败测试**

`tests/test_scenario_factory.py`：

```python
"""场景卡工厂守卫测试（离线；StubLLM 见 Task 4）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

# Task 1 只导入本 Task 已实现的符号（`generate_card`/`layer_of` 由 Task 2 追加到本行）
from scripts.scenario_factory.cards import ScenarioCard


def _judge_card(**over):
    base = {
        "card_id": "sc-10231-0001", "seed": 10231, "module": "judge",
        "pack": "world-packs/xianxia_wendao",
        "axes": {"genre": "仙侠", "style": "口语叙事", "entity_forms": ["人名"],
                 "stat_system": "灵石", "relation": "师门", "scale": {"nodes": 6, "npcs": 2}},
        "facts": [
            {"type": "物品与装备", "importance": 8, "text": "玩家的剑名为「听雨」",
             "anchors": ["听雨"], "in_material": True},
        ],
        "corruptions": [
            {"category": "setting", "target_fact": 0, "method": "近义改写",
             "detail": "剑名「听雨」演绎为「听风」", "expect": "问题类型：设定矛盾"},
        ],
        "material": {"day": 3, "scene": "山门", "present": ["bai_zhi"],
                     "affections": {"bai_zhi": 45}},
        "history_spec": {"turns": 1, "target_tokens": 600, "noise": "寒暄"},
    }
    base.update(over)
    return base


def test_valid_judge_card_passes():
    ScenarioCard(**_judge_card())


def test_card_id_must_match_seed():
    with pytest.raises(ValidationError, match="card_id"):
        ScenarioCard(**_judge_card(card_id="sc-99999-0001"))


def test_target_fact_out_of_range():
    bad = _judge_card()
    bad["corruptions"][0]["target_fact"] = 5
    with pytest.raises(ValidationError, match="target_fact"):
        ScenarioCard(**bad)


def test_setting_requires_in_material_true():
    bad = _judge_card()
    bad["facts"][0]["in_material"] = False
    with pytest.raises(ValidationError, match="setting"):
        ScenarioCard(**bad)


def test_confab_requires_false_and_all_false_and_present():
    good = _judge_card()
    good["facts"][0]["in_material"] = False
    good["corruptions"][0].update(category="confab", expect="问题类型：虚构事实")
    ScenarioCard(**good)  # 全 false + present 非空 → 通过
    bad = _judge_card(facts=[
        {"type": "物品与装备", "importance": 8, "text": "t", "anchors": ["a"], "in_material": False},
        {"type": "目标与线索", "importance": 5, "text": "b", "anchors": ["b"], "in_material": True},
    ])
    bad["corruptions"][0].update(category="confab", expect="问题类型：虚构事实")
    with pytest.raises(ValidationError, match="全部"):
        ScenarioCard(**bad)


def test_expect_must_align_with_judge_categories():
    bad = _judge_card()
    bad["corruptions"][0]["expect"] = "问题类型：时间线错误"
    with pytest.raises(ValidationError, match="设定矛盾"):
        ScenarioCard(**bad)


def test_judge_requires_pack_material_and_single_corruption():
    with pytest.raises(ValidationError, match="pack"):
        ScenarioCard(**_judge_card(pack=None))
    with pytest.raises(ValidationError, match="恰好 1 条"):
        ScenarioCard(**_judge_card(corruptions=[]))


def test_extract_card_accepts_existing_for_dedup_discipline():
    """契约：existing 是生产模板的一半（`已有事实：{existing}`），必须可入卡。"""
    card = ScenarioCard(**_judge_card(module="extract", pack=None, material=None,
                                      existing=["玩家的佩剑名叫听雨"], facts=[],
                                      corruptions=[]))
    assert card.existing == ["玩家的佩剑名叫听雨"] and card.facts == []


def test_ooc_target_fact_may_be_null():
    card = _judge_card()
    card["corruptions"][0].update(category="ooc", target_fact=None,
                                  expect="问题类型：OOC")
    ScenarioCard(**card)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: FAIL（`ModuleNotFoundError: scripts.scenario_factory`）

- [ ] **Step 3: 实现 cards.py 模型部分**

`scripts/scenario_factory/__init__.py`：`"""场景卡数据工厂（spec: docs/plan-route-a-factory.md）。"""`

`scripts/scenario_factory/cards.py`：

```python
"""场景卡：卡即标签——真值由程序拥有，LLM 只做表面演绎（spec §3）。"""
from __future__ import annotations

import random
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# judge.py ①②③ 的类别词（expect 与之对齐，§3.3）
CATEGORY_EXPECT = {"setting": "设定矛盾", "confab": "虚构事实", "ooc": "OOC"}
CORRUPTION_METHODS = ("近义改写", "指代漂移", "时间线错位", "实体合并")

SEED_SPACES = {"train": range(10000, 20000), "dev": range(20000, 30000),
               "eval": range(30000, 40000)}


def layer_of(seed: int) -> str:
    for name, space in SEED_SPACES.items():
        if seed in space:
            return name
    raise ValueError(f"seed 不在 train/dev/eval 任一空间: {seed}")


class FactSpec(BaseModel):
    type: str
    importance: int = Field(ge=1, le=10)
    text: str
    anchors: list[str] = Field(min_length=1)
    in_material: bool = True


class CorruptionSpec(BaseModel):
    category: Literal["setting", "confab", "ooc"]
    target_fact: int | None = None
    method: Literal["近义改写", "指代漂移", "时间线错位", "实体合并"]
    detail: str
    expect: str


class PreservePoint(BaseModel):
    text: str
    anchors: list[str] = Field(min_length=1)


class MaterialSpec(BaseModel):
    day: int = Field(ge=1)
    scene: str
    present: list[str] = Field(default_factory=list)
    affections: dict[str, float] = Field(default_factory=dict)
    # None（缺省/省略）= 取 facts[in_material=true] 全部下标；[] = 显式一条不写
    facts: list[int] | None = None
    memories: dict[str, list[str]] = Field(default_factory=dict)
    recent: str = ""  # 决策 17：与生产同形的检索上下文（rank_facts/lore 命中都吃）


class HistorySpec(BaseModel):
    turns: int = Field(ge=1)
    target_tokens: int = Field(ge=1)
    noise: str = ""


class AxesSpec(BaseModel):
    genre: str
    style: str
    entity_forms: list[str]
    stat_system: str
    relation: str
    scale: dict[str, int]
    input_form: str | None = None


class ScenarioCard(BaseModel):
    card_id: str
    seed: int
    module: Literal["extract", "judge", "compress", "reflect"]
    pack: str | None = None
    axes: AxesSpec
    facts: list[FactSpec] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    # extract 专用：已有事实 → 生产模板的 `已有事实：{existing}` 段（见 §4.2.1「考去重纪律」）。
    # **facts 为空 = 该回合无新事实 = 标签「无」**（负例；近义改写型负例须同时给 existing）。
    existing: list[str] = Field(default_factory=list)
    corruptions: list[CorruptionSpec] = Field(default_factory=list)
    preserve_points: list[PreservePoint] = Field(default_factory=list)
    material: MaterialSpec | None = None
    # compress 专用：旧摘要（生产 user 模板的 `<旧摘要>` 段；首压为空串，增量合并非空）
    old_summary: str = ""
    history_spec: HistorySpec

    @model_validator(mode="after")
    def _check_rules(self) -> "ScenarioCard":
        if not re.fullmatch(rf"sc-{self.seed}-\d{{4}}", self.card_id):
            raise ValueError(f"card_id 须为 sc-{{seed}}-XXXX: {self.card_id}")
        layer_of(self.seed)  # seed 必须在三段种子空间之一
        if self.module != "judge":
            return self
        if not self.pack or not self.material:
            raise ValueError("judge 卡必须带 pack 与 material（§3.2/决策 17）")
        if len(self.corruptions) != 1:
            raise ValueError("单通路纪律：每张 judge 卡恰好 1 条 corruption（§3.3）")
        c = self.corruptions[0]
        if c.category != "ooc":
            if c.target_fact is None or not 0 <= c.target_fact < len(self.facts):
                raise ValueError("target_fact 必须指向存在的 facts 下标")
            f = self.facts[c.target_fact]
            if c.category == "setting" and not f.in_material:
                raise ValueError("setting 矛盾必须建立在 in_material: true 的事实上")
            if c.category == "confab":
                if f.in_material:
                    raise ValueError("confab 所指事实必须 in_material: false")
                if not self.material.present:
                    raise ValueError("confab 必须非空 present（说话人角色卡必进材料）")
                if any(f2.in_material for f2 in self.facts):
                    raise ValueError("confab 卡的 facts 全部须 in_material: false")
        key = CATEGORY_EXPECT[c.category]
        if key not in c.expect:
            raise ValueError(f"expect 须含生产判官类别词「{key}」（judge.py ①②③）")
        return self
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scenario_factory tests/test_scenario_factory.py
git commit -m "feat(factory): 场景卡 schema 与 §3.3 校验规则（Task 1）"
```

---

### Task 2: 轴空间 + 确定性生成器（含 dev 孪生，决策 19）

**Files:**
- Modify: `scripts/scenario_factory/cards.py`（追加）
- Test: `tests/test_scenario_factory.py`（追加）

- [ ] **Step 1: 写失败测试（追加到测试文件）**

```python
# 本 Task 起把 Task 1 的那行导入**扩为**下面这行（保留 ScenarioCard，追加三个符号）：
from scripts.scenario_factory.cards import (
    PACK_BY_GENRE,
    ScenarioCard,
    generate_card,
    layer_of,
)


def test_layer_of():
    assert layer_of(10231) == "train"
    assert layer_of(20231) == "dev"
    assert layer_of(30231) == "eval"
    with pytest.raises(ValueError):
        layer_of(999)


def test_generate_is_deterministic_and_seed_spaced():
    a = generate_card(10231, 6, "extract")   # seq%10==6 → 正例卡（⑦⑧ 是负例位，见生成器）
    b = generate_card(10231, 6, "extract")
    assert a == b  # 同 seed+seq+module → 同一张卡（可复现）
    assert a.card_id == "sc-10231-0006"
    assert a.module == "extract" and len(a.facts) >= 3 and a.events


def test_eval_only_axis_never_leaks_to_train_dev():
    for seq in range(60):
        assert generate_card(10000 + seq, seq, "extract").axes.genre != "民国谍战"
        dev_card = generate_card(20000 + seq, seq, "extract")
        assert dev_card.axes.genre != "民国谍战"  # dev 只许孪生「抗战谍战」
        assert dev_card.axes.style != "书信体"
    genres = {generate_card(30000 + s, s, "extract").axes.genre for s in range(60)}
    assert "民国谍战" in genres  # eval 空间抽得到留出轴


def test_judge_card_genre_must_have_pack():
    for seq in range(40):
        c = generate_card(10231, seq, "judge")
        assert c.axes.genre in PACK_BY_GENRE  # 无包映射的题材不出 judge 卡


def test_extract_dedup_discipline_cards_exist_and_carry_existing():
    """spec §4.2.1 硬要求：负例必配，其中「已有事实的近义改写」型须带 existing。"""
    cards = [generate_card(10231, s, "extract") for s in range(80)]
    dedup = [c for c in cards if c.existing]
    assert dedup, "生成器必须产出「已有事实」型卡片（考去重纪律）"
    for c in dedup:
        assert c.facts == []          # 近义改写型 → 标签「无」（无新事实）
        assert c.events               # 仍需情节骨架供演绎器写复述型回合
    plain_neg = [c for c in cards if not c.existing and not c.facts]
    assert plain_neg, "生成器必须产出「纯寒暄」型负例（无 existing、无 facts）"
    assert len([c for c in cards if not c.facts]) / len(cards) >= 0.15  # 负例 ≥15%


def test_fact_anchors_are_pairwise_disjoint():
    """反向校验（Task 4）的前提：同一张卡上不同事实的 anchors 不得相交——
    否则 setting 卡的「原 anchor 不得出现」永远不成立，该类卡永久产出不了样本。"""
    for seq in range(40):
        for mod in ("extract", "judge", "compress"):
            anchors = [a for f in generate_card(10231, seq, mod).facts for a in f.anchors]
            assert len(anchors) == len(set(anchors)), f"{mod}#{seq} anchors 相交: {anchors}"


def test_corruption_detail_quotes_are_parseable():
    """Task 4 的 `_corruption_swap` 用 `「([^」]+)」` 解析 detail：
    嵌套「」（如把整条 text 引进去）会解出残缺串 → 该卡**恒被丢弃**，
    而 confab 占 judge 配额 ≥40% —— 是静默良率损失，必须钉住。"""
    for seq in range(40):
        c = generate_card(10231, seq, "judge")
        cor = c.corruptions[0]
        assert cor.detail.count("「") == cor.detail.count("」"), cor.detail
        quoted = re.findall(r"「([^」]+)」", cor.detail)
        assert all("「" not in q and "」" not in q for q in quoted), cor.detail
        if cor.category == "setting":
            assert len(quoted) == 2, f"setting 需「原值」「新值」两引（反向校验依赖）: {cor.detail}"
        elif cor.category == "confab":
            assert len(quoted) == 1, f"confab 只应引被断言的 anchor 一个「」: {cor.detail}"
        else:  # ooc：改写语气/底线，不涉及具体值 → 无引用
            assert quoted == [], cor.detail
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "layer or generate or axis"`
Expected: FAIL（`ImportError: cannot import name 'generate_card'`）

- [ ] **Step 3: 实现生成器（追加到 cards.py）**

```python
# ---- 轴空间（§8 + 决策 19：留出轴值只对 eval 开放，dev 配同类孪生） ----
GENRES_BASE = ["古代武侠", "现代都市", "太空科幻", "仙侠", "蒸汽朋克", "校园", "年代"]
GENRE_EVAL_ONLY, GENRE_DEV_TWIN = "民国谍战", "抗战谍战"
STYLES_BASE = ["口语叙事", "冷硬白描", "技术术语"]
STYLE_EVAL_ONLY, STYLE_DEV_TWIN = "书信体", "日记体"
ENTITY_POOL = ["人名", "地名", "组织", "物品", "技艺", "货币"]
STAT_POOL = ["银两", "灵石", "信用点", "法币", "声望"]
RELATION_POOL = ["师门", "雇佣", "单线联络", "敌对", "家族"]

# judge 卡物化依赖真实世界包（§4.1）：无包映射的题材不出 judge 卡
PACK_BY_GENRE = {
    "古代武侠": "world-packs/ancient_jianghu",
    "现代都市": "world-packs/urban_neon",
    "仙侠": "world-packs/xianxia_wendao",
    "民国谍战": "world-packs/G1_republic_spy",  # G1 待造，见 §10.3 依赖说明
}

# judge 卡必带 pack 的题材 → 该包现有 NPC（材料装配用；无映射的用中性占位）
NPC_BY_PACK = {"xianxia_wendao": "bai_zhi", "urban_neon": "lin_che",
               "ancient_jianghu": "shen_qingqiu"}
DEFAULT_JUDGE_NPC = "station_chief"  # G1 待造，先用占位（材料装配排在 G1 之后）

_FACT_TPL = [  # (type, importance, text 模板, anchors 模板)
    # {g1} = 流派词；{n0}/{n1} = **本事实独占**的专名槽（不可跨模板共用！
    # 共用会让两张事实带同一 anchor，则 setting 卡的「原词不得出现」反向校验永远不成立，
    # 该类卡将永久产出不了样本 —— 静默良率损失）
    ("物品与装备", 8, "玩家的{g1}名为「{n0}」", ["{n0}"]),
    ("债务与人情", 6, "玩家欠{n0}五十两，约定中秋前归还", ["五十两", "中秋"]),
    ("身份身世", 9, "玩家的代号是「{n0}」", ["{n0}"]),
    ("目标与线索", 5, "玩家在打听「{n0}」的下落", ["{n0}"]),
    ("承诺与约定", 7, "玩家答应把{n0}转交给{n1}", ["{n0}", "{n1}"]),
]
_NAME_POOL = ["听雨", "白鸮", "断刃崖", "灰雀号", "密码本", "环宇", "旧书店", "青瓷"]


def _axes_for(layer: str, rng: random.Random) -> AxesSpec:
    genres = list(GENRES_BASE)
    styles = list(STYLES_BASE)
    if layer == "eval":
        genres.append(GENRE_EVAL_ONLY)
        styles.append(STYLE_EVAL_ONLY)
    elif layer == "dev":  # 决策 19：孪生轴值（同类不同取值），不得与 eval 重合
        genres.append(GENRE_DEV_TWIN)
        styles.append(STYLE_DEV_TWIN)
    return AxesSpec(
        genre=rng.choice(genres), style=rng.choice(styles),
        entity_forms=rng.sample(ENTITY_POOL, k=3), stat_system=rng.choice(STAT_POOL),
        relation=rng.choice(RELATION_POOL),
        scale={"nodes": rng.randint(5, 12), "npcs": rng.randint(2, 6)},
    )


def _names(rng: random.Random) -> list[str]:
    """整池打乱（8 个）：每张事实独占 2 个槽（4 事实 × 2 = 8），保证 anchors 互不相交。"""
    pool = list(_NAME_POOL)
    rng.shuffle(pool)
    return pool


def generate_card(seed: int, seq: int, module: str) -> ScenarioCard:
    """确定性生成：同 (seed, seq, module) 必出同一张卡。轴值按层开放（§8）。"""
    layer = layer_of(seed)
    rng = random.Random(f"{seed}:{seq}:{module}")
    axes = _axes_for(layer, rng)
    pool = _names(rng)
    n2, n3 = pool[0], pool[1]          # events（情节骨架）用的公共专名
    g1 = {"古代武侠": "佩剑", "仙侠": "佩剑"}.get(axes.genre, "装备")
    facts = []
    for k, (t, i, x, al) in enumerate(rng.sample(_FACT_TPL, k=4)):
        slots = {"g1": g1, "n0": pool[2 * k], "n1": pool[2 * k + 1]}  # 本事实独占槽
        facts.append(FactSpec(type=t, importance=i,
                              text=x.format(**slots),
                              anchors=[a.format(**slots) for a in al]))
    hs = HistorySpec(turns=rng.randint(1, 8),
                     target_tokens=rng.choice([600, 3000, 20000]), noise="日常寒暄")
    base = dict(card_id=f"sc-{seed}-{seq:04d}", seed=seed, module=module,
                axes=axes, history_spec=hs)
    if module == "extract":
        # spec §4.2.1：负例必配 ≥15%，两类（用 seq%10 保证**确定性**且任取 10 连号都两类齐全）：
        #   ⑦ 近义改写型 → 带 existing、无新事实 → 考「不得重复已有事实」（去重纪律）
        #   ⑧ 纯寒暄型   → 既无 existing 也无新事实 → 考「无」
        slot = seq % 10
        if slot == 7:
            return ScenarioCard(
                existing=[facts[0].text],
                events=[f"玩家与{n2}又把旧事重提了一遍", "闲谈收尾"],
                facts=[], **base)
        if slot == 8:
            return ScenarioCard(
                events=[f"玩家与{n2}闲谈天气与茶汤", "当日无事发生"],
                facts=[], **base)
        return ScenarioCard(events=[f"玩家与{n2}提起{n3}", f"玩家按{g1}起誓"],
                            facts=facts, **base)
    if module == "judge":
        genre = axes.genre if axes.genre in PACK_BY_GENRE else rng.choice(
            list(PACK_BY_GENRE))
        base["axes"] = axes = axes.model_copy(update={"genre": genre})
        category = rng.choice(["setting", "confab", "ooc"])
        if category == "setting":
            # 反向校验需要「原值 → 新值」两个「」引用（Task 4 的 _corruption_swap 依赖此格式）
            kept = [f.model_copy() for f in facts[:2]]
            cor = CorruptionSpec(category="setting", target_fact=0, method="近义改写",
                detail=f"「{kept[0].anchors[0]}」演绎为「{n3}」",
                expect="问题类型：设定矛盾")
        elif category == "confab":
            # 断言型编造：整条事实不在材料里。detail 只引 **一个**「」（被断言的 anchor），
            # 不得嵌套引用整条 text（text 自带「」，会让 Task 4 的正则解析出残缺串 → 该卡恒被丢弃）
            kept = [f.model_copy(update={"in_material": False}) for f in facts[:2]]
            cor = CorruptionSpec(category="confab", target_fact=0, method="近义改写",
                detail=f"把「{kept[0].anchors[0]}」这件事当作既成事实断言——材料里从未有过",
                expect="问题类型：虚构事实")
        else:
            kept = facts[:2]
            cor = CorruptionSpec(category="ooc", target_fact=None, method="近义改写",
                detail="说话人语气撞其角色卡 speech_style", expect="问题类型：OOC")
        npc = NPC_BY_PACK.get(PACK_BY_GENRE[genre].rsplit("/", 1)[-1], DEFAULT_JUDGE_NPC)
        return ScenarioCard(pack=PACK_BY_GENRE[genre], facts=kept, corruptions=[cor],
            material=MaterialSpec(day=rng.randint(2, 15), scene=f"{genre}·场景",
                present=[npc], affections={npc: 45}, recent=f"玩家向{n2}问起{n3}"),
            **base)
    if module == "compress":
        # input_form（spec §3.2）：首压 = 无旧摘要；增量合并 = 带旧摘要（生产 user 模板首段）
        form = "增量合并" if seq % 4 == 0 else "首压"
        # 注意：base 已含 axes，须**改 base 再解包**，不能同时传 axes= 与 **base
        #（否则 TypeError: got multiple values for keyword argument 'axes'）
        base["axes"] = axes.model_copy(update={"input_form": form})
        return ScenarioCard(
            facts=facts,
            old_summary="【剧情摘要】" + f"此前玩家已与{n2}相识，旧事略。" if form == "增量合并" else "",
            preserve_points=[PreservePoint(text=f.text, anchors=f.anchors) for f in facts[:2]],
            events=[f"事件{i}：玩家与{n2}周旋" for i in range(3)], **base)
    return ScenarioCard(facts=facts, **base)  # reflect（照造，不进第一批训练）
```

注：`_FACT_TPL`/`_NAME_POOL` 为最小可用内容池，扩池不改逻辑；配额矩阵（plan-phase1-data.md §4.4）在 Task 8 装配侧按层计数落实。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: 16 passed（Task 1 的 9 + Task 2 的 7）

- [ ] **Step 5: Commit**

```bash
git add scripts/scenario_factory/cards.py tests/test_scenario_factory.py
git commit -m "feat(factory): 轴空间/种子空间与确定性生成器（Task 2）"
```

---

### Task 3: 材料装配器 materialize.py（含 recent，校验①②③）

**Files:**
- Create: `scripts/scenario_factory/materialize.py`
- Test: `tests/test_scenario_factory.py`（追加）

- [ ] **Step 1: 写失败测试（追加）**

```python
from scripts.scenario_factory.materialize import MaterializeError, build_material


def _setting_card(**over):
    """setting 卡：facts 全 in_material=true —— 专用来测校验②（在位）。"""
    base = _judge_card()          # 夹具本身即 setting 卡（听雨 → 听风）
    base.update(over)
    return ScenarioCard(**base)


def _confab_card(**over):
    """confab 卡：facts **全** in_material=false（Task 1 硬规则）—— 专用来测校验③（泄漏）。"""
    base = _judge_card()
    base["facts"][0]["in_material"] = False
    base["corruptions"][0].update(category="confab", expect="问题类型：虚构事实")
    base.update(over)
    return ScenarioCard(**base)


def test_setting_material_materializes_facts():
    """校验②：in_material=true 的事实须物化进「关键事实」区（全部 anchors 在位）。"""
    text = build_material(_setting_card())
    assert "听雨" in text and "白芷" in text      # 事实 + 在场角色卡


def test_confab_material_excludes_the_claim():
    """校验③：confab 的断言事实不得进材料（该卡 facts 全 false → 关键事实区为空）。"""
    assert "听雨" not in build_material(_confab_card())


def test_material_leak_raises():
    card = _confab_card()
    card.material.memories = {"bai_zhi": ["玩家提到佩剑听雨"]}  # 人为泄漏
    with pytest.raises(MaterializeError, match="泄漏"):
        build_material(card)


def test_missing_in_material_anchor_raises():
    card = _setting_card()
    card.facts[0].anchors = ["不存在的专名xyz"]
    with pytest.raises(MaterializeError, match="未在材料中"):
        build_material(card)


def test_present_npc_must_be_in_pack():
    card = _setting_card()
    card.material.present = ["ghost_npc"]
    with pytest.raises(MaterializeError, match="不在包里"):
        build_material(card)
```

注：测试用真实包 `xianxia_wendao`（仓库先例：`tests/test_second_worldpack.py` 同法）。`material.facts` 省略（None）→ 缺省取 `in_material=true` 下标。**spec 侧已先行改好并提交**（详见 Step 5 的表：A.2/A.3 的 `facts: []` 行已删、`recent` 已字符串化、§3.2 已补 `facts: null` 语义注释），故本 Task **不需要再动 spec**——Step 5 只做核验（`git diff` 应为空）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k material`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 materialize.py**

```python
"""材料装配器：卡 → GameState → ContextBuilder.status_text（§4.1，生产同形）。

复用生产组装器（禁止另写材料模板）；recent 字段来自决策 17——status_text 的
rank_facts / lore 命中都吃 context = scene + node.goal + recent（context.py:181），
工厂必须传 recent，否则排序输入与生产不同形。
"""
from __future__ import annotations

import pathlib

from game_agent.context import ContextBuilder
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import load_worldpack

from .cards import ScenarioCard

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class MaterializeError(ValueError):
    """材料装配或校验失败——调用方丢弃该样本并计数（§4.1）。"""


def build_material(card: ScenarioCard) -> str:
    if not card.pack or not card.material:
        raise MaterializeError("judge/compress 卡必须带 pack + material")
    pack = load_worldpack(REPO_ROOT / card.pack)
    m = card.material
    state = GameState.from_pack(pack)
    state.day, state.scene = m.day, m.scene
    state.present_npcs = list(m.present)
    for k, v in m.affections.items():
        if k in state.affections:
            state.affections[k] = float(v)
    idx = m.facts if m.facts is not None else [
        i for i, f in enumerate(card.facts) if f.in_material
    ]
    state.player_facts = [
        MemoryEntry(fact=card.facts[i].text, day=m.day, round=0,
                    importance=card.facts[i].importance)
        for i in idx
    ]
    state.npc_memories = {
        k: [MemoryEntry(fact=t, day=m.day, round=0) for t in v]
        for k, v in m.memories.items()
    }
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent=m.recent)
    _check(card, pack, text)
    return text


def _check(card: ScenarioCard, pack, text: str) -> None:
    m = card.material
    if not text.strip():
        raise MaterializeError("材料为空")
    for npc_id in m.present:  # 校验①：在场 NPC 角色卡确落材料
        if npc_id not in pack.npcs:
            raise MaterializeError(f"present 的 NPC 不在包里: {npc_id}")
        if pack.npcs[npc_id].name not in text:
            raise MaterializeError(f"在场角色卡未落入材料: {npc_id}")
    for f in card.facts:  # 校验②在位（全部 anchors）/ ③泄漏（任一 anchors 出现即泄漏）
        hit = [a for a in f.anchors if a in text]
        if f.in_material and len(hit) != len(f.anchors):
            raise MaterializeError(f"in_material 事实 anchors 未在材料中: {f.anchors}")
        if not f.in_material and hit:
            raise MaterializeError(f"材料泄漏：confab anchors 出现在材料中: {hit}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q -k material`
Expected: 5 passed

- [ ] **Step 5: 核验 spec 已修订 + Commit**

spec 的相关修正在**设计阶段就已落实并提交**（不留给实现阶段做），共**四处**：

| 处 | 现状（已落实） |
| --- | --- |
| §3.2 schema | `recent: ""` —— **字符串**，与 `context.py:131-137` 的 `recent: str = ""` 同形；并补了 `facts: null` 的语义注释（`null`=缺省取 `facts[in_material=true]`；`[]`=显式一条不写） |
| §4.1 装配行 | `status_text(state, None, recent=material.recent)`（不再是 `"\n".join(material.recent)`） |
| 附录 A.2 | 已删除 `facts: []` 行；`recent` 已字符串化；并补注"省略 facts = 缺省取 `in_material=true` 的事实" |
| 附录 A.3 | 已删除 `facts: []` 行（**省略即空、非漏字段**：§3.3 强制 confab 卡全 `in_material: false`，缺省取用自然为空）；`recent` 已字符串化 |

因此本步**只核验、不改动**：

```bash
git diff docs/plan-route-a-factory.md   # 应为空（无任何输出）
```

若不为空，说明 spec 又被写回旧写法 → 按上表回正，并在提交说明里注明原因。

然后**只提交本 Task 的两个代码文件**：

```bash
git add scripts/scenario_factory/materialize.py tests/test_scenario_factory.py
git commit -m "feat(factory): 材料装配器与校验①②③（Task 3）"
```

---

### Task 4: 演绎器 verbalize.py（anchors 在位 + 反向校验 + 重演丢弃）

**Files:**
- Create: `scripts/scenario_factory/verbalize.py`
- Test: `tests/test_scenario_factory.py`（追加）

- [ ] **Step 1: 写失败测试（追加）**

```python
import re
from types import SimpleNamespace

from scripts.scenario_factory.verbalize import verbalize_card


class StubLLM:
    """budgets._complete 的轻量替身协议：complete_with_meta(messages, **kw)。

    **队列语义：末条粘滞**（取到最后一条后重复使用）。原因是有两处会多吃一条队列，
    夹具若给"刚好够"的条数就会 `IndexError: pop from empty list`：
      ① `complete_checked` 在空响应/截断（`finish_reason="length"`）时会用
         **升级预算重试一次**（`budgets.py:70-81`）；
      ② `verbalize_card` 自身有 `MAX_ATTEMPTS` 循环。
    调用次数仍由 `self.calls` 精确可数，故断言不受影响。
    """

    def __init__(self, texts, finish="stop"):
        self.texts = list(texts)
        self.finish = finish
        self.calls = []

    def complete_with_meta(self, messages, **kw):
        self.calls.append(kw)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return SimpleNamespace(text=text, finish_reason=self.finish)


def _extract_card():
    return generate_card(10231, 6, "extract")   # 正例位（必有 anchors 可校验）


def test_verbalize_ok_first_try():
    card = _extract_card()
    anchors = [a for f in card.facts for a in f.anchors]
    llm = StubLLM(["渡口茶棚里，" + "，".join(anchors) + "，闲谈收尾。"])
    r = verbalize_card(llm, card)
    assert not r.dropped and r.attempts == 1 and llm.calls[0]["temperature"] == 0.9


def test_verbalize_retry_then_drop_when_anchor_missing():
    card = _extract_card()
    llm = StubLLM(["没有专名的文本", "还是没有"])
    r = verbalize_card(llm, card)
    assert r.dropped and r.attempts == 2


def test_verbalize_truncated_discards():
    llm = StubLLM(["半截文本"], finish="length")
    assert verbalize_card(llm, _extract_card()).dropped


def _setting_card_from_generator():
    """取一张 setting 卡：反向校验（原词不得出现）只对它有意义。"""
    return next(c for s in range(20)
                if (c := generate_card(10231, s, "judge")).corruptions[0].category == "setting")


def test_verbalize_corruption_reverse_check():
    card = _setting_card_from_generator()
    hit, rest = card.facts[0], card.facts[1:]
    quoted = re.findall(r"「([^」]+)」", card.corruptions[0].detail)  # [原值, 新值]
    rest_anchors = [a for f in rest for a in f.anchors]

    # 好样本：其余事实 anchors 在位 + corruption 新值在位 + 被命中事实的**原 anchor 不在**
    good = "叙事：" + "、".join(rest_anchors + [quoted[-1]])
    assert not verbalize_card(StubLLM([good]), card).dropped

    # 坏样本：把原 anchor 写回去 → 必须丢弃（这就是反向校验）
    bad = "叙事：" + "、".join(rest_anchors + [quoted[-1], hit.anchors[0]])
    assert verbalize_card(StubLLM([bad]), card).dropped


def test_verbalize_confab_requires_the_fabricated_anchor():
    """confab：叙事**必须**把编造的 anchor 说出来 —— 与 setting 的"原词不得出现"方向相反。

    早先一版把 confab 也按 setting 处理（同一条 anchor 既要求在位、又列为不得出现），
    结果 confab 卡恒被丢弃；而 confab 占 judge 配额 ≥40%，是重大静默损失。
    """
    card = next(c for s in range(20)
                if (c := generate_card(10231, s, "judge")).corruptions[0].category == "confab")
    cor = card.corruptions[0]
    assert cor.target_fact is not None
    wanted = re.findall(r"「([^」]+)」", cor.detail)      # 被断言的 anchor
    rest = [a for i, f in enumerate(card.facts) if i != cor.target_fact for a in f.anchors]

    good = "叙事：" + "、".join(rest + wanted)            # 说出了编造 → 收下
    assert not verbalize_card(StubLLM([good]), card).dropped

    missing_claim = "叙事：" + "、".join(rest)            # 没说出编造 → 丢弃
    assert verbalize_card(StubLLM([missing_claim]), card).dropped
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k verbalize`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 verbalize.py**

```python
"""演绎器：卡 → 自然文本（spec §4）。只写表面形态；标签永不从文本反推。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from game_agent.budgets import complete_checked

from .cards import ScenarioCard

VERBALIZE_SYSTEM = (
    "你是文本演绎器。把给定的场景卡（JSON）演绎成一段自然中文叙事。"
    "纪律：①卡面 facts 的 anchors 专名/数字必须原样出现；②只写叙事正文——"
    "不得输出标签、不得列事实清单、不得在末尾总结；③不得新增卡面没有的事实性专名"
    "与数字；④语体/长度/脏度按用户指令（允许口语碎句与闲笔）。"
)
VERBALIZE_TEMPERATURE = 0.9  # 演绎要多样性（0.8~1.0 档）；标注/评判类仍 temp=0
MAX_ATTEMPTS = 2             # 缺要素 → 重演 1 次 → 仍缺则丢弃并计数


@dataclass
class VerbalizeResult:
    text: str
    attempts: int
    dropped: bool = False


def _corruption_swap(card: ScenarioCard) -> tuple[int | None, list[str], list[str]]:
    """按 category 返回 (被命中事实下标, **须在位**的值, **须缺席**的原值)。

    三类语义不同，早先一版把它们当成同一件事，导致 confab 卡**恒被丢弃**：
      - setting：原值须缺席、新值须在位（detail 格式 `「原值」演绎为「新值」`）
      - confab ：被断言的 anchor **须在位**（叙事必须把编造说出来），无缺席要求
      - ooc    ：不涉及具体值 → hit_idx=None（全部事实 anchors 须在位）
    """
    if card.module != "judge" or not card.corruptions:
        return None, [], []
    c = card.corruptions[0]
    quoted = re.findall(r"「([^」]+)」", c.detail)
    if c.category == "setting" and c.target_fact is not None:
        absent = [a for a in card.facts[c.target_fact].anchors if a in quoted]
        return c.target_fact, [q for q in quoted if q not in absent], absent
    if c.category == "confab":
        return c.target_fact, quoted, []
    return None, quoted, []


def _missing_anchors(card: ScenarioCard, text: str) -> list[str]:
    hit_idx, present, _ = _corruption_swap(card)
    missing = [a for i, f in enumerate(card.facts) if i != hit_idx
               for a in f.anchors if a not in text]
    missing += [p for p in present if p not in text]
    return missing


def _originals_present(card: ScenarioCard, text: str) -> list[str]:
    _, _, absent = _corruption_swap(card)
    return [a for a in absent if a in text]


def verbalize_card(llm, card: ScenarioCard) -> VerbalizeResult:
    user = (
        f"语体：{card.axes.style}；长度约 {card.history_spec.target_tokens} token；"
        f"可掺入的闲笔：{card.history_spec.noise}\n场景卡 JSON：\n"
        + card.model_dump_json()
    )
    if card.module == "judge":
        c = card.corruptions[0]
        user += (f"\n矛盾自然化：按「{c.method}」处理——{c.detail}；"
                 "detail 中「」内的值必须出现在文中，被改写事实的原词不得出现。")
    msgs = [{"role": "system", "content": VERBALIZE_SYSTEM},
            {"role": "user", "content": user}]
    max_tokens = int(card.history_spec.target_tokens * 1.2)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text, finish = complete_checked(llm, msgs, purpose="aux",
                                        max_tokens=max_tokens,
                                        temperature=VERBALIZE_TEMPERATURE)
        if finish == "length":  # 截断丢弃（complete_checked 已升预算重试过一次）
            continue
        if not _missing_anchors(card, text) and not _originals_present(card, text):
            return VerbalizeResult(text=text, attempts=attempt)
    return VerbalizeResult(text="", attempts=MAX_ATTEMPTS, dropped=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q -k verbalize`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scenario_factory/verbalize.py tests/test_scenario_factory.py
git commit -m "feat(factory): 演绎器与 anchors 在位/反向校验（Task 4）"
```

---

### Task 5: rubric 评委 rubric_judge.py（score / pairwise / select / probe 四模式）

**Files:**
- Create: `scripts/rubric_judge.py`
- Test: `tests/test_scenario_factory.py`（追加；StubLLM 已在 Task 4 段定义，直接复用）

- [ ] **Step 1: 写失败测试（追加）**

```python
import random

from scripts.rubric_judge import (
    make_probe, pairwise, probe_detected, quality_sample, score, select,
)
from scripts.scenario_factory.cards import PreservePoint


class MsgLLM(StubLLM):
    """StubLLM + 记录 messages（验证 pairwise 的位置交换）。"""

    def __init__(self, texts, finish="stop"):
        super().__init__(texts, finish)
        self.msgs = []

    def complete_with_meta(self, messages, **kw):
        self.msgs.append(list(messages))
        return super().complete_with_meta(messages, **kw)


_PP = [PreservePoint(text="玩家的剑名为「听雨」", anchors=["听雨"])]


def test_score_parses_json_and_validates_range():
    llm = StubLLM(['{"保真": 2, "简洁": 1, "结构": 2, "流畅": 2}'])
    s = score(llm, summary="摘要", material="材料", preserve_points=_PP)
    assert s == {"保真": 2, "简洁": 1, "结构": 2, "流畅": 2}
    assert llm.calls[0]["temperature"] == 0.0  # 评委一律 temp=0（spec §7.3）


def test_score_extracts_json_from_noisy_output():
    llm = StubLLM(['好的，评分如下：{"保真": 1, "简洁": 2, "结构": 1, "流畅": 2} 完毕'])
    assert score(llm, summary="s", material="m", preserve_points=_PP)["保真"] == 1
    bad = StubLLM(["无法评分"])
    with pytest.raises(ValueError, match="JSON"):
        score(llm=bad, summary="s", material="m", preserve_points=_PP)
    out = StubLLM(['{"保真": 3, "简洁": 0, "结构": 0, "流畅": 0}'])
    with pytest.raises(ValueError, match="越界"):
        score(llm=out, summary="s", material="m", preserve_points=_PP)


def test_pairwise_swaps_positions_and_majority_rules():
    # 评委永远偏好"先看到的那份"：A位/B位各半 → 映射回原始后 a 全胜
    llm = MsgLLM(['{"winner": "A"}', '{"winner": "B"}'] * 3)
    w = pairwise(llm, a="摘要甲", b="摘要乙", material="材料", preserve_points=_PP)
    assert w == "A" and len(llm.msgs) == 6  # 位置交换 ×3 重复 = 6 次（spec §10.2 口径）
    users = [m[-1]["content"] for m in llm.msgs]
    assert users[0].index("摘要甲") < users[0].index("摘要乙")  # 第 1 次 a 在前
    assert users[1].index("摘要乙") < users[1].index("摘要甲")  # 第 2 次已交换
    tie = MsgLLM(['{"winner": "A"}'] * 6)  # 恒定判 A 位 → 3:3 → tie
    assert pairwise(tie, a="甲", b="乙", material="m", preserve_points=_PP) == "tie"


def test_select_picks_highest_total():
    llm = StubLLM([
        '{"保真": 1, "简洁": 1, "结构": 1, "流畅": 1}',  # 总 4
        '{"保真": 2, "简洁": 2, "结构": 2, "流畅": 1}',  # 总 7 ←
        '{"保真": 1, "简洁": 2, "结构": 1, "流畅": 1}',  # 总 5
    ])
    assert select(llm, candidates=["c0", "c1", "c2"], material="m",
                  preserve_points=_PP) == 1


def test_make_probe_kinds_and_detection():
    clean = "玩家的剑名为「听雨」。他答应中秋前还银。渡口无事。"
    rng = random.Random(1)
    p1 = make_probe(clean, _PP, kind="删要点", rng=rng)
    assert "听雨" not in p1
    p2 = make_probe(clean, _PP, kind="注入虚构", rng=rng)
    assert "听雨" in p2 and len(p2) > len(clean)
    p3 = make_probe(clean, _PP, kind="打乱结构", rng=rng)
    assert sorted(p3) == sorted(clean) and p3 != clean
    assert probe_detected({"保真": 0, "简洁": 1, "结构": 1, "流畅": 1}, "删要点")
    assert not probe_detected({"保真": 1, "简洁": 1, "结构": 0, "流畅": 1}, "删要点")


def test_quality_sample_flags_template_like_output():
    """spec §7.4 质检员：自然度 <1 的样本须被剔除（抽检是常驻关卡，不是一次性检查）。"""
    llm = StubLLM(['{"自然度": 0}', '{"自然度": 2}'])
    rows = [{"id": "a", "input": "填空式模板文本"}, {"id": "b", "input": "自然叙事"}]
    assert quality_sample(llm, rows, rate=1.0, seed=1) == ["a"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "score or pairwise or select or probe"`
Expected: FAIL（`ModuleNotFoundError: No module named 'scripts.rubric_judge'`）

- [ ] **Step 3: 实现 rubric_judge.py**

```python
"""rubric 评委（spec §7）：选优 / 打分 / 成对比较 / 探针校准，一个入口四种模式。

纪律（spec §7.3）：temp=0；成对比较 = 匿名 + 位置交换 ×3 重复 = 每对 6 次调用；
探针检出口径——打分制 = 对应维度落 0 分档；成对比较 = 探针（坏）与干净样本配对必须判负。
评委只评形态，永不输出标签（标签在卡上）。
"""
from __future__ import annotations

import json
import math
import random
import re

from game_agent.budgets import MIN_CALL_TOKENS, complete_checked
from scripts.scenario_factory.cards import PreservePoint

DIMS = ("保真", "简洁", "结构", "流畅")
SCORE_TEMPERATURE = 0.0
PAIRWISE_REPEATS = 3  # ×2 位置 = 6 次调用
# 评委单次预算：**不得低于 budgets.MIN_CALL_TOKENS（500）**——硬规则（budgets.py:20）。
# 思考模型把推理链算进 max_tokens，给 200 会被思考吃光 → 空判词（正是 retro §5.3 那批 bug）。
# 走单一真源、不写死数字，避免两处漂移。
MAX_TOKENS = MIN_CALL_TOKENS

RUBRIC_SYSTEM = (
    "你是压缩摘要的质量评委。对照【材料】与【必保全要点】，按四维量规给【待评摘要】打 0/1/2 分，"
    "只输出一行 JSON，不要任何额外文字。\n"
    "保真：2=要点全在且无材料外事实；1=缺 1 个要点或 1 处轻微走样；0=缺 ≥2 个要点或出现材料外事实。\n"
    "简洁：2=无复述冗余；1=轻微冗余；0=大段照搬或兜圈。\n"
    "结构：2=时间与因果清楚；1=轻微跳跃；0=支离破碎。\n"
    "流畅：2=自然书面中文；1=有语病不妨碍理解；0=难以卒读。\n"
    '输出格式：{"保真": 0, "简洁": 0, "结构": 0, "流畅": 0}'
)
PAIRWISE_SYSTEM = (
    "你是压缩摘要评委。对照【材料】与【必保全要点】，按保真>简洁>结构>流畅的优先级"
    "判断候选 A / 候选 B 谁更好。只输出一行 JSON："
    '{"winner": "A"} 或 {"winner": "B"} 或 {"winner": "tie"}。'
)

PROBE_KINDS = ("删要点", "注入虚构", "打乱结构")
_PROBE_DIM = {"删要点": "保真", "注入虚构": "保真", "打乱结构": "结构"}
_FABRICATED = ["北冥真人", "天外星舰", "幽冥鬼市"]  # 必不在任何材料内的虚构专名


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", text or "", re.S)
        if not m:
            raise ValueError(f"评委输出不含 JSON: {(text or '')[:80]!r}")
        return json.loads(m.group(0))


def _complete(llm, system: str, user: str) -> str:
    text, finish = complete_checked(
        llm, [{"role": "system", "content": system}, {"role": "user", "content": user}],
        purpose="aux", max_tokens=MAX_TOKENS, temperature=SCORE_TEMPERATURE)
    if finish != "stop":
        raise ValueError(f"评委输出截断: finish={finish}")
    return text


def _points_text(preserve_points: list[PreservePoint]) -> str:
    return "；".join(p.text for p in preserve_points) or "（无）"


def score(llm, *, summary: str, material: str,
          preserve_points: list[PreservePoint]) -> dict[str, int]:
    """打分模式：四维各 0/1/2。评委输出异常 → ValueError（调用方计批次异常）。"""
    user = (f"【材料】\n{material}\n\n【必保全要点】\n{_points_text(preserve_points)}"
            f"\n\n【待评摘要】\n{summary}")
    d = _extract_json(_complete(llm, RUBRIC_SYSTEM, user))
    out = {k: int(d[k]) for k in DIMS}
    if any(v not in (0, 1, 2) for v in out.values()):
        raise ValueError(f"维度分越界: {out}")
    return out


def select(llm, *, candidates: list[str], material: str,
           preserve_points: list[PreservePoint]) -> int:
    """选优模式（拒绝采样用，spec §6）：总分最高者，同分取先。"""
    best, best_i = -1, 0
    for i, c in enumerate(candidates):
        total = sum(score(llm, summary=c, material=material,
                          preserve_points=preserve_points).values())
        if total > best:
            best, best_i = total, i
    return best_i


def _judge_once(llm, *, first: str, second: str, material: str,
                preserve_points: list[PreservePoint]) -> str:
    user = (f"【材料】\n{material}\n\n【必保全要点】\n{_points_text(preserve_points)}"
            f"\n\n【候选 A】\n{first}\n\n【候选 B】\n{second}")
    return str(_extract_json(_complete(llm, PAIRWISE_SYSTEM, user)).get("winner"))


def pairwise(llm, *, a: str, b: str, material: str,
             preserve_points: list[PreservePoint]) -> str:
    """成对模式：返回 'A' | 'B' | 'tie'。匿名 + 位置交换 ×3 重复，多数票；平票 → tie。"""
    votes = {"A": 0, "B": 0, "tie": 0}
    for _ in range(PAIRWISE_REPEATS):
        w1 = _judge_once(llm, first=a, second=b, material=material,
                         preserve_points=preserve_points)
        votes[w1 if w1 in votes else "tie"] += 1
        w2 = _judge_once(llm, first=b, second=a, material=material,
                         preserve_points=preserve_points)
        votes[{"A": "B", "B": "A"}.get(w2, "tie")] += 1  # 换位的胜者映射回原始
    top = max(votes.values())
    winners = [k for k, v in votes.items() if v == top]
    return winners[0] if len(winners) == 1 else "tie"


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？；\n])", text) if s]


def make_probe(summary: str, preserve_points: list[PreservePoint], *,
               kind: str, rng: random.Random) -> str:
    """探针模式·程序改坏（spec §7.3）：返回一份已知缺陷样本。"""
    if kind == "删要点":
        anchor = preserve_points[0].anchors[0]
        kept = [s for s in _sentences(summary) if anchor not in s]
        return "".join(kept) if kept else "（要点已删）"
    if kind == "注入虚构":
        return summary + f"后来{rng.choice(_FABRICATED)}现身，接管了一切。"
    if kind == "打乱结构":
        parts = _sentences(summary)
        if len(parts) < 2:
            return summary[::-1]
        shuffled = parts[:]
        while shuffled == parts:
            rng.shuffle(shuffled)
        return "".join(shuffled)
    raise ValueError(f"未知探针坏法: {kind}")


def probe_detected(scores: dict[str, int], kind: str) -> bool:
    """打分制检出口径（spec §7.3）：探针的对应维度分落入 0 分档。"""
    return scores.get(_PROBE_DIM[kind]) == 0


# ---- §7.4 数据质检员（第三处评委用法）：抽检自然度，防演绎器退化回模板 ----
NATURALNESS_SYSTEM = (
    "你是数据质检员。只判断这段中文文本的**自然度/模板味**，不评判内容对错。"
    "2=像人写的自然叙事；1=略有拼凑感但不刺眼；0=明显填空式模板或复读机。"
    '只输出一行 JSON：{"自然度": 0}'
)


def naturalness(llm, *, text: str) -> int:
    """抽检单条文本的自然度（0~2）。§7.4：<1 剔除并重造。"""
    d = _extract_json(_complete(llm, NATURALNESS_SYSTEM, f"【待检文本】\n{text}"))
    v = int(d["自然度"])
    if v not in (0, 1, 2):
        raise ValueError(f"自然度分越界: {v}")
    return v


def quality_sample(llm, samples: list[dict], *, rate: float = 0.20,
                   seed: int = 20260912) -> list[str]:
    """按 rate 抽检合成样本，返回自然度 <1 的 id（调用方剔除并重造）。**常驻关卡**，非一次性。"""
    if not samples:
        return []
    rng = random.Random(seed)
    k = min(max(1, math.ceil(len(samples) * rate)), len(samples))
    bad: list[str] = []
    for i in sorted(rng.sample(range(len(samples)), k=k)):  # 升序消费，结果与顺序无关
        s = samples[i]
        if naturalness(llm, text=s.get("input") or s.get("narration") or "") < 1:
            bad.append(s["id"])
    return bad
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "score or pairwise or select or probe or quality"`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/rubric_judge.py tests/test_scenario_factory.py
git commit -m "feat(factory): rubric 评委四模式与探针改坏/检出口径（Task 5）"
```

---

### Task 6: assemble.py 样本构建三分支 + compress 拒绝采样（spec §6）

**Files:**
- Create: `scripts/scenario_factory/assemble.py`
- Test: `tests/test_scenario_factory.py`（追加）

- [ ] **Step 1: 写失败测试（追加）**

```python
from game_agent.compression import COMPRESS_SYSTEM, SUMMARY_MAX_TARGET
from game_agent.memory import EXTRACT_SYSTEM
from scripts.scenario_factory.assemble import (
    build_compress_sample, build_extract_sample, build_judge_sample,
    compress_messages, extract_messages,
)


def _all_anchors(card):
    return [a for f in card.facts for a in f.anchors]


def test_build_extract_sample_labels_come_from_card():
    card = generate_card(10231, 6, "extract")   # 正例位
    llm = StubLLM(["叙事：" + "，".join(_all_anchors(card)) + "。"])
    r = build_extract_sample(llm, card)
    assert r.sample and r.dropped_reason is None
    assert r.sample["genre"] == card.axes.genre
    assert r.sample["expect_empty"] is False
    assert [x["text"] for x in r.sample["labels"]] == [f.text for f in card.facts]


def test_build_extract_input_matches_production_template():
    """spec §4.2 硬纪律：输入模板逐字对齐生产（`game.py:_extract_facts`）。"""
    card = generate_card(10231, 6, "extract")
    llm = StubLLM(["叙事：" + "，".join(_all_anchors(card)) + "。"])
    got = build_extract_sample(llm, card).sample["input"]
    assert got.startswith("已有事实：") and "<回合内容>" in got and got.endswith("</回合内容>")
    # 与生产同源：system 段必须是引擎的 EXTRACT_SYSTEM（不得另写提示词）
    msgs = extract_messages(card, "回合文本")
    assert msgs[0] == {"role": "system", "content": EXTRACT_SYSTEM}
    assert msgs[1]["content"] == "已有事实：\n\n<回合内容>\n回合文本\n</回合内容>"


def test_build_extract_dedup_discipline_negative_with_existing():
    """去重纪律样本：带 existing、标签为空（spec §4.2.1 负例硬要求）。"""
    card = generate_card(10231, 7, "extract")   # seq%10==7 → 近义改写型
    assert card.existing and not card.facts
    r = build_extract_sample(StubLLM(["玩家又把旧事重提了一遍。"]), card)
    assert r.sample["labels"] == [] and r.sample["expect_empty"] is True
    assert r.sample["input"].startswith(f"已有事实：{card.existing[0]}")


def test_compress_input_matches_production_template():
    """spec §4.2 硬纪律：compress 输入模板逐字对齐生产（`game.py:_compress_history`）。"""
    card = _compress_card()
    history, summary = _history_and_summary(card)
    llm = StubLLM([history, summary])
    build_compress_sample(llm, card, sampling="off")
    msgs = compress_messages(card, "新增历史文本")
    assert msgs[0] == {"role": "system",
                       "content": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)}
    assert msgs[1]["content"] == (
        f"<旧摘要>\n{card.old_summary}\n</旧摘要>\n\n<新增历史>\n新增历史文本\n</新增历史>")


def test_build_judge_sample_setting():
    card = ScenarioCard(**_judge_card())  # setting 卡：听雨→听风
    llm = StubLLM(["沈青秋收剑笑道：这柄听风倒是趁手。"])  # 替身词在、原词不在
    r = build_judge_sample(llm, card)
    assert r.sample and r.sample["expect"] == "问题类型：设定矛盾"
    assert "听雨" in r.sample["material"]      # 材料含原事实
    assert "听风" in r.sample["narration"]     # 叙事含改写值


def _compress_card(tokens=20000):
    return next(c for s in range(60)
                if (c := generate_card(10231, s, "compress")
                   ).history_spec.target_tokens == tokens)


def _history_and_summary(card):
    anchors = _all_anchors(card)
    history = "长历史：" + "，".join(anchors) + "。" + "流水账。" * 50
    keep = [a for p in card.preserve_points for a in p.anchors]
    summary = "要点摘要：" + "，".join(keep) + "。"
    return history, summary


def test_compress_off_single_candidate_no_select():
    card = _compress_card()
    history, summary = _history_and_summary(card)
    llm = StubLLM([history, summary])  # 1 次历史 + 1 次摘要；off 档不选优
    r = build_compress_sample(llm, card, sampling="off")
    assert r.sample and r.sample["candidates"] == 1 and len(llm.calls) == 2
    assert r.sample["long_input"] is True  # 20000 ≥ 8000


def test_compress_long_tier_runs_n4_and_select():
    card = _compress_card()
    history, summary = _history_and_summary(card)
    scores = ['{"保真": 2, "简洁": 2, "结构": 2, "流畅": 2}'] * 4
    llm = StubLLM([history, summary, summary, summary, summary] + scores)
    r = build_compress_sample(llm, card, sampling="long")
    assert r.sample["candidates"] == 4 and len(llm.calls) == 1 + 4 + 4


def test_compress_program_kill_fabrication_and_missing_point():
    card = _compress_card()
    history, good = _history_and_summary(card)
    bad = "摘要提到「北冥真人」。"  # 历史外「」词 → 虚构杀
    llm = StubLLM([history, bad, bad, bad, bad])
    r = build_compress_sample(llm, card, sampling="long")
    assert r.sample is None and "虚构" in r.dropped_reason
    no_point = "只写了些无关紧要的话。"
    llm2 = StubLLM([history, no_point])
    r2 = build_compress_sample(llm2, card, sampling="off")
    assert r2.sample is None and "缺要点" in r2.dropped_reason


def test_compress_short_input_tier_stays_single_under_long():
    card = _compress_card(tokens=600)
    history, summary = _history_and_summary(card)
    llm = StubLLM([history, summary])
    r = build_compress_sample(llm, card, sampling="long")  # 600 < 阈值 → 仍 n=1
    assert r.sample["candidates"] == 1 and r.sample["long_input"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "build_ or compress"`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 assemble.py（样本构建部分）**

```python
"""数据集装配（spec §9.1）：样本构建三分支 + 质量门/门禁/配额/去重/出库。

标签纪律：labels/expect/preserve_points 全部取自卡面，永不从文本反推（spec §2）。

**输入模板必须逐字对齐生产调用**（spec §4.2 开头的硬纪律，也是 spec §9.2「五模块输入契约
继承不动」的落地）——提示词一律从引擎 import，**不得另写**，否则训练分布 ≠ 推理分布：
  - extract  模板引自 `game.py:_extract_facts`（同一行格式见 `extract_eval.py:159-166`）
  - compress 模板引自 `game.py:_compress_history`
契约由守卫测试 `test_*_input_matches_production_template` 钉住，防两处漂移。

compress 拒绝采样三档（spec §6）：off=单次直出；long=仅长输入档 n=4；all=全量 n=4。
程序先杀三项：虚构（摘要「」词不在历史）/缺要点/超长。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from game_agent.budgets import COMPRESS_MAX_TOKENS, complete_checked
from game_agent.compression import COMPRESS_SYSTEM, SUMMARY_MAX_TARGET
from game_agent.memory import EXTRACT_SYSTEM
from scripts.rubric_judge import select

from .cards import ScenarioCard
from .materialize import MaterializeError, build_material
from .verbalize import verbalize_card

SAMPLE_VERSION = "route-a-v1"
LONG_INPUT_TOKENS = 8000   # spec §6「长输入档」阈值（计划口径；spec 原话为"20K 级"）
CANDIDATES_N = 4
SUMMARY_TEMPERATURE = 0.7  # 候选需多样性（spec §6 的 temp 0.8 档）；评委仍 temp=0


def extract_messages(card: ScenarioCard, turn: str) -> list[dict]:
    """生产同款 extract 输入（逐步对齐 `game.py:_extract_facts`）。"""
    existing = "；".join(card.existing)
    return [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user",
         "content": f"已有事实：{existing}\n\n<回合内容>\n{turn}\n</回合内容>"},
    ]


def compress_messages(card: ScenarioCard, history: str) -> list[dict]:
    """生产同款 compress 输入（逐步对齐 `game.py:_compress_history`）。"""
    return [
        {"role": "system", "content": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)},
        {"role": "user",
         "content": f"<旧摘要>\n{card.old_summary}\n</旧摘要>\n\n<新增历史>\n"
                    f"{history}\n</新增历史>"},
    ]


@dataclass
class BuildResult:
    sample: dict | None
    dropped_reason: str | None = None


def build_extract_sample(llm, card: ScenarioCard) -> BuildResult:
    r = verbalize_card(llm, card)
    if r.dropped:
        return BuildResult(None, "演绎丢弃")
    return BuildResult({
        "id": card.card_id, "module": "extract", "version": SAMPLE_VERSION,
        "genre": card.axes.genre,
        # 生产同款 user 段（含 `已有事实：` 与 `<回合内容>` 包裹）——不是裸文本
        "input": extract_messages(card, r.text)[1]["content"],
        "existing": list(card.existing),
        "expect_empty": not card.facts,   # facts 空 = 该回合无新事实 = 标签「无」（负例）
        "labels": [{"type": f.type, "text": f.text, "importance": f.importance}
                   for f in card.facts],
    })


def build_judge_sample(llm, card: ScenarioCard) -> BuildResult:
    try:
        material = build_material(card)
    except MaterializeError as e:
        return BuildResult(None, f"材料装配失败: {e}")
    r = verbalize_card(llm, card)
    if r.dropped:
        return BuildResult(None, "演绎丢弃")
    c = card.corruptions[0]
    return BuildResult({
        "id": card.card_id, "module": "judge", "version": SAMPLE_VERSION,
        "genre": card.axes.genre, "pack": card.pack, "material": material,
        "narration": r.text, "expect": c.expect, "category": c.category,
        "speaker": card.material.present[0],
    })


def _fabrication_hit(summary: str, history: str) -> str | None:
    """程序先杀①：摘要的「」引用词不在历史中 → 虚构。"""
    for w in re.findall(r"「([^」]+)」", summary):
        if w not in history:
            return w
    return None


def _program_kill(summary: str, history: str, card: ScenarioCard) -> str | None:
    if len(summary) > SUMMARY_MAX_TARGET:   # 生产目标长度（compression.SUMMARY_MAX_TARGET=800）
        return "超长"
    if w := _fabrication_hit(summary, history):
        return f"虚构:{w}"
    for p in card.preserve_points:  # 程序先杀②：缺要点
        if not any(a in summary for a in p.anchors):
            return f"缺要点:{p.text}"
    return None


def _summarize_once(llm, history: str, card: ScenarioCard) -> str:
    """生产同款 compress 调用（含 `COMPRESS_MAX_TOKENS=4000` 预算；截断即弃）。"""
    text, finish = complete_checked(llm, compress_messages(card, history),
                                    purpose="compress", max_tokens=COMPRESS_MAX_TOKENS,
                                    temperature=SUMMARY_TEMPERATURE)
    if finish != "stop":
        raise ValueError("摘要截断")
    return text


def _sampling_on(card: ScenarioCard, sampling: str) -> bool:
    return sampling == "all" or (
        sampling == "long" and card.history_spec.target_tokens >= LONG_INPUT_TOKENS)


def build_compress_sample(llm, card: ScenarioCard, *, sampling: str = "off"
                          ) -> BuildResult:
    hist = verbalize_card(llm, card)
    if hist.dropped:
        return BuildResult(None, "历史演绎丢弃")
    n = CANDIDATES_N if _sampling_on(card, sampling) else 1
    cands, killed = [], []
    for _ in range(n):
        try:
            s = _summarize_once(llm, hist.text, card)
        except ValueError as e:
            killed.append(str(e))
            continue
        if why := _program_kill(s, hist.text, card):
            killed.append(why)
            continue
        cands.append(s)
    if not cands:
        return BuildResult(None, f"候选全杀: {killed}")
    best = select(llm, candidates=cands, material=hist.text,
                  preserve_points=card.preserve_points) if len(cands) > 1 else 0
    return BuildResult({
        "id": card.card_id, "module": "compress", "version": SAMPLE_VERSION,
        "genre": card.axes.genre, "input": hist.text, "output": cands[best],
        "sampling": sampling, "candidates": len(cands), "killed": killed,
        "long_input": card.history_spec.target_tokens >= LONG_INPUT_TOKENS,
        "preserve_points": [{"text": p.text, "anchors": p.anchors}
                            for p in card.preserve_points],
    })
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "build_ or compress"`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scenario_factory/assemble.py tests/test_scenario_factory.py
git commit -m "feat(factory): 样本构建三分支与 compress 拒绝采样三档（Task 6）"
```

---

### Task 7: 质量门 + card_hook 出厂门禁 + confab 人读清单（决策 16）

**Files:**
- Modify: `scripts/scenario_factory/assemble.py`（追加）
- Test: `tests/test_scenario_factory.py`（追加）

- [ ] **Step 1: 写失败测试（追加）**

```python
import re

from game_agent.worldpack import load_worldpack
from scripts.scenario_factory.assemble import (
    REPO_ROOT, BatchStats, hook_gate, quality_gate,
)


def _bai_zhi_card_text(pack):
    # 口径与 card_hook_check.CARD_FIELDS 一致；注意是 **personality**（NpcSpec 字段名），
    # 不是 card_hook_check 旧版写的 `persona`（那个字段不存在 → 恒为 None → 死字段，已修）
    spec = pack.npcs["bai_zhi"]
    return " ".join(str(spec.model_dump().get(f)) for f in
                    ("personality", "speech_style", "boundaries", "forbidden"))


def test_hook_gate_catches_confab_collision_only():
    pack = load_worldpack(REPO_ROOT / "world-packs/xianxia_wendao")
    han = re.sub(r"[^一-鿿]", "", _bai_zhi_card_text(pack))
    two = han[4:6]  # 取自角色卡的二字串 → 必撞词面
    hit = {"category": "confab", "speaker": "bai_zhi", "narration": f"他说{two}如何"}
    assert hook_gate(hit, pack)  # 撞卡 → 非空列表
    assert hook_gate({**hit, "category": "setting"}, pack) == []  # 非 confab 不查
    clean = {"category": "confab", "speaker": "bai_zhi", "narration": "齉龘塾鷟"}
    assert hook_gate(clean, pack) == []  # 生僻字串必不撞


def test_quality_gate_drop_rate():
    ok = BatchStats(built=9, dropped=1)
    assert quality_gate(ok) is None
    bad = BatchStats(built=6, dropped=4)  # 40% > 30%
    assert "丢弃率" in quality_gate(bad)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "hook or quality"`
Expected: FAIL（`ImportError: cannot import name 'BatchStats'`）

- [ ] **Step 3: 实现（追加到 assemble.py）**

```python
# ---- 质量门与出厂门禁（spec §9.2：card_hook_check 复用不重写） ----
import pathlib
import sys
from collections import Counter
from dataclasses import field as dc_field

from game_agent.worldpack import load_worldpack  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))  # card_hook_check 非包成员：脚本层 sys.path 注入
from card_hook_check import CARD_FIELDS, card_hook  # noqa: E402

GATE_MAX_DROP_RATE = 0.30  # 丢弃率超阈 → 批作废（先停产线，不硬凑量）
QUALITY_SAMPLE_RATE = 0.20  # §7.4 质检员抽检比例（常驻关卡）


@dataclass
class BatchStats:
    built: int = 0
    dropped: int = 0
    reasons: Counter = dc_field(default_factory=Counter)


def hook_gate(sample: dict, pack) -> list[str]:
    """confab 出厂门禁：narration × 说话人角色卡（四字段）词面撞 → 返回撞词（调用方丢弃）。

    依据 card_hook_check.py 文首实证：confab 撞卡会多开「设定矛盾」通路，拦截率被
    系统性抬高、跨包不可比。只查 confab；词面之外（语义撞卡、「不得由材料事实组合
    推出」）属人读清单（决策 16），见 manual_review_row()。
    """
    if sample.get("category") != "confab":
        return []
    spec = pack.npcs.get(sample["speaker"])
    card_text = (" ".join(str(spec.model_dump().get(f)) for f in CARD_FIELDS)
                 if spec else "")
    return card_hook(sample["narration"], card_text)


def quality_gate(stats: BatchStats) -> str | None:
    total = stats.built + stats.dropped
    if total and stats.dropped / total > GATE_MAX_DROP_RATE:
        return f"丢弃率 {stats.dropped}/{total} 超 {GATE_MAX_DROP_RATE:.0%}"
    return None


def manual_review_row(sample: dict) -> dict:
    """confab 人读清单行（决策 16 的不可程序化检查）：
    「断言不得由材料已有事实组合推出」只能人读——逐行核对 material 与 narration。"""
    return {"id": sample["id"], "material": sample["material"],
            "narration": sample["narration"], "expect": sample["expect"]}
```

注意：`REPO_ROOT` 与 Task 3 materialize.py 里的定义同值；若 ruff 报重复定义，改为
`from .materialize import REPO_ROOT` 并删除本段定义。`dataclass` 已在 Task 6 导入，无需重复。

- [ ] **Step 3b: 把两个门禁**接进产线**（原稿定义了却从未被调用——必须补，否则门禁等于不存在）**

1. `hook_gate` 接在 judge 样本构建处（`build_judge_sample` 内、material 装配之后）：

```python
    if hooked := hook_gate({"category": c.category, "speaker": card.material.present[0],
                            "narration": r.text}, load_pack(card.pack)):
        return BuildResult(None, f"confab 撞卡: {'/'.join(hooked)}")
```

   注意 `hook_gate` 只对 `category == "confab"` 生效（非 confab 直接返回 `[]`）。
   另：`materialize.py` 需加**带缓存的包加载**，同批上千张卡不要重复解析 YAML：

```python
_PACK_CACHE: dict[str, object] = {}


def load_pack(pack_path: str):
    """带缓存的包加载（同批 1000+ 张卡只解析一次）。"""
    if pack_path not in _PACK_CACHE:
        _PACK_CACHE[pack_path] = load_worldpack(REPO_ROOT / pack_path)
    return _PACK_CACHE[pack_path]
```

   `build_material` 内改为 `pack = load_pack(card.pack)`。

2. `quality_sample`（质检员）接在**出库前**（Task 8 的 `main` 里，见该处 Step 3）：

```python
    bad_ids = quality_sample(llm, samples, rate=QUALITY_SAMPLE_RATE)
    if bad_ids:
        print(f"[质检] 自然度 <1 剔除 {len(bad_ids)} 条（须重造）：{bad_ids[:10]}")
        drop = set(bad_ids)
        samples = [s for s in samples if s["id"] not in drop]
```

   并加一条守卫，确保"定义了必被调用"（防再次出现"门禁存在但产线不用"）：

```python
def test_pipeline_calls_both_gates():
    import inspect
    from scripts.scenario_factory import assemble
    src = inspect.getsource(assemble)
    assert "hook_gate(" in src and "quality_sample(" in src
    assert "quality_sample(llm, samples" in src  # 在 main 里真的被调
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "hook or quality"`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scenario_factory/assemble.py tests/test_scenario_factory.py
git commit -m "feat(factory): 质量门、confab 卡中性门禁与人读清单（Task 7）"
```

---

### Task 8: 配额计数 + 前缀去重 + sha256 出库 CLI（三层种子空间，spec §8）

**Files:**
- Modify: `scripts/scenario_factory/assemble.py`（追加）
- Test: `tests/test_scenario_factory.py`（追加）

- [ ] **Step 1: 写失败测试（追加）**

```python
import json

from game_agent.evalmeta import file_digest
from scripts.scenario_factory.assemble import (
    dedup, fingerprint, quota_gaps, write_layer,
)


def _rows(mod, genres, n_input="输入"):
    return [{"id": f"{mod}-{i}", "module": mod, "genre": g,
             "input": f"{n_input}{i}", "long_input": i % 2 == 0}
            for i, g in enumerate(genres)]


def test_fingerprint_prefix_sensitive():
    a = fingerprint("甲" * 64 + "尾巴A")
    b = fingerprint("甲" * 64 + "尾巴B")
    assert a == b                      # 前缀 64 字符相同 → 同指纹
    assert fingerprint("甲" * 64) != fingerprint("乙" * 64)


def test_dedup_drops_second_same_prefix():
    rows = [{"id": "1", "input": "同一段开头" * 20},
            {"id": "2", "input": "同一段开头" * 20}]
    out, dup = dedup(rows, set())
    assert len(out) == 1 and dup == 1


def test_dedup_handles_judge_samples_without_input_key():
    """judge 样本只有 narration（无 input 键）——原稿在此处 KeyError。"""
    rows = [{"id": "j1", "narration": "同一段开头" * 20},
            {"id": "j2", "narration": "同一段开头" * 20},
            {"id": "j3", "narration": "截然不同的另一段叙事"}]
    out, dup = dedup(rows, set())
    assert [s["id"] for s in out] == ["j1", "j3"] and dup == 1


def test_quota_gaps_reports_genre_skew():
    balanced = _rows("extract", ["古代武侠"] * 5 + ["仙侠"] * 5)
    assert quota_gaps(balanced) == []
    skewed = _rows("extract", ["古代武侠"] * 19 + ["仙侠"])
    assert any("仙侠" in g and "<" in g for g in quota_gaps(skewed))


def test_write_layer_manifest_sha_matches(tmp_path):
    rows = _rows("extract", ["古代武侠", "仙侠"], n_input="样例")
    manifest = write_layer("dev", rows, tmp_path)
    f = tmp_path / "dev" / "extract.jsonl"
    assert f.exists() and len(f.read_text(encoding="utf-8").splitlines()) == 2
    sha = file_digest(f)
    assert manifest["modules"]["extract"]["sha256"] == sha
    assert manifest["frozen"] is False
    ev = write_layer("eval", rows, tmp_path)
    assert ev["frozen"] is True and (tmp_path / "eval" / "OPEN_LOG.md").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "fingerprint or dedup or quota or write_layer"`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 实现（追加到 assemble.py）**

```python
# ---- 配额 / 去重 / 出库（spec §8：三层种子空间；eval 冻结纪律） ----
import argparse
import datetime
import hashlib
import json

from game_agent.evalmeta import file_digest

from .cards import generate_card, layer_of

DATA_ROOT = REPO_ROOT / "data" / "route-a"
PREFIX_N = 64                # 前缀指纹长度（字符）：防跨层泄漏与近复用
# ⚠️ 已知风险（实施时盯住丢弃率）：演绎文本开头常同形（同一 VERBALIZE_SYSTEM + 同题材），
# 64 字前缀可能把**不同样本**判成重复；而 dup 计入 dropped → 可能把丢弃率推过 GATE_MAX_DROP_RATE，
# 形成"越像越丢、越丢越像"的反馈。若"前缀去重"占 dropped 的比例异常高（>1/3），
# 改用整文 hash 作精确去重 + 二字组 Jaccard 判近重复（复用 scripts/near_dup_check.py 口径）。
GENRE_MIN_SHARE = 0.10       # 层内每题材 ≥10%（plan-phase1-data.md §4.4 轴矩阵按层计数）
LONG_MIN_SHARE = 0.20        # compress 长输入档 ≥20%（决策 18：合成/真实分列计数）
LAYER_BASE = {"train": 10000, "dev": 20000, "eval": 30000}


def fingerprint(text: str) -> str:
    return hashlib.sha256(text[:PREFIX_N].encode("utf-8")).hexdigest()[:16]


def _sample_text(s: dict) -> str:
    """跨模块取"表面文本"：extract/compress 用 `input`，judge 用 `narration`。

    （原稿只取 `s["input"]`，而 `build_judge_sample` **不产出 input 键** → judge 一跑就 KeyError。）
    """
    return s.get("input") or s.get("narration") or ""


def dedup(samples: list[dict], seen: set[str]) -> tuple[list[dict], int]:
    out, dup = [], 0
    for s in samples:
        fp = fingerprint(_sample_text(s))
        if fp in seen:
            dup += 1
            continue
        seen.add(fp)
        out.append(s)
    return out, dup


def quota_gaps(samples: list[dict]) -> list[str]:
    """配额缺口清单（空=达标）。只报告不硬杀：缺口由产线补产，不是丢样本的理由。"""
    gaps: list[str] = []
    by_mod: dict[str, list[dict]] = {}
    for s in samples:
        by_mod.setdefault(s["module"], []).append(s)
    for mod, rows in by_mod.items():
        for g, n in Counter(r.get("genre", "?") for r in rows).items():
            if n / len(rows) < GENRE_MIN_SHARE:
                gaps.append(f"{mod}/{g}: {n}/{len(rows)} < {GENRE_MIN_SHARE:.0%}")
    comp = by_mod.get("compress", [])
    if comp:
        long_n = sum(1 for r in comp if r.get("long_input"))
        if long_n / len(comp) < LONG_MIN_SHARE:
            gaps.append(f"compress 长输入档: {long_n}/{len(comp)} < {LONG_MIN_SHARE:.0%}"
                        "（合成/真实须分列计数，决策 18）")
    return gaps


def write_layer(layer: str, samples: list[dict], out_dir: pathlib.Path) -> dict:
    """出库：{layer}/{module}.jsonl + manifest.json（sha256/计数/冻结标志）。"""
    layer_dir = out_dir / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    by_mod: dict[str, list[dict]] = {}
    for s in samples:
        by_mod.setdefault(s["module"], []).append(s)
    manifest = {"layer": layer, "version": SAMPLE_VERSION,
                "written_at": datetime.date.today().isoformat(),
                "frozen": layer == "eval", "modules": {}}
    for mod, rows in sorted(by_mod.items()):
        f = layer_dir / f"{mod}.jsonl"
        f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                     encoding="utf-8")
        # 出库摘要同样走 file_digest（换行归一化）：Windows 下 write_text 会把 \n 落成 CRLF，
        # 裸 read_bytes() 摘要会让同一份数据集在不同平台得到两个 sha（与 eval-sets 那次同因）
        manifest["modules"][mod] = {
            "count": len(rows), "file": str(f.relative_to(out_dir)),
            "sha256": file_digest(f),
        }
    (layer_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if layer == "eval":  # 冻结纪律（spec §8 + plan-phase1-data.md §3.3 扩展）
        with (layer_dir / "OPEN_LOG.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- {manifest['written_at']} WRITE 出库 "
                     f"{sum(m['count'] for m in manifest['modules'].values())} 条；"
                     "此后每次打开（读取用于决策）须在此追加一行计数。\n")
        print("[eval 层已冻结] 请打 git tag：git tag eval-route-a-YYYYMMDD")
    return manifest


def _build_module(llm, module: str, count: int, seed_base: int, sampling: str,
                  stats: BatchStats, seen: set[str]) -> list[dict]:
    builders = {"extract": build_extract_sample, "judge": build_judge_sample}
    out = []
    for i in range(count):
        card = generate_card(seed_base + i, i, module)
        if module == "compress":
            r = build_compress_sample(llm, card, sampling=sampling)
        else:
            r = builders[module](llm, card)
        if r.sample is None:
            stats.dropped += 1
            stats.reasons[r.dropped_reason.split(":")[0]] += 1
            continue
        out.append(r.sample)
        stats.built += 1
    rows, dup = dedup(out, seen)
    stats.dropped += dup
    stats.reasons["前缀去重"] += dup
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="场景卡工厂出库（三层种子空间，spec §8）")
    p.add_argument("--layer", required=True, choices=["train", "dev", "eval"])
    p.add_argument("--extract", type=int, default=0)
    p.add_argument("--judge", type=int, default=0)
    p.add_argument("--compress", type=int, default=0)
    p.add_argument("--sampling", default="off", choices=["off", "long", "all"])
    p.add_argument("--seed-base", type=int, default=None,
                   help="缺省按层取 10000/20000/30000")
    p.add_argument("--out", default=str(DATA_ROOT))
    p.add_argument("--dry-run", action="store_true",
                   help="只出卡做配额预演，不调 LLM、不出库（零成本）")
    args = p.parse_args(argv)

    seed_base = args.seed_base if args.seed_base is not None else LAYER_BASE[args.layer]
    assert layer_of(seed_base) == args.layer, "seed 与层不一致（三层空间隔离，spec §8）"
    out_dir = pathlib.Path(args.out)

    if args.dry_run:  # 配额预演：零 API 成本，先看轴分布再决定产多少
        for mod, n in (("extract", args.extract), ("judge", args.judge),
                       ("compress", args.compress)):
            if not n:
                continue
            genres = Counter(generate_card(seed_base + i, i, mod).axes.genre
                             for i in range(n))
            long_n = sum(1 for i in range(n)
                         if generate_card(seed_base + i, i, mod
                                          ).history_spec.target_tokens >= LONG_INPUT_TOKENS)
            print(f"[dry-run] {mod}: {n} 卡，题材 {dict(genres)}，长输入 {long_n}/{n}")
        return 0

    from game_agent.config import load_settings
    from game_agent.llm import LLMClient
    from game_agent.usage import UsageTracker
    from scripts.rubric_judge import quality_sample

    # usage 记账：**必须显式建 tracker 并传进 LLMClient** —— 落盘只发生在
    # `LLMClient._record_usage`（llm.py:289-291），`complete_checked` 本身**不接触**
    # UsageTracker（原稿此处写反了，会导致 Task 11 的成本回填无源）。
    # 走 `from_settings` 同时带来：模型路由（judge/compress 档）+ 侧信道关思考
    # （`DEEPSEEK_DISABLE_THINKING`，长输入熔断的修复）。
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（.env）")
        return 1
    tracker = UsageTracker("reports/usage-route-a.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    stats = BatchStats()
    seen: set[str] = set()
    seen_file = out_dir / "seen_fingerprints.json"  # 跨层去重真源：train 先产，dev/eval 复用
    if seen_file.exists():
        seen |= set(json.loads(seen_file.read_text(encoding="utf-8")))
    samples: list[dict] = []
    for mod, n in (("extract", args.extract), ("judge", args.judge),
                   ("compress", args.compress)):
        if n:
            samples += _build_module(llm, mod, n, seed_base, args.sampling, stats, seen)
    if why := quality_gate(stats):
        print(f"[✗] 质量门未过：{why}——批作废，先停产线")
        return 1
    # §7.4 质检员（常驻关卡，Task 7 Step 3b）：抽检自然度，<1 剔除重造
    bad_ids = quality_sample(llm, samples, rate=QUALITY_SAMPLE_RATE)
    if bad_ids:
        print(f"[质检] 自然度 <1 剔除 {len(bad_ids)} 条（须重造）：{bad_ids[:10]}")
        bad_set = set(bad_ids)
        samples = [s for s in samples if s["id"] not in bad_set]
    for g in quota_gaps(samples):
        print(f"[配额缺口] {g}")
    manifest = write_layer(args.layer, samples, out_dir)
    seen_file.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    confab = [s for s in samples if s.get("category") == "confab"]
    if confab:  # 决策 16：人读清单随批交付
        mr = out_dir / args.layer / f"confab-manual-review-{args.layer}.jsonl"
        mr.write_text("\n".join(json.dumps(manual_review_row(s), ensure_ascii=False)
                                for s in confab) + "\n", encoding="utf-8")
        print(f"[人读清单] {mr}（{len(confab)} 条 confab，决策 16 待人工复核）")
    print(f"[✓] {args.layer} 出库 {sum(m['count'] for m in manifest['modules'].values())} 条"
          f"（丢弃 {stats.dropped}：{dict(stats.reasons)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: 跑测试确认通过 + dry-run 配额预演（零成本）**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "fingerprint or dedup or quota or write_layer"`
Expected: 5 passed

Run: `uv run python -m scripts.scenario_factory.assemble --layer train --extract 100 --judge 80 --compress 30 --dry-run`
Expected: 打印各模块卡数、题材分布、长输入占比；无 API 调用（`--dry-run` 在 `load_settings` 之前 return，故不需要 key）

- [ ] **Step 5: Commit**

```bash
git add scripts/scenario_factory/assemble.py tests/test_scenario_factory.py
git commit -m "feat(factory): 配额/去重/sha256 出库 CLI 与三层种子空间（Task 8）"
```

---

### Task 9: 轨道 2 评测跑批 + 探针校准 + 定 X（决策 13，M4 验收项）

**Files:**
- Modify: `scripts/rubric_judge.py`（追加 `run_eval` 与 `main`）
- Test: `tests/test_scenario_factory.py`（追加）

- [ ] **Step 1: 写失败测试（追加）**

```python
from scripts.rubric_judge import (
    budget_policy, probe_positions, prompt_version, run_eval,
)


def _compress_rows(n):
    return [{"id": f"c-{i}", "input": f"历史{i}：玩家的剑名为「听雨」。",
             "output": f"摘要{i}：玩家的剑名为「听雨」。",
             "preserve_points": [{"text": "玩家的剑名为「听雨」", "anchors": ["听雨"]}]}
            for i in range(n)]


def test_run_eval_injects_probes_and_validates_batch():
    rows = _compress_rows(10)
    idx = probe_positions(10, 0.2, 1)
    # 锚定抽样行为：Random(1).sample(range(10), k=2) == [1, 2]，**不是**"前 2 个"
    assert idx == [1, 2]
    good = '{"保真": 2, "简洁": 2, "结构": 2, "流畅": 2}'
    zero = '{"保真": 0, "简洁": 0, "结构": 0, "流畅": 0}'
    # 按 run_eval 的**升序消费**构造队列：探针位拿 0 分（= 检出），其余拿满分
    probe_set = set(idx)
    llm = StubLLM([zero if i in probe_set else good for i in range(10)])
    rep = run_eval(llm, rows, probe_rate=0.2, seed=1)
    assert rep["probe_indices"] == [1, 2]
    assert len(rep["probes"]) == 2 and rep["probe_detection"] == 1.0
    assert rep["batch_valid"] is True and len(rep["scores"]) == 8


def test_report_carries_provenance_fingerprints():
    """spec §6.4/§9.2：报告必须自证口径（缺一即为不合格报告）。"""
    rows = _compress_rows(4)
    good = '{"保真": 2, "简洁": 2, "结构": 2, "流畅": 2}'
    llm = StubLLM([good] * 4)
    rep = run_eval(llm, rows, probe_rate=0.25, seed=1, meta={
        "prompt_version": prompt_version(), "budget_policy": budget_policy(),
        "judge_model": "stub", "endpoint": {"model": "stub"}})
    for k in ("prompt_version", "budget_policy", "judge_model", "endpoint"):
        assert rep.get(k), f"报告缺 {k}"


def test_run_eval_invalidates_batch_when_probes_missed():
    rows = _compress_rows(10)
    good = '{"保真": 2, "简洁": 2, "结构": 2, "流畅": 2}'
    llm = StubLLM([good] * 10)  # 探针也拿高分 → 检出 0%
    rep = run_eval(llm, rows, probe_rate=0.2, seed=1)
    assert rep["batch_valid"] is False  # <90% → 批作废（spec §7.3）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k run_eval`
Expected: FAIL（`ImportError: cannot import name 'run_eval'`）

- [ ] **Step 3: 实现（追加到 rubric_judge.py）**

```python
# ---- 轨道 2 批跑（spec §7.2/§7.3：掺探针 → 打分 → 检出率门） ----
import argparse
import hashlib
import pathlib
import sys

from game_agent.config import load_settings
from game_agent.endpoint import fingerprint_for
from game_agent.llm import LLMClient
from game_agent.usage import UsageTracker

PROBE_MIN_RATE = 0.10   # 探针掺入 ≥10%
DETECT_MIN = 0.90       # 检出率 <90% → 当批成绩全部作废
PROBE_SEED = 20260912


def prompt_version() -> str:
    """rubric 提示词指纹（spec §6.4/§9.2：报告必须自证口径）。评委提示词一改即换新值。"""
    return hashlib.sha256(
        (RUBRIC_SYSTEM + PAIRWISE_SYSTEM + NATURALNESS_SYSTEM).encode("utf-8")
    ).hexdigest()[:16]


def budget_policy() -> str:
    """预算策略指纹（budgets.py 的 sha 前 12 位）——改常量即整套重测（spec §9.2 继承项）。"""
    import game_agent.budgets as _b
    return "budgets.py@" + hashlib.sha256(
        pathlib.Path(_b.__file__).read_bytes()).hexdigest()[:12]


def probe_positions(n: int, rate: float, seed: int = PROBE_SEED) -> list[int]:
    """探针抽样位置（**与 run_eval 同源**：测试据此构造夹具、报告据此留痕）。"""
    if n <= 0:
        return []
    k = min(max(1, math.ceil(n * rate)), n)
    return sorted(random.Random(seed).sample(range(n), k=k))


def _restore_points(sample: dict) -> list[PreservePoint]:
    return [PreservePoint(text=p["text"], anchors=p["anchors"])
            for p in sample.get("preserve_points", [])]


def run_eval(llm, samples: list[dict], *, probe_rate: float = PROBE_MIN_RATE,
             seed: int = PROBE_SEED, meta: dict | None = None) -> dict:
    """对 compress 样本批跑打分轨。探针替换法：被抽中的样本以其改坏版送入评委，
    成绩只计入探针检出统计，不混入干净样本的分数分布（防污染报告口径）。

    `meta`：调用方注入溯源指纹（prompt_version / budget_policy / endpoint / judge_model），
    满足 spec §6.4 的报告 schema——**缺指纹的报告不予采信**。
    """
    probe_idx = set(probe_positions(len(samples), probe_rate, seed))
    kind_rng = random.Random(seed + 1)   # 与位置抽样**解耦**：改其一不影响另一
    rows, probes = [], []
    for i, s in enumerate(samples):
        pps = _restore_points(s)
        if i in probe_idx:
            kind = kind_rng.choice(PROBE_KINDS)
            bad = make_probe(s["output"], pps, kind=kind, rng=kind_rng)
            sc = score(llm, summary=bad, material=s["input"], preserve_points=pps)
            probes.append({"id": s["id"], "kind": kind,
                           "detected": probe_detected(sc, kind)})
        else:
            sc = score(llm, summary=s["output"], material=s["input"],
                       preserve_points=pps)
            rows.append({"id": s["id"], **sc})
    det = (sum(p["detected"] for p in probes) / len(probes)) if probes else 1.0
    return {"scores": rows, "probes": probes, "probe_detection": det,
            "probe_indices": sorted(probe_idx),
            "batch_valid": det >= DETECT_MIN, **(meta or {})}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="rubric 轨道 2 批跑（spec §7）")
    p.add_argument("--samples", required=True, help="compress.jsonl（出库产物）")
    p.add_argument("--probe-rate", type=float, default=PROBE_MIN_RATE)
    p.add_argument("--report", default=None, help="报告输出路径（json）")
    args = p.parse_args(argv)
    import json as _json
    import pathlib as _pl
    samples = [_json.loads(line) for line in
               _pl.Path(args.samples).read_text(encoding="utf-8").splitlines() if line]
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（.env）")
        return 1
    tracker = UsageTracker("reports/usage-rubric.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    rep = run_eval(llm, samples, probe_rate=args.probe_rate, meta={
        "prompt_version": prompt_version(),
        "budget_policy": budget_policy(),
        "judge_model": llm.model_for("aux"),
        "endpoint": fingerprint_for(settings, "aux"),
        "sample_set": str(args.samples),
    })
    out = _json.dumps(rep, ensure_ascii=False, indent=2)
    if args.report:
        _pl.Path(args.report).write_text(out, encoding="utf-8")
    print(f"探针检出率 {rep['probe_detection']:.0%}（门 {DETECT_MIN:.0%}）；"
          f"有效样本 {len(rep['scores'])} 条 · prompt_version={rep['prompt_version']}")
    if not rep["batch_valid"]:
        print("[✗] 探针检出率不达标——当批成绩全部作废（报告已带 batch_valid=false 留痕），"
              "修评委提示词后整批重评")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "run_eval or report"`
Expected: 3 passed

- [ ] **Step 5: 定 X（决策 13 的 M4 验收项，人工+程序）**

前置：dev 层 compress 已出库（Task 8）、轨道 1 规则保全率可跑（`phase1_probe.eval_compress`
同款，spec §9.2 复用）。步骤：

1. 跑轨道 2：`uv run python scripts/rubric_judge.py --samples data/route-a/dev/compress.jsonl --report reports/rubric-eval-YYYYMMDD.json`——**批作废则先修评委提示词，不定 X**；
2. 对同一批跑轨道 1 规则保全率，逐样本对齐 id；
3. 逐样本算 `diff_i = |规则保全率_i − 保真维分_i / 2|`（轨道 2 折到 0~1），取 `diff` 分布的
   P95 向上取整到 5pp 作为 X 的候选取值；
4. 写 `reports/rubric-x-calibration-YYYYMMDD.md`，必须含三要素（决策 13 原文）：
   **X 是什么量**（轨道 1 保全率与轨道 2 保真折算值的绝对差，单位 pp）、**定在多少**
   （P95 取整值）、**依据哪批数据**（dev 批 id 范围、样本数、探针检出率、日期）；
5. 报告同步写回 spec §12.A 决策 13 行（"X 已定：……"）。

- [ ] **Step 6: Commit**

```bash
git add scripts/rubric_judge.py tests/test_scenario_factory.py reports/rubric-x-calibration-*.md docs/plan-route-a-factory.md
git commit -m "feat(factory): 轨道 2 批跑与探针校准门；定 X 报告（Task 9，决策 13）"
```

---

### Task 10: 决策 15——EXTRACT_SYSTEM 提示词收紧实验（零 API 成本，需 GPU 机）

**Files:**
- Modify: `game_agent/memory.py:50-62`（EXTRACT_SYSTEM 全文替换）
- Test: `tests/test_extract_prompt.py`（追加 1 个守卫）

背景（spec §12.A 决策 15 取证定案）：14B 在 93 个正例 run 里 38.7% 恒输出「无」——
系统性保守判定（宁可不记），是提示词能治的病，不该先用 LoRA 治。本任务收紧提示词，
复测后再定 extract 训练量（必要时砍掉该训练批）。

- [ ] **Step 1: 写失败测试（追加到 tests/test_extract_prompt.py）**

```python
def test_extract_prompt_has_decision_rule_against_conservatism():
    """决策 15（路线 A spec §12.A）：治「宁可不记」——判定式 + 「无」通路收窄。"""
    assert "判定式" in EXTRACT_SYSTEM
    assert "偏向输出" in EXTRACT_SYSTEM
    assert "仅当通篇没有任何上述内容" in EXTRACT_SYSTEM  # 「无」通路收窄
    assert "没有值得长期记住的事实就只输出" not in EXTRACT_SYSTEM  # 旧保守句已替换
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_extract_prompt.py -q`
Expected: 1 failed（新守卫），其余 4 个存量测试仍过

- [ ] **Step 3: 替换 EXTRACT_SYSTEM（memory.py:50-62）**

```python
EXTRACT_SYSTEM = (
    "你是事实提炼器。从给定的游戏回合内容中，提炼关于「玩家」的长期事实"
    "（身份身世 / 称谓与别名 / 所属与来历 / 物品与装备 / 能力与技艺 / "
    "地点与势力 / 承诺与约定 / 债务与人情 / 目标与线索 / 关系变化）。"
    "同一类别在不同题材下形态不同：装备可以是剑、光剑或旧货车，"
    "能力可以是剑术、法术或驾驶技术，所属可以是门派、公司或舰队。"
    "判定式：只要回合中出现具体专名（人名/地名/组织名/物品名）、数字或数值、"
    "承诺与约定、债务、新立的目标或线索，就必须输出对应事实；"
    "拿不准是否值得长期记住时，偏向输出。"
    "每条输出一行，格式：重要性|事实，重要性为 1~10 的整数"
    "（8-10：身份身世、生死承诺、命运级转折；5-7：重要关系进展与关键事件；"
    "1-4：日常喜好与琐事）。"
    "不要编号、不要解释；仅当通篇没有任何上述内容时才只输出「无」。"
    "日常琐事（吃了什么、天气如何）不算事实；剧情进展的瞬时状态"
    "（正在赶路、伤势已愈等）不算——但刚发生的承诺、债务、新得物品、新立目标必须记。"
    "「已有事实」中已经存在的（含近义改写）不要重复输出。"
)
```

要点：①中性枚举与跨题材示例**原样保留**（Step 0 成果不动，存量 4 个守卫不破）；
②新增判定式正面清单 + "偏向输出"反保守；③「无」通路从"没有值得记住"收窄为
"通篇没有上述内容"；④"瞬时状态"补全对照——承诺/债务/新得/新目标即使刚发生也必须记。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_extract_prompt.py tests/test_memory.py -q`
Expected: 全过（含新守卫 5 个 prompt 测试）

- [ ] **Step 5: 复测（14B 在 GPU 机；flash 侧在本机——**两侧都必须跑**）**

**先弄清脚本的真实形态**（原稿命令写错了）：`scripts/extract_eval.py` 的参数只有
`--gate / --dry-run / --limit / --temperature / --repeat` —— **没有** `--model` / `--out`；
端点与模型来自 `.env`（`DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`，见 `config.py:49-53`），
输出路径写死为 `reports/extract-eval-{时间戳}.json`（`extract_eval.py:225`）。
报告里的 `prompt_version` 由 `sha256(EXTRACT_SYSTEM)[:16]` **自动算出**（`extract_eval.py:34`），
所以改了提示词之后，新报告会自动带新值——这正是不用手工登记的原因。

**（a）14B 侧（GPU 机，零 API 成本）** —— 把 `.env` 指向本地端点后跑：

```bash
# .env 三行改为：DEEPSEEK_BASE_URL=http://127.0.0.1:8000/v1
#               DEEPSEEK_MODEL=local-14b
#               DEEPSEEK_API_KEY=EMPTY      （脚本要求 key 非空）
uv run python scripts/extract_eval.py --repeat 3 --temperature 0
```

> 若嫌改 `.env` 麻烦，可参照 `scripts/phase1_probe.py:232-233` 已有的
> `--local-base-url / --local-model` 给 `extract_eval.py` 补同款参数——那是**改脚本**，
> 属本计划范围外，需单独提交。

**（b）flash 侧（本机，约 ¥0.2）** —— **必须重测**。决策 15 明文要求"提示词改动 = 新实验 →
新 `prompt_version`，**两侧基线须重测**"；只测一侧就是"换尺子不重测 = 假对比"
（`plan-phase1-data.md` §3.5.6c 的教训）：

```bash
# 恢复 .env 指向云端后
uv run python scripts/extract_eval.py --repeat 3 --temperature 0
```

**对照基线怎么读**（原稿也引错了）：`reports/extract-eval-20260912-141241.json` 的 `summary`
是**极性修正前**的口径（`recall 42/96`、`eval_set_sha256=c4214adf…` —— 该值正是修正前那份
`direct.yaml` 的**归一化（LF）摘要**，与 Windows 侧记录的 `9a552245cd9a` 是同一文件），
**并不含** 45.2%（42/93）与"11 负例"——那两个数来自 spec §12.C 决策 20 的**离线重切**
（分母 93 = 31 正 × 3，负例 33 = 11 × 3）。做对照时按新口径比，**不要直接读那个 JSON 的 summary**。

**验收判据**（spec 决策 15 封板口径；1、2 两条**同时**成立才算达标）：

1. **C 类（恒输出「无」）run 数**：从 36/93 降到**原来的 1/3 以下（≤12 run）**；
2. **负例不得退化**：现 33/33 必须仍为 33/33 ——"偏向输出"要防的正是把负例一起污染掉；
3. 参考项（不计入判据）：正例 recall 应自 45.2%（42/93）显著回升。

达标 → 按决策 15 进入"再定训练量 / 必要时砍掉 extract 训练批"；不达标 → 回到"必训"。
结果与两侧对照表写入 `reports/`（两侧报告各自带新 `prompt_version`），
并在 spec §12.C 追加一行已执行记录。

- [ ] **Step 6: Commit**

```bash
git add game_agent/memory.py tests/test_extract_prompt.py
git commit -m "feat(memory): EXTRACT_SYSTEM 判定式收紧（Task 10，决策 15 实验）"
```

---

### Task 11: 总装验收 + 成本回填 + 文档回写

**Files:**
- Modify: `docs/plan-route-a-factory.md`（§12.C 追加执行记录）
- Create: `reports/route-a-cost-YYYYMMDD.md`

- [ ] **Step 1: 全量测试**

Run: `uv run pytest -q`
Expected: **341 存量** + 本计划新增 **52 个守卫**（Task 1~9：9+7+5+5+6+9+3+5+3）+ Task 10 的 1 个
prompt 守卫 = **394 全绿**

> 计数口径（2026-09-12 实测）：`pytest --collect-only -q` 当前 **341**（原稿写 279，是 Step 0 之后的旧数）；
> 计划枚举的守卫数逐个数为 52（原稿写 30 也不对）。Task 2 加了 anchors 不相交 + detail 可解析
> 两条守卫（执行时发现的两处真缺陷）、Task 3 的夹具拆分把 4 变 5、Task 4 加了 confab 反向校验守卫、
> Task 5 加了质检员、Task 6 加了三条模板/负例守卫、Task 8 加了 judge 去重守卫、Task 9 加了报告指纹守卫。

- [ ] **Step 2: M1~M4 验收项核对（对照 spec §10.3 逐项打勾）**

| 里程碑 | 验收项 | 证据 |
| --- | --- | --- |
| M1 | 卡 schema + 生成器 + 守卫测试 + 卡面出厂门禁 | Task 1/2 测试绿；**`python scripts/card_hook_check.py --gate`** 通过（注意：无 `[project.scripts]` 入口，不能写成 `card_hook_check --gate`；且它扫的是 `world-packs/` 的既有语料——**工厂卡的等价门禁是 `hook_gate`（Task 7），已接进 `build_judge_sample`**，两者口径同源） |
| M2 | 演绎器 + 要素校验 + 材料装配器（extract/judge） | Task 3/4 测试绿；校验①②③有对应用例 |
| M3 | compress 演绎 + 拒绝采样 + rubric 评委 | Task 5/6 测试绿；三档调用数断言在案 |
| M4 | 轨道 2 + 探针校准 + 三层出齐 + 出库 + **定 X** | Task 8/9；`rubric-x-calibration-*.md` 三要素齐 |

- [ ] **Step 3: 成本回填**

从 usage 记账汇总实际 token —— **记账来源已修正**：`complete_checked` **不接触** `UsageTracker`，
落盘只发生在 `LLMClient._record_usage`，且**只有构造时显式传了 `tracker=` 才有数据**
（该修法已落到 Task 8/9 的 `from_settings(..., tracker=UsageTracker("reports/usage-route-a.jsonl"))`）。

写 `reports/route-a-cost-YYYYMMDD.md`：与 spec §10.2 估算（默认档 **~¥62**）并列对照；
**超 ¥90 触发停批复盘**（spec §10.2 门限不变）。报告须附 `prompt_version` / `budget_policy` /
`endpoint` 三个指纹（spec §6.4）。

- [ ] **Step 4: 文档回写 + Commit**

spec `docs/plan-route-a-factory.md` §12.C 追加：M1~M4 完成日期、各层出库 sha256、
X 取值报告路径、决策 15 复测结论。然后：

```bash
git add docs/plan-route-a-factory.md reports/route-a-cost-*.md
git commit -m "docs(factory): M1~M4 验收与成本回填（Task 11）"
```

---

## 自检（写作技能要求）

**1. spec 覆盖**：

| spec 条款 | 落地任务 |
| --- | --- |
| §3 场景卡 schema / §3.3 校验 | Task 1 |
| §8 三层种子空间 + 决策 19 dev 孪生轴 | Task 2（生成器）+ Task 8（出库） |
| §4 演绎器 / §4.1 校验①②③ | Task 4 / Task 3 |
| §6 拒绝采样三档 + 程序先杀 | Task 6 |
| §7.2 rubric 四维 / §7.3 校准协议 | Task 5 / Task 9 |
| §9.1 五个新组件 | Task 1~8 全部落名 |
| §9.2 card_hook_check 复用 | Task 7 —— **注意口径**：spec 写的是"卡出厂跑 `scripts/card_hook_check.py --gate`"，但那个 CLI 扫的是 `world-packs/` 的既有语料，**扫不到工厂产出的卡**；工厂的等价门禁是 import 其 `card_hook` 函数的 `hook_gate`（逐条卡判），CLI 只在 M1 验收时另行确认既有语料仍中性 |
| §10.3 M1~M4 | Task 11 核对表 |
| 决策 13（X 的度量与取值） | Task 9 Step 5 |
| 决策 15（extract 提示词收紧） | Task 10 |
| 决策 16（confab 人读清单） | Task 7 `manual_review_row` + Task 8 main 交付 |
| 决策 18（长输入档分列计数） | Task 6 `long_input` 字段 + Task 8 `quota_gaps` |
| **spec §4.2 输入模板逐字对齐生产**（§9.2 继承项） | Task 6 `extract_messages` / `compress_messages` + 3 条契约守卫 |
| **spec §4.2.1 负例 ≥15%（含去重纪律型）** | Task 2 `seq%10` 出两种负例 + Task 6 负例守卫 |
| **spec §6.4 报告 schema（指纹四件）** | Task 9 `prompt_version` / `budget_policy` / `endpoint` / `judge_model` + 守卫 |
| **spec §7.4 数据质检员（自然度抽检 20%）** | Task 5 `naturalness` / `quality_sample` + Task 7 Step 3b 接线 + Task 8 `main` |
| **spec §9.2 usage 记账** | Task 8/9 显式 `UsageTracker` 传入 `from_settings` |

**2. 占位符扫描**：无 TBD/TODO；唯一人工步骤（Task 9 Step 5 定 X、Task 10 Step 5 GPU 复测）
均给出确切命令与验收口径——这两步本质需要真机/人读，已在步骤内写明交付物。

**3. 类型一致性**：`PreservePoint(text, anchors)`（Task 1）在 Task 5/6/9 一致；
`verbalize_card(llm, card) -> VerbalizeResult`（Task 4）在 Task 6 一致；
`score/select/pairwise/make_probe/probe_detected/quality_sample/naturalness`（Task 5）在
Task 6/7/9 签名一致；`probe_positions/prompt_version/budget_policy/run_eval`（Task 9）在测试一致；
`extract_messages/card` 与 `compress_messages/card`（Task 6，**公开**）供测试直接调；
`load_pack(pack_path)`（Task 3 追加，带缓存）在 Task 6 `build_judge_sample` 的门禁接线处使用；
`complete_checked(llm, msgs, purpose=, max_tokens=, temperature=) -> (text, finish)`
（Task 4 已建立）在 Task 5/6 一致；`BuildResult(sample, dropped_reason)`（Task 6）
在 Task 8 `_build_module` 一致；`StubLLM`（Task 4 测试段，**末条粘滞**）在 Task 5/6/9 复用。

**4. 产线接线自检（原稿最大的一处缺口）**：门禁/质检**定义了必须被调用**——
`hook_gate` 接进 `build_judge_sample`、`quality_sample` 接进 `main`，
由 `test_pipeline_calls_both_gates` 守卫。原稿定义了 `hook_gate` 却从未调用，等于门禁不存在。

**已知边界（实施时遵守，不算缺口）**：
- `_judge_card()` 夹具的 `material.memories` 缺省为空 dict——MaterialSpec 须允许；
- Task 6 测试 `_judge_card()` 的 `pack` 指向真实包 xianxia_wendao（与 Task 3 先例一致）；
- Task 10 的 GPU 复测不阻塞 M1~M4（spec 上游依赖声明）。

---

## 交接

计划完成并保存到 `docs/plan-route-a-impl.md`（遵循项目 `docs/plan-*.md` 约定）。
两种执行方式：

**1. Subagent-Driven（推荐）**——每个 Task 派一个全新 subagent 执行，任务间我做两阶段审查，迭代快；

**2. Inline 执行**——用 executing-plans 技能在本会话内分批执行，检查点处暂停给你审。

选哪种？

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

## 进度（执行侧维护：**每完成一个 Task 就更新本表 + 勾掉该 Task 的 Step 复选框 + 补执行记录**）

| Task | 状态 | 提交 | 与计划的偏差 |
| --- | --- | --- | --- |
| 1 卡 schema + §3.3 校验 | ✅ 完成 | `d0b7195` | 发现并修正 3 处计划缺陷（见 Task 1 执行记录） |
| 2 轴空间 + 确定性生成器 | ✅ 完成 | `f81a40b`（+ 评审补修，见执行记录） | 共修正 **6 处**计划/实现缺陷（执行 3 + 评审 3） |
| 3 材料装配器 + 校验①②③ | ✅ 完成 | 见 Task 3 执行记录 | 修正 2 处（`-k material` 过滤器；好感覆写静默跳过 → 坏标签） |
| 4 演绎器 + 反向校验 | ✅ 完成 | 见 Task 4 执行记录 | 修正 1 处（自然化指令对 confab 原稿是反的） |
| 5 rubric 评委四模式 | ✅ 完成 | 见 Task 5 执行记录 | 修正 1 处（`-k` 过滤器；顺带审计了全部 Task 的过滤器） |
| 6 样本构建三分支 + 拒绝采样 | ✅ 完成 | 见 Task 6 执行记录 | 修正 1 处（compress 样本存裸文本；评审补修） |
| 7 质量门 + 门禁接线 + 人读清单 | ✅ 完成 | 见 Task 7 执行记录 | 修正 1 处（守卫顺序：`test_pipeline_calls_both_gates` 移到 Task 8） |
| 8 配额/去重/出库 CLI | ✅ 完成 | 见 Task 8 执行记录 | ⚠️ 遗留一项：§4.4 轴矩阵未全量落实（见执行记录） |
| 9 轨道 2 批跑 + 定 X | ✅ 代码完成 / ⏸ Step 5 待真机 | 见 Task 9 执行记录 | 无计划缺陷；定 X 需 dev 层出库数据 |
| 10 EXTRACT_SYSTEM 收紧实验 | ✅ 完成 / ❌ **未达标（改动已撤回）** | `f65f84f`（证据；引擎零净改动） | 判据②两侧皆败 → 按封板口径回"必训"；新增对照工具 1 个（计划外，见执行记录） |
| 11 总装验收 + 成本回填 | ✅ 完成 / ⚠️ M3·M4 带遗留（6 个发现待修） | `3b9c9ce`（证据）+ 文档提交 | 「定 X」因轨道 2 批作废**未完成**；生产批量未跑；发现①~⑥ 只记录未修 |

**Task 外的既有改动（已落地，供后续 Task 参考）**

- `5fc17ad` **冻结摘要换行归一化**：新增 `game_agent/evalmeta.file_digest`
  （读字节 → `\r\n`/`\r` 归一为 `\n` → sha256），四处调用点同源
  （`build_eval_sets` / `test_eval_frozen` / `extract_eval` / `dedup_test`）。
  起因：原摘要随 `core.autocrlf` 变化 → 冻结守卫在 Linux 上必红、报告 sha 跨机不可对账。
  `digests.json` 已按新口径重算（`direct.yaml` = `b3d918e53d90…`）。
- `15096de` 计划文档同步（Task 2/4/8/10/11 的条款与计数）。
- `aa78893` Task 3 Step 5 由"顺手修 spec"改为"核验 spec 已修订"（spec 修正已先行落地）。

**测试基线**（`pytest -q`）：存量 **342**（含 card_hook 守卫 1）+ Task 1 守卫 **9** +
Task 2 守卫 **10** + Task 3 守卫 **8** + Task 4 守卫 **5** + Task 5 守卫 **6** +
Task 6 守卫 **10** + Task 7 守卫 **2** + Task 8 守卫 **6** + Task 9 守卫 **3** +
`evalmeta` 换行守卫 **1** + 对照工具 `extract_compare` 守卫 **5** +
Task 11 守卫 **10**（用途标签 2 + 成本回填 4 + 定 X 4）= **417 passed**。

> **Task 10 的 1 条 prompt 守卫已随实验未达标一并撤回**（连同 `EXTRACT_SYSTEM` 改动本身）——
> 未达标物不得留在引擎里；撤回依据见 `reports/extract-task10-report.md`。
> `scripts/extract_compare.py` 是实验副产品（同集对照 / 离线重切片，零 API），保留并自带 5 条守卫。
> **Task 11 新增守卫的去向**：`tests/test_scenario_factory.py` +2（用途标签）、
> 新文件 `tests/test_route_a_cost.py` 4 条、新文件 `tests/test_rubric_x_calibrate.py` 4 条。

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
| `game_agent/memory.py` | Task 10 曾改 `EXTRACT_SYSTEM`（决策 15 实验）——**实验未达标、改动已撤回，引擎净改动为零**（`reports/extract-task10-report.md`） |

**分层纪律**：`game_agent/`（引擎包）只被 import，除 Task 10 的实验（**已撤回、净改动为零**）外零改动；工厂全部住 `scripts/`。`card_hook_check` 不是包成员，由 `assemble.py` 用 `sys.path` 注入 `scripts/` 后 import（脚本层先例见 `scripts/diag_turn.py:17`；**tests 层不用此法**——测试里"导入非包脚本"的既有惯例是 `importlib.util.spec_from_file_location`，见 `tests/test_card_hook_check.py:17-19`；本计划的测试只 import `scripts.scenario_factory.*`，故不涉及）。

---

### Task 1: 包骨架 + 场景卡 Schema（pydantic 模型 + §3.3 校验规则）

**Files:**
- Create: `scripts/scenario_factory/__init__.py`
- Create: `scripts/scenario_factory/cards.py`
- Test: `tests/test_scenario_factory.py`

- [x] **Step 1: 写失败测试**

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

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: FAIL（`ModuleNotFoundError: scripts.scenario_factory`）

- [x] **Step 3: 实现 cards.py 模型部分**

`scripts/scenario_factory/__init__.py`：`"""场景卡数据工厂（spec: docs/plan-route-a-factory.md）。"""`

`scripts/scenario_factory/cards.py`：

```python
"""场景卡：卡即标签——真值由程序拥有，LLM 只做表面演绎（spec §3）。"""
from __future__ import annotations

import pathlib
import random
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

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

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: 9 passed

- [x] **Step 5: Commit**

```bash
git add scripts/scenario_factory tests/test_scenario_factory.py
git commit -m "feat(factory): 场景卡 schema 与 §3.3 校验规则（Task 1）"
```

> **执行记录（2026-09-12）✅ 完成** —— 提交 `d0b7195`
>
> - Step 2 确认失败 ✓：`ModuleNotFoundError: No module named 'scripts.scenario_factory'`
> - Step 4 确认通过 ✓：**9 passed**；全量 342 → **351 passed**
> - 落地文件：`scripts/scenario_factory/__init__.py`、`cards.py`（119 行）、
>   `tests/test_scenario_factory.py`
>
> **执行中发现并修正的 3 处计划缺陷**（都会让 Step 4 直接红，已同步进本文档）：
>
> 1. **本 Task 的测试导入行引用了尚不存在的符号**：原写 `import ScenarioCard, generate_card, layer_of`，
>    而 `generate_card` 要到 Task 2 才落地 → 实际会是 collection error，不是"9 passed"。
>    已改为只导入 `ScenarioCard`，并注明由 Task 2 扩行（**扩行须保留 `ScenarioCard`**，见 Task 2）。
> 2. **Task 2 的测试块原本完全没有导入行** —— 用到 `generate_card`/`layer_of`/`PACK_BY_GENRE`
>    却一个都没 import。已补。
> 3. `test_extract_card_accepts_existing_for_dedup_discipline` **漏了 `ScenarioCard(...)` 包装**
>    （直接对 dict 取 `.existing` → `AttributeError`）。测试与计划已同步修正。

---

### Task 2: 轴空间 + 确定性生成器（含 dev 孪生，决策 19）

**Files:**
- Modify: `scripts/scenario_factory/cards.py`（追加）
- Test: `tests/test_scenario_factory.py`（追加）

- [x] **Step 1: 写失败测试（追加到测试文件）**

```python
# 本 Task 起把 Task 1 的那行导入**扩为**下面这行（保留 ScenarioCard，追加四个符号）：
from scripts.scenario_factory.cards import (
    GENRE_EVAL_ONLY,
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


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_judge_genre_never_leaks_holdout_and_pack_must_exist():
    """judge 分支重映射的两条硬约束（原版只按 PACK_BY_GENRE 的键随机取 → 两条都破）：

    1. **留出轴只对 eval 开放**（决策 19）：含留出轴的候选会让 ~14% 的 train judge 卡
       变成「民国谍战」；
    2. **包必须存在**：映射到 G1（待造）的卡在材料装配时必炸。
    """
    for layer, base in (("train", 10000), ("dev", 20000), ("eval", 30000)):
        for seq in range(60):
            card = generate_card(base + seq, seq, "judge")
            if layer != "eval":
                assert card.axes.genre != GENRE_EVAL_ONLY, card.card_id
            assert (REPO_ROOT / PACK_BY_GENRE[card.axes.genre]).is_dir(), card.card_id


def test_judge_recent_carries_no_fact_anchor():
    """`material.recent` 不得带本卡任何事实的 anchor（原版 n2/n3 正是 facts[0] 的槽）。"""
    for seq in range(60):
        card = generate_card(10231, seq, "judge")
        anchors = [a for f in card.facts for a in f.anchors]
        assert not any(a in card.material.recent for a in anchors), card.card_id


def test_corruption_new_value_does_not_collide_with_card_entities():
    """corruption 的「新值」不得落在任何事实的文本/anchors 里，否则材料里原值与新值并存、
    "材料说 A、叙述说 B"的陷阱被稀释（原版 20% 的 setting 卡中招）。"""
    seen = 0
    for seq in range(60):
        card = generate_card(10231, seq, "judge")
        cor = card.corruptions[0]
        if cor.category != "setting":
            continue
        seen += 1
        quoted = re.findall(r"「([^」]+)」", cor.detail)
        assert quoted[0] != quoted[1], card.card_id
        for f in card.facts:
            assert quoted[-1] not in f.text, f"{card.card_id}: {quoted[-1]} 已在事实文本里"
    assert seen, "样本里没有 setting 卡"
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "layer or generate or axis"`
Expected: FAIL（`ImportError: cannot import name 'generate_card'`）

- [x] **Step 3: 实现生成器（追加到 cards.py）**

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


def judge_genres_for(layer: str) -> list[str]:
    """可出 judge 卡的题材 = **有真实包可物化**的题材（§4.1）+ 留出轴纪律（决策 19）。

    两条约束缺一不可（原版只写 `rng.choice(list(PACK_BY_GENRE))`，两条都破）：

    1. **包必须存在**：材料要由真实包物化，映射到 G1（待造）只会在装配时炸 —— 出了卡也是废卡；
    2. **留出轴只对 eval 开放**：`PACK_BY_GENRE` 含留出轴「民国谍战」，放进 train/dev 的候选
       会让约 **14%** 的 train judge 卡变成留出轴。**G1 就绪前 eval 也拿不到**（第 1 条已滤掉）
       —— judge 侧的留出轴覆盖等 G1。
    """
    avail = [g for g, p in PACK_BY_GENRE.items() if (REPO_ROOT / p).is_dir()]
    return avail if layer == "eval" else [g for g in avail if g != GENRE_EVAL_ONLY]

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
# 10 个：前 8 给事实（4 事实 × 2 槽），后 2 给情节骨架/检索上下文 —— **两段不得重叠**
_NAME_POOL = ["听雨", "白鸮", "断刃崖", "灰雀号", "密码本", "环宇", "旧书店", "青瓷",
              "沈砚", "罗九"]
FACT_SLOTS = 8          # 事实占用 pool[0:FACT_SLOTS]，其余留给情节骨架
RECENT_TEXT = "玩家近日独自打理杂物，未与旁人来往"   # 材料检索上下文：**去专名**


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
    """整池打乱（10 个）：每张事实独占 2 个槽（4 事实 × 2 = 8），保证 anchors 互不相交；
    余下 2 个槽专供情节骨架/检索上下文 —— 早先版让它们复用 pool[0]/pool[1]，
    等于撞上 facts[0] 的槽。"""
    pool = list(_NAME_POOL)
    rng.shuffle(pool)
    return pool


def generate_card(seed: int, seq: int, module: str) -> ScenarioCard:
    """确定性生成：同 (seed, seq, module) 必出同一张卡。轴值按层开放（§8）。"""
    layer = layer_of(seed)
    rng = random.Random(f"{seed}:{seq}:{module}")
    axes = _axes_for(layer, rng)
    pool = _names(rng)
    n2, n3 = pool[FACT_SLOTS], pool[FACT_SLOTS + 1]   # 情节骨架专用槽：**不与任何事实槽重叠**
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
        # 候选题材 = 有包可物化 + 留出轴只对 eval 开放（决策 19；见 judge_genres_for）
        cands = judge_genres_for(layer)
        if not cands:
            raise ValueError(
                "没有任何可物化的世界包，无法出 judge 卡——先造 G1 或补 PACK_BY_GENRE")
        genre = axes.genre if axes.genre in cands else rng.choice(cands)
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
                present=[npc], affections={npc: 45}, recent=RECENT_TEXT),
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

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: 19 passed（Task 1 的 9 + Task 2 的 10）

- [x] **Step 5: Commit**

```bash
git add scripts/scenario_factory/cards.py tests/test_scenario_factory.py
git commit -m "feat(factory): 轴空间/种子空间与确定性生成器（Task 2）"
```

> **执行记录（2026-09-12）✅ 完成** —— 提交 `f81a40b`
>
> - Step 2 确认失败 ✓：`ImportError: cannot import name 'generate_card'`
> - Step 4 确认通过 ✓：**16 passed**（Task 1 的 9 + Task 2 的 7）；全量 351 → **358 passed**
> - 落地：`cards.py` 追加 136 行（轴空间 / `NPC_BY_PACK` / `_axes_for` / `_names` /
>   `generate_card`），`tests/test_scenario_factory.py` 追加 7 个守卫
>
> **执行中发现并修正的 3 处计划缺陷**（都会造成**静默良率损失**，已同步进本文档）：
>
> 1. **compress 分支同时传 `axes=` 与 `**base`**（`base` 已含 `axes`）→
>    `TypeError: got multiple values for keyword argument 'axes'`。
>    改为"先改 `base` 再解包"。
> 2. **confab 的 `detail` 把整条 `text` 引进去** → `text` 自带「」造成**嵌套引用**，
>    Task 4 的正则 `「([^」]+)」` 会解出残缺串 → **该卡恒被丢弃**；
>    而 confab 占 judge 配额 **≥40%**，是重大静默损失。改为只引被断言的 anchor 一个「」，
>    并加守卫 `test_corruption_detail_quotes_are_parseable` 钉住
>    （setting 两引 / confab 一引 / ooc 无引）。
> 3. **事实模板共用 `{n1}`** 会让同一张卡上两张事实带同一 anchor →
>    setting 卡的「原 anchor 不得出现」反向校验**永远不成立**，该类卡同样永久产出不了样本。
>    改为每张事实独占 2 个专名槽，并加守卫 `test_fact_anchors_are_pairwise_disjoint`。
>
> **连带发现**：第 2 条暴露出 **Task 4 对 confab 的逻辑本身自相矛盾**
> （同一条 anchor 既要求"在位"、又列为"不得出现"）——已在 Task 4 预先修正
> `_corruption_swap` 为按 category 返回 `(下标, 须在位, 须缺席)`，并补齐 confab 守卫。
> 执行 Task 4 时按修正后的版本走即可。
>
> **评审补修（2026-09-12，第二轮）✅ 已修** —— 由外部评审指出、经实测确认的 3 处缺陷，
> 均已修 + 补守卫（守卫由 7 → **10**，全量 359 → **362 passed**）：
>
> 4. **judge 分支的题材重映射会把留出轴漏进 train/dev**：原 `rng.choice(list(PACK_BY_GENRE))`
>    的候选含留出轴「民国谍战」，实测 **train 27/200 = 14%、dev 25/200 = 12%** 的 judge 卡
>    变成留出轴 → **违反决策 19**（`test_eval_only_axis_never_leaks_to_train_dev` 只测了
>    extract 路径，没兜住 judge 分支）。**连带**：映射目标是 `world-packs/G1_republic_spy`，
>    而该包**待造** → 这批卡走到材料装配必炸（好在 Task 3 才发现，等于白产）。
>    修法：新增 `judge_genres_for(layer)` —— 候选**先按包是否存在过滤**，再对非 eval 层
>    剔除留出轴；G1 就绪前 eval 也拿不到留出轴（无包则无法物化，出了卡也是废卡）。
>    守卫 `test_judge_genre_never_leaks_holdout_and_pack_must_exist`。
> 5. **`material.recent` 撞 `facts[0]` 的专名**：`n2, n3 = pool[0], pool[1]` 恰是 facts[0]
>    的独占槽（每事实 2 槽，k=0 → pool[0]/pool[1]），实测 25/30 的 judge 卡 recent 含
>    facts[0] 的 anchor。**注意机理**：`recent` **不渲染进材料**（只作 `rank_facts` /
>    `select_lore` 的打分输入，`context.py:181/202`），所以**不会**触发 Task 3 的校验③泄漏、
>    也不会成批报废 confab 卡；真后果是**污染检索命中 + 语义不自洽**。
>    修法：`_NAME_POOL` 扩到 **10** 个（`FACT_SLOTS = 8`：事实占前 8，情节骨架用后 2），
>    `recent` 改为**去专名**常量 `RECENT_TEXT`。
>    守卫 `test_judge_recent_carries_no_fact_anchor`。
> 6. **setting 卡的改写「新值」撞事实原文**（第 5 条的同源后果，评审未点出、实测发现）：
>    新值取 `n3 = pool[1]`，而 facts[0] 若恰为「承诺与约定」模板（同时用 n0 与 n1），
>    该新值**本来就写在 facts[0].text 里** → 材料中"原值"与"新值"并存，
>    "材料说 A、叙述说 B"的陷阱纯度被稀释。实测 **8/40 = 20%** 的 setting 卡中招
>    （样例：`facts[0].text='玩家答应把青瓷转交给密码本'` 而新值就是 `'密码本'`）。
>    修法同第 5 条（新值与事实槽彻底分离）。
>    守卫 `test_corruption_new_value_does_not_collide_with_card_entities`。

---

### Task 3: 材料装配器 materialize.py（含 recent，校验①②③）

**Files:**
- Create: `scripts/scenario_factory/materialize.py`
- Test: `tests/test_scenario_factory.py`（追加）

- [x] **Step 1: 写失败测试（追加）**

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


def test_present_npc_without_affection_track_raises():
    """present NPC 必须在包的 `schedule.affections` 里（评审指出、已实测）。

    否则 `status_text` 会：① 好感行**整体略过**该 NPC；② 在场角色卡的**语气档**按
    `state.affections.get(npc.id, 0.0)` 兜底 —— 材料"卡里声明过 45、却按 0 档渲染语气"，
    与卡片意图矛盾 = **坏标签**（T2 语气冲突卡的整条通路就是好感档）。
    用假包做**单元级前置校验**：真实三包目前都满足 `npcs ⊆ affections`，构造不出来。
    """
    pack = SimpleNamespace(npcs={"ghost": SimpleNamespace(name="鬼")},
                           schedule=SimpleNamespace(affections={"real": None}))
    card = _setting_card()
    card.material.present = ["ghost"]
    with pytest.raises(MaterializeError, match="好感轨"):
        _precheck(card, pack)


def test_material_affection_override_must_be_declared():
    """卡声明的好感覆写必须能落地：旧实现 `if k in state.affections:` 会**静默跳过**。"""
    card = _setting_card()
    card.material.affections = {"nobody_in_schedule": 45.0}
    with pytest.raises(MaterializeError, match="没有好感轨"):
        build_material(card)


def test_judge_npc_mapping_targets_have_affection_tracks():
    """`NPC_BY_PACK` 的映射目标必须在对应包的好感轨与角色卡里 ——
    否则整批该题材的 judge 卡都会被 `_precheck` 丢弃（静默良率归零）。"""
    for nick, npc in NPC_BY_PACK.items():
        pack_dir = REPO_ROOT / "world-packs" / nick
        if not pack_dir.is_dir():
            continue                     # G1 待造，跳过
        pack = load_worldpack(pack_dir)
        assert npc in pack.npcs, f"{nick} 的映射目标 {npc} 没有角色卡"
        assert npc in pack.schedule.affections, f"{nick} 的映射目标 {npc} 没有好感轨"
```

注：测试用真实包 `xianxia_wendao`（仓库先例：`tests/test_second_worldpack.py` 同法）。`material.facts` 省略（None）→ 缺省取 `in_material=true` 下标。**spec 侧已先行改好并提交**（详见 Step 5 的表：A.2/A.3 的 `facts: []` 行已删、`recent` 已字符串化、§3.2 已补 `facts: null` 语义注释），故本 Task **不需要再动 spec**——Step 5 只做核验（`git diff` 应为空）。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: FAIL（`ModuleNotFoundError: scripts.scenario_factory.materialize`）

> **为什么本 Task 不用 `-k` 过滤**（执行时实测）：`-k material` **两头都不准** ——
> 既漏掉 Task 3 的 `test_present_npc_must_be_in_pack`（名字里没有 material），
> 又混进 Task 1 的 `test_setting_requires_in_material_true` 与
> `test_judge_requires_pack_material_and_single_corruption`（命中 6 个 = Task 3 的 4 个 + Task 1 的 2 个）。
> 故改为跑整文件 + 看**累计**数。

- [x] **Step 3: 实现 materialize.py**

```python
"""材料装配器：卡 → GameState → ContextBuilder.status_text（§4.1，生产同形）。

**硬纪律**：必须复用生产组装器（`ContextBuilder`），不得另写材料模板 —— 与
`plan-phase1-data.md` §4.2.2 开头"输入模板必须逐字对齐生产调用"是同一条纪律，只是它落在材料侧。
复用组装器只保证"组装器"同形；**"输入"同形**由本模块负责：

- `material.recent`（决策 17）：`status_text` 的 `rank_facts` / `select_lore` 都吃
  `context = scene + node.goal + recent`（`context.py:181`），生产的 recent 来自最近 2 条
  真实玩家发言；工厂不给就会让**排序输入与生产不同形**；
- `material.facts`（下标）：`None` = 缺省取 `facts[in_material=true]`；`[]` = 显式一条不写。

三道校验（§4.1）：① 在场角色卡确落材料；② in_material 事实的 anchors **全部**在位；
③ confab 的 anchors **不得**出现在材料中（泄漏即等于给判官开出第二条通路，退回踩坑 #17 的老坑）。
"""
from __future__ import annotations

from game_agent.context import ContextBuilder
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import WorldPack, load_worldpack

from .cards import REPO_ROOT, ScenarioCard   # REPO_ROOT 复用 cards 的定义，不重复定义


class MaterializeError(ValueError):
    """材料装配或校验失败——调用方丢弃该样本并计数（§4.1）。"""


_PACK_CACHE: dict[str, WorldPack] = {}


def load_pack(pack_path: str) -> WorldPack:
    """带缓存的包加载：同批上千张卡只解析一次 YAML（否则每张卡都重读全部 yaml）。
    Task 7 的门禁接线与 Task 8 的出库都用这个入口。"""
    if pack_path not in _PACK_CACHE:
        _PACK_CACHE[pack_path] = load_worldpack(REPO_ROOT / pack_path)
    return _PACK_CACHE[pack_path]


def build_material(card: ScenarioCard) -> str:
    """卡 → 材料文本（与生产 `status_text` 逐字同形）。失败抛 MaterializeError。"""
    if not card.pack or not card.material:
        raise MaterializeError("judge/compress 卡必须带 pack + material")
    pack = load_pack(card.pack)
    m = card.material
    _precheck(card, pack)          # 包级前置：错就早抛，不做无用的组装
    state = GameState.from_pack(pack)
    state.day, state.scene = m.day, m.scene
    state.present_npcs = list(m.present)
    for k, v in m.affections.items():
        state.affections[k] = float(v)   # _precheck 已保证键存在，不再静默跳过
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
    _check_text(card, pack, text)
    return text


def _precheck(card: ScenarioCard, pack: WorldPack) -> None:
    """包级前置校验：这些错**无法在材料里表达**，必须早抛 —— 否则静默产出坏标签。

    背景（评审指出、已实测）：旧实现把好感覆写写成 `if k in state.affections:`，
    而 `state.affections` 只含 `schedule.yaml` 列出的对象（`state.py:103`）——
    于是"卡要 45、包没有该好感轨"时**静默跳过**，而 `status_text` 用
    `state.affections.get(npc.id, 0.0)` 渲染在场角色卡的语气档，结果材料会
    **声明过 45 却按 0 档渲染语气**，同时把该 NPC 从好感行里整体略过
    —— 材料与卡片意图矛盾 = 坏标签（T2 语气冲突卡的整条通路就是好感档）。
    """
    m = card.material
    for npc_id in m.present:
        if npc_id not in pack.npcs:
            raise MaterializeError(f"present 的 NPC 不在包里: {npc_id}")
        if npc_id not in pack.schedule.affections:
            raise MaterializeError(
                f"present 的 NPC 没有好感轨（schedule.affections 未声明）: {npc_id}——"
                "语气档会按 0.0 兜底、好感行会略过它，材料与卡片意图不符")
    undeclared = [k for k in m.affections if k not in pack.schedule.affections]
    if undeclared:
        raise MaterializeError(
            f"material.affections 声明了包里没有好感轨的对象: {undeclared}——"
            "该覆写无法落地，语气档会按 0.0 兜底渲染")


def _check_text(card: ScenarioCard, pack: WorldPack, text: str) -> None:
    m = card.material
    if not text.strip():
        raise MaterializeError("材料为空")
    for npc_id in m.present:  # 校验①：在场 NPC 角色卡确落材料（存在性由 _precheck 保证）
        if pack.npcs[npc_id].name not in text:
            raise MaterializeError(f"在场角色卡未落入材料: {npc_id}")
    for f in card.facts:  # 校验②在位（全部 anchors）/ ③泄漏（任一 anchors 出现即泄漏）
        hit = [a for a in f.anchors if a in text]
        if f.in_material and len(hit) != len(f.anchors):
            raise MaterializeError(f"in_material 事实 anchors 未在材料中: {f.anchors}")
        if not f.in_material and hit:
            raise MaterializeError(f"材料泄漏：confab anchors 出现在材料中: {hit}")
```

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: **27 passed**（Task 1 的 9 + Task 2 的 10 + Task 3 的 8）

- [x] **Step 5: 核验 spec 已修订 + Commit**

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

> **执行记录（2026-09-12）✅ 完成**
>
> - Step 2 确认失败 ✓：`ModuleNotFoundError: No module named 'scripts.scenario_factory.materialize'`
> - Step 4 确认通过 ✓：整文件 **24 passed**（9 + 10 + 5）；全量 362 → **367 passed**
> - Step 5 核验 ✓：`git diff docs/plan-route-a-factory.md` **为空** —— spec 的四处修正
>   （§3.2 `recent: str` + `facts: null` 语义、§4.1 `recent=material.recent`、
>   A.2/A.3 删 `facts: []` 行与 `recent` 字符串化）已在设计阶段落地并随 `073e17f` 提交，
>   本 Step 只核验、不改动。
> - 落地：新增 `scripts/scenario_factory/materialize.py`
>
> **执行中发现并修正的 1 处计划缺陷**：
>
> 1. **`-k material` 这个 filter 两头都不准**（实测）：既**漏掉**本 Task 的
>    `test_present_npc_must_be_in_pack`（名字里没有 material），又**混进** Task 1 的
>    `test_setting_requires_in_material_true` 与 `test_judge_requires_pack_material_and_single_corruption`
>    → 实际命中 6 个（Task 3 的 4 个 + Task 1 的 2 个），而计划写"5 passed"。
>    已改为跑整文件 + 看**累计**数（24），并在 Step 2 注明原因。
>
> **实现侧相对计划的两处收敛**（已同步进 Step 3 代码）：
>
> - `REPO_ROOT` 改为从 `cards` 复用（不重复定义）—— 计划 Task 7 原先担心的
>   "两处定义同值 / ruff 报重复定义"就此消除；
> - `load_pack`（带缓存的包加载）**提前到本 Task 落地**，而不是留到 Task 7 的 Step 3b 再加；
>   Task 7 的 Step 3b 已改为"直接复用"。
>
> **评审补修（2026-09-12，第二轮）✅ 已修** —— 由外部评审指出、经实测确认（守卫 5 → **8**，
> 全量 372 → **375 passed**）：
>
> 2. **好感覆写被静默跳过 → 材料会主动断言错的语气档**（比"落成初始值"更糟，实测澄清）：
>    原 `for k, v in m.affections.items(): if k in state.affections:` ——
>    而 `state.affections` 只含 `schedule.yaml` 列出的对象（`state.py:103`）。
>    当"present/覆写的 NPC 有角色卡、但没有好感轨"时，后果是**两条**：
>    ① 好感行里该 NPC **整体消失**（不是落成初始值）；② 在场角色卡的「当前语气」
>    按 `state.affections.get(npc.id, 0.0)` 兜底 → 渲染**0 档**语气。
>    实测对照（仙侠包，卡声明 45）：正常 = `苏晚晴 45/100（客气有礼，偶有关切）`；
>    好感轨缺失 = 好感行无此人、语气却是 `冷淡疏离，公事公办`
>    → **材料"声明过 45 却按 0 档渲染"= 坏标签**（T2 语气冲突卡的整条通路就是好感档）。
>    现状：三个包的 `npcs ⊆ affections` 都成立、`NPC_BY_PACK` 三处映射也都命中，故属**潜伏**问题。
>    修法：抽出 `_precheck(card, pack)` 做**包级前置校验**（错就早抛，不做无用组装）——
>    ① `present` 的 NPC 必须在 `pack.npcs` 且**有好感轨**；
>    ② `material.affections` 的每个键必须在 `schedule.affections` 里；
>    随后 `build_material` 里改为**无条件写入**（不再 `if k in ...`）。
>    `_check` 拆为 `_precheck`（包级）+ `_check_text`（文本级：① 卡片入材料 / ②在位 / ③泄漏）。
>    守卫：`test_present_npc_without_affection_track_raises`（假包单元级，真实包构造不出来）、
>    `test_material_affection_override_must_be_declared`、
>    `test_judge_npc_mapping_targets_have_affection_tracks`（钉住映射表，防静默良率归零）。

---

### Task 4: 演绎器 verbalize.py（anchors 在位 + 反向校验 + 重演丢弃）

**Files:**
- Create: `scripts/scenario_factory/verbalize.py`
- Test: `tests/test_scenario_factory.py`（追加）

- [x] **Step 1: 写失败测试（追加）**

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

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k verbalize`
Expected: FAIL（`ModuleNotFoundError`）

- [x] **Step 3: 实现 verbalize.py**

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


def _judge_hint(card: ScenarioCard) -> str:
    """矛盾自然化指令——**按 category 分派**（一句话指令无法同时适配三类）。

    原稿只有一句"「」内的值必须出现、被改写事实的原词不得出现"，对 confab 是**反的**
    （confab 的 anchor 恰恰必须出现），会把 confab 的演绎引到错方向。
    """
    c = card.corruptions[0]
    if c.category == "setting":
        hint = ("把被命中事实换个说法写进叙事：detail 中「」内的**新值必须出现**，"
                "**原值不得出现**（这是要考的设定矛盾）。")
    elif c.category == "confab":
        hint = ("把 detail 提到的那件事**当作既成事实直接断言**（材料里从未有过它，"
                "这正是要考的点）；不得写成「听说 / 可能 / 似乎」。")
    else:
        hint = "改写说话人的语气或底线，使其贴合 detail 的描述（不涉及具体专名）。"
    return f"\n矛盾自然化：{hint}\n（category={c.category}；detail：{c.detail}）"


def verbalize_card(llm, card: ScenarioCard) -> VerbalizeResult:
    """卡 → 自然文本。缺要素重演一次，仍缺则 dropped=True（调用方计数）。"""
    user = (
        f"语体：{card.axes.style}；长度约 {card.history_spec.target_tokens} token；"
        f"可掺入的闲笔：{card.history_spec.noise}\n场景卡 JSON：\n"
        + card.model_dump_json()
    )
    if card.module == "judge":
        user += _judge_hint(card)
    msgs = [{"role": "system", "content": VERBALIZE_SYSTEM},
            {"role": "user", "content": user}]
    max_tokens = int(card.history_spec.target_tokens * 1.2)  # spec §4 的 ×1.2 上限
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

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q -k verbalize`
Expected: 5 passed

- [x] **Step 5: Commit**

```bash
git add scripts/scenario_factory/verbalize.py tests/test_scenario_factory.py
git commit -m "feat(factory): 演绎器与 anchors 在位/反向校验（Task 4）"
```

> **执行记录（2026-09-12）✅ 完成**
>
> - Step 2 确认失败 ✓：`ModuleNotFoundError: No module named 'scripts.scenario_factory.verbalize'`
> - Step 4 确认通过 ✓：`-k verbalize` **5 passed**；整文件 **32 passed**（9+10+8+5）；
>   全量 367 → **372 passed**
> - 落地：新增 `scripts/scenario_factory/verbalize.py`
>
> **执行中发现并修正的 1 处计划缺陷**：
>
> 1. **自然化指令对 confab 是反的**（原稿一句话适配三类，但三类方向本就不同）：
>    原句"detail 中「」内的值必须出现在文中，**被改写事实的原词不得出现**"——
>    对 setting 正确，对 confab 却**自相矛盾**（confab 的 anchor 恰恰**必须**出现，
>    否则"缺席证据"这条通路不成立；且 confab 并无"被改写"的事实）。
>    照原句写会把 confab（占 judge 配额 ≥40%）的演绎引向错方向。
>    修法：抽出 `_judge_hint()` **按 category 分派**。
>
> **功能性核验**（stub 演绎器，三类各取真实卡）：
>
> | category | 须在位 | 须缺席 | 行为 |
> | --- | --- | --- | --- |
> | setting | 新值（`听雨`） | 原值（`环宇`） | 新值在位 → 收下；原值也写回 → 丢弃 ✓ |
> | confab | 被断言的 anchor（`密码本`） | （无） | 说出编造 → 收下；没说 → 丢弃 ✓ |
> | ooc | 无（hit_idx=None） | （无） | 全部事实 anchors 须在位 ✓ |

---

### Task 5: rubric 评委 rubric_judge.py（score / pairwise / select / probe 四模式）

**Files:**
- Create: `scripts/rubric_judge.py`
- Test: `tests/test_scenario_factory.py`（追加；StubLLM 已在 Task 4 段定义，直接复用）

- [x] **Step 1: 写失败测试（追加）**

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

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scripts.rubric_judge'`）

> **为什么不用 `-k`**（与其他 Task 同一课）：`-k "score or pairwise or select or probe"` 会把
> Task 2 的 `test_fact_anchors_are_pairwise_disjoint` 一起命中（含 "pairwise"）→ 实际 7 个。
> **过滤器审计结论**（逐个按名字核过）：Task 4 的 `-k verbalize`、Task 6 的 `-k "build_ or compress"`、
> Task 8 的 `-k "fingerprint or dedup or quota or write_layer"`、Task 9 的 `-k "run_eval or report"`
> 四个**已验证准确**（与各自 Task 的守卫一一对应，无多无少），继续沿用；
> **Task 3/5/7 三个不准**（或漏或混），一律改为跑整文件 + 看**累计**数。

- [x] **Step 3: 实现 rubric_judge.py**

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

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: **38 passed**（Task 1 的 9 + Task 2 的 10 + Task 3 的 8 + Task 4 的 5 + Task 5 的 6）

- [x] **Step 5: Commit**

```bash
git add scripts/rubric_judge.py tests/test_scenario_factory.py
git commit -m "feat(factory): rubric 评委四模式与探针改坏/检出口径（Task 5）"
```

> **执行记录（2026-09-12）✅ 完成**
>
> - Step 2 确认失败 ✓：`ModuleNotFoundError: No module named 'scripts.rubric_judge'`
> - Step 4 确认通过 ✓：整文件 **38 passed**（9+10+8+5+6）；全量 375 → **381 passed**
> - 落地：新增 `scripts/rubric_judge.py`（`score` / `select` / `pairwise` / `make_probe` /
>   `probe_detected` 五件 + §7.4 质检员 `naturalness` / `quality_sample`）
>
> **执行中发现并修正的 1 处计划缺陷（同类第三次）**：
> `-k "score or pairwise or select or probe"` 会把 Task 2 的
> `test_fact_anchors_are_pairwise_disjoint` 一起命中（含 "pairwise"）→ 实际 7 个而非 6 个。
>
> **顺带做了全量过滤器审计**（逐个按测试名核对），结论写进 Step 2 的注：
> **准确**（继续沿用）= Task 4 `-k verbalize`、Task 6 `-k "build_ or compress"`、
> Task 8 `-k "fingerprint or dedup or quota or write_layer"`、Task 9 `-k "run_eval or report"`；
> **不准**（已改为跑整文件 + 累计数）= Task 3 `-k material`、Task 5 本处、
> Task 7 `-k "hook or quality"`。根因是 `-k` 在同文件里按**子串**匹配，跨 Task 必然互相干扰 ——
> 一次审完，省得后面每个 Task 都踩。

---

### Task 6: assemble.py 样本构建三分支 + compress 拒绝采样（spec §6）

**Files:**
- Create: `scripts/scenario_factory/assemble.py`
- Test: `tests/test_scenario_factory.py`（追加）

- [x] **Step 1: 写失败测试（追加）**

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
    """spec §4.2 硬纪律：compress 输入模板逐字对齐生产（`game.py:_compress_history`）。

    **必须断言样本字段本身** —— 原稿只断言了 `compress_messages()` 的函数输出，
    于是"样本里存裸文本"整批漏过（与 extract 侧的断言方式不对称）。
    """
    card = _compress_card()
    history, summary = _history_and_summary(card)
    r = build_compress_sample(StubLLM([history, summary]), card, sampling="off")
    assert r.sample["input"] == compress_messages(card, history)[1]["content"]
    assert r.sample["input"].startswith("<旧摘要>")
    assert r.sample["input"].endswith("</新增历史>")
    msgs = compress_messages(card, "新增历史文本")
    assert msgs[0] == {"role": "system",
                       "content": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)}
    assert msgs[1]["content"] == (
        f"<旧摘要>\n{card.old_summary}\n</旧摘要>\n\n<新增历史>\n新增历史文本\n</新增历史>")


def test_compress_incremental_sample_carries_old_summary():
    """增量合并档（`seq%4==0`，约 25% 的 compress 卡）的样本必须把旧摘要带进 input。

    否则模型学不到"读旧摘要 → 合并"这条通路，而评测时用的却是带旧摘要的完整模板。
    """
    card = next(c for s in range(60)
                if (c := generate_card(10231, s, "compress")).old_summary)
    history, summary = _history_and_summary(card)
    r = build_compress_sample(StubLLM([history, summary]), card, sampling="off")
    assert card.old_summary in r.sample["input"]
    assert "<旧摘要>" in r.sample["input"]


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

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "build_ or compress"`
Expected: FAIL（`ModuleNotFoundError`）

- [x] **Step 3: 实现 assemble.py（样本构建部分）**

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
    # **单一素材真源**：模型看到的就是这一段（含 <旧摘要>/<新增历史> 包裹），
    # 故样本 input、虚构判定的素材、评委的【材料】三处都用它。早先版本只有 `hist.text`
    # （裸历史），三处后果：① 样本 input 与生产模板不一致（spec §4.2 硬纪律）；
    # ② 增量合并档的旧摘要根本不进样本（约 25% 的卡，模型学不到"读旧摘要→合并"）；
    # ③ 虚构判定以裸历史为素材 → 旧摘要里的「」引用词被误判成编造，候选被误杀。
    source = compress_messages(card, hist.text)[1]["content"]
    n = CANDIDATES_N if _sampling_on(card, sampling) else 1
    cands, killed = [], []
    for _ in range(n):
        try:
            s = _summarize_once(llm, hist.text, card)
        except ValueError as e:
            killed.append(str(e))
            continue
        if why := _program_kill(s, source, card):
            killed.append(why)
            continue
        cands.append(s)
    if not cands:
        return BuildResult(None, f"候选全杀: {killed}")
    best = select(llm, candidates=cands, material=source,
                  preserve_points=card.preserve_points) if len(cands) > 1 else 0
    return BuildResult({
        "id": card.card_id, "module": "compress", "version": SAMPLE_VERSION,
        "genre": card.axes.genre,
        # 生产同款 user 段（与 extract 侧对称：那边也存带模板包裹的 user 段）
        "input": source, "output": cands[best],
        "sampling": sampling, "candidates": len(cands), "killed": killed,
        "long_input": card.history_spec.target_tokens >= LONG_INPUT_TOKENS,
        "preserve_points": [{"text": p.text, "anchors": p.anchors}
                            for p in card.preserve_points],
    })
```

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "build_ or compress"`
Expected: 10 passed

- [x] **Step 5: Commit**

```bash
git add scripts/scenario_factory/assemble.py tests/test_scenario_factory.py
git commit -m "feat(factory): 样本构建三分支与 compress 拒绝采样三档（Task 6）"
```

> **执行记录（2026-09-12）✅ 完成**
>
> - Step 2 确认失败 ✓：`ModuleNotFoundError: No module named 'scripts.scenario_factory.assemble'`
> - Step 4 确认通过 ✓：`-k "build_ or compress"` **9 passed**（该过滤器已审计为准确）；
>   整文件 **47 passed**；全量 381 → **390 passed**
> - 落地：新增 `scripts/scenario_factory/assemble.py`（`extract_messages` / `compress_messages` /
>   三分支 `build_*_sample` / 程序先杀三项 / 三档拒绝采样）
> - **本 Task 无计划缺陷** —— 前几轮已把过滤器与模板问题清掉，这次一次跑通
>
> **功能性核验**（三个分支的真实产物，确认对齐生产）：
>
> | 分支 | 产物要点 |
> | --- | --- |
> | extract 正例 | `input = '已有事实：\n\n<回合内容>\n…\n</回合内容>'`；labels 取自卡面 4 条；`expect_empty=False` |
> | extract 负例（去重纪律） | `existing=['玩家答应把沈砚转交给灰雀号']`；`labels=[]`；`expect_empty=True` |
> | compress（long 档） | system 段 = `COMPRESS_SYSTEM`；`candidates=4`、`long_input=True`、`killed=[]`；preserve_points 2 条 |
>
> 两侧 system 段实测都为引擎提示词（`EXTRACT_SYSTEM` / `COMPRESS_SYSTEM`）—— **不再是自造提示词**，
> spec §4.2「输入模板逐字对齐生产」落地。
>
> **评审补修（2026-09-12，第二轮）✅ 已修** —— 由外部评审指出、经实测确认（守卫 9 → **10**，
> 全量 390 → **391 passed**）：
>
> 2. **compress 样本的 `input` 存的是裸历史，而非生产 user 段**（与 extract 侧不对称）：
>    `extract` 侧存的是 `extract_messages(...)[1]["content"]`（带 `已有事实：`/`<回合内容>` 包裹），
>    而 compress 侧写的却是 `"input": hist.text`。实测三处后果：
>    ① `input == 生产 user 段` → **False**、`startswith("<旧摘要>")` → **False**
>       → 训练分布 ≠ 推理分布（spec §4.2 硬纪律要防的正是这个）；
>    ② **增量合并形态整条丢失**：约 25% 的 compress 卡带 `old_summary`（`seq%4==0`），
>       但裸文本 input 里旧摘要根本不进样本 → 模型学不到"读旧摘要→合并"，
>       而评测时用的却是带旧摘要的完整模板；
>    ③ **虚构判定会误杀**（评审未点出、实测发现）：`_program_kill(s, hist.text, …)` 以**裸历史**
>       为素材，而模型看到的是含旧摘要的完整段 → 旧摘要里的「」引用词会被判成编造。
>       实测：旧摘要含「夜明珠」（不在裸历史里）、候选照它复述 → 旧素材判 `虚构:夜明珠`，
>       新素材判 `None`（正确放行）。
>    **守卫为什么漏过**：`test_compress_input_matches_production_template` 原稿只断言了
>    `compress_messages()` 的**函数输出**，没断言**样本字段本身** —— 与 extract 侧
>    （`got.startswith("已有事实：")`）不对称，于是"样本存裸文本"整批漏过。
>    修法：引入 `source = compress_messages(card, hist.text)[1]["content"]` 作为**单一素材真源**，
>    **样本 input / 虚构判定素材 / 评委【材料】三处统一用它**；
>    并把守卫改成断言样本字段（`input == compress_messages(...)[1]["content"]` +
>    `startswith("<旧摘要>")` + `endswith("</新增历史>")`），另补
>    `test_compress_incremental_sample_carries_old_summary` 钉住增量合并形态。

---

### Task 7: 质量门 + card_hook 出厂门禁 + confab 人读清单（决策 16）

**Files:**
- Modify: `scripts/scenario_factory/assemble.py`（追加）
- Test: `tests/test_scenario_factory.py`（追加）

- [x] **Step 1: 写失败测试（追加）**

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

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: FAIL（`ImportError: cannot import name 'BatchStats'`）

> 不用 `-k "hook or quality"`：它**漏掉**首个守（其名字不含这两个词），
> 又**混进** Task 5 的 `test_quality_sample_flags_template_like_output`（含 "quality"）。

- [x] **Step 3: 实现（追加到 assemble.py）**

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

- [x] **Step 3b: 把两个门禁**接进产线**（原稿定义了却从未被调用——必须补，否则门禁等于不存在）**

1. `hook_gate` 接在 judge 样本构建处（`build_judge_sample` 内、material 装配之后）：

```python
    if hooked := hook_gate({"category": c.category, "speaker": card.material.present[0],
                            "narration": r.text}, load_pack(card.pack)):
        return BuildResult(None, f"confab 撞卡: {'/'.join(hooked)}")
```

   注意 `hook_gate` 只对 `category == "confab"` 生效（非 confab 直接返回 `[]`）。
   另：`load_pack`（**带缓存的包加载，已在 Task 3 落地**）直接复用 —— 同批上千张卡不要重复解析 YAML：

```python
from .materialize import load_pack   # Task 3 已提供；此处只是使用
```

2. `quality_sample`（质检员）接在**出库前**（Task 8 的 `main` 里，见该处 Step 3）：

```python
    bad_ids = quality_sample(llm, samples, rate=QUALITY_SAMPLE_RATE)
    if bad_ids:
        print(f"[质检] 自然度 <1 剔除 {len(bad_ids)} 条（须重造）：{bad_ids[:10]}")
        drop = set(bad_ids)
        samples = [s for s in samples if s["id"] not in drop]
```

   并加一条守卫，确保"定义了必被调用"（防再次出现"门禁存在但产线不用"）——
   见 **Task 8 的测试块**（`test_pipeline_calls_both_gates`）：放在本 Task 会必红，
   因为它的第二、三条断言要求 `quality_sample(` 已出现在 `assemble.py` 里，
   而质检员的接线在 **Task 8 的 `main`**（本 Task 只接 `hook_gate`）。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: **50 passed**（Task 1~6 的 48 + Task 7 的 2）

> 注：本 Task 原写"Task 6 的 9 + Task 7 的 3 = 50"——数字巧合但仍对，构成已变：
> Task 6 加了 compress 契约守卫（9 → 10），而 `test_pipeline_calls_both_gates`
> 已移到 Task 8（它的断言依赖 Task 8 的 `main`），故本 Task 为 2 条。

- [x] **Step 5: Commit**

```bash
git add scripts/scenario_factory/assemble.py tests/test_scenario_factory.py
git commit -m "feat(factory): 质量门、confab 卡中性门禁与人读清单（Task 7）"
```

> **执行记录（2026-09-12）✅ 完成**
>
> - Step 2 确认失败 ✓：`ImportError: cannot import name 'BatchStats'`
> - Step 4 确认通过 ✓：整文件 **50 passed**（Task 1~6 的 48 + Task 7 的 2）；
>   全量 391 → **393 passed**
> - 落地：`assemble.py` 追加 `BatchStats` / `hook_gate` / `quality_gate` / `manual_review_row`，
>   并把 `hook_gate` **接进 `build_judge_sample`**（Step 3b）
>
> **本 Task 的核心是补上"定义了却没调用"**：原稿定义了 `hook_gate` 却从未在产线上调用 ——
> 等于门禁**不存在**。现在 judge 样本构建时逐条过门禁，撞卡即丢弃并计入 `dropped`。
>
> **功能性核验**（门禁真的在产线上生效，不只是函数能跑）：
>
> | 输入 | 结果 |
> | --- | --- |
> | 干净叙事（不撞卡） | `sample` 正常产出、`hook_gate` 撞词 = `[]` |
> | 撞卡叙事（含角色卡二字串「口快」） | **被丢弃**，`dropped_reason = confab 撞卡: 口快` ✓ |
> | `quality_gate(built=9, dropped=1)` | `None`（10% ≤ 30%）✓ |
> | `quality_gate(built=6, dropped=4)` | `丢弃率 4/10 超 30%` ✓ |
> | `manual_review_row` | 4 个字段（`id`/`material`/`narration`/`expect`）✓ |
>
> **执行中发现并修正的 1 处计划缺陷（顺序问题）**：
> `test_pipeline_calls_both_gates` 原排在**本 Task**，但它的断言
> （`"quality_sample(" in src`、`"quality_sample(llm, samples" in src`）要求质检员**已接线**，
> 而那在 **Task 8 的 `main`** —— 放本 Task 会必红。已移到 Task 8 的测试块，并在 Step 3b 注明。
> 连带把 Task 8 的 Step 4 改为整文件 + **56 passed**、Task 9 改为 **59 passed**，
> 并把 Task 11 的构成改为 `9+10+8+5+6+10+**2**+**6**+3`（总数 59 不变）。

---

### Task 8: 配额计数 + 前缀去重 + sha256 出库 CLI（三层种子空间，spec §8）

**Files:**
- Modify: `scripts/scenario_factory/assemble.py`（追加）
- Test: `tests/test_scenario_factory.py`（追加）

- [x] **Step 1: 写失败测试（追加）**

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


def test_pipeline_calls_both_gates():
    """门禁**定义了必须被调用**（原稿定义了 `hook_gate` 却从未调用 = 门禁不存在）。

    放在本 Task 而非 Task 7：本断言要求 `quality_sample(` 已出现在 `assemble.py` 里，
    而质检员的接线在**本 Task 的 `main`**（Task 7 只接 `hook_gate`）。
    """
    import inspect

    from scripts.scenario_factory import assemble

    src = inspect.getsource(assemble)
    assert "hook_gate(" in src, "hook_gate 定义了却没被调用"
    assert "quality_sample(" in src, "quality_sample 定义了却没被调用"
    assert "quality_sample(llm, samples" in src, "质检员没接在 main 的产线上"
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k "fingerprint or dedup or quota or write_layer"`
Expected: FAIL（`ImportError`）

> （Step 4 改用整文件 + 累计数，见该处说明。）

- [x] **Step 3: 实现（追加到 assemble.py）**

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

- [x] **Step 4: 跑测试确认通过 + dry-run 配额预演（零成本）**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: **56 passed**（Task 1~7 的 50 + Task 8 的 6）

> 本 Task 的 `-k "fingerprint or dedup or quota or write_layer"` 对本 Task 的 5 条**准确**，
> 但**漏掉**新增的 `test_pipeline_calls_both_gates`（名字里没有那四个词）。
> 与其再拼一个易碎的过滤器，直接跑整文件 + 累计数。

Run: `uv run python -m scripts.scenario_factory.assemble --layer train --extract 100 --judge 80 --compress 30 --dry-run`
Expected: 打印各模块卡数、题材分布、长输入占比；无 API 调用（`--dry-run` 在 `load_settings` 之前 return，故不需要 key）

- [x] **Step 5: Commit**

```bash
git add scripts/scenario_factory/assemble.py tests/test_scenario_factory.py
git commit -m "feat(factory): 配额/去重/sha256 出库 CLI 与三层种子空间（Task 8）"
```

> **执行记录（2026-09-12）✅ 完成**
>
> - Step 2 确认失败 ✓：`ImportError: cannot import name 'dedup'`
> - Step 4 确认通过 ✓：整文件 **56 passed**（Task 1~7 的 50 + Task 8 的 6）；
>   全量 393 → **399 passed**
> - 落地：`assemble.py` 追加 `fingerprint` / `_sample_text` / `dedup` / `quota_gaps` /
>   `write_layer` / `_build_module` / `main`（含 `--dry-run`）
> - **无计划缺陷**（前几轮已把模板/过滤器/顺序问题清掉）
>
> **dry-run 配额预演（零成本，实测三层）** —— 顺带验证**决策 19 在 CLI 层也成立**：
>
> | 层 | extract 题材 | 留出轴纪律 |
> | --- | --- | --- |
> | train | 7 个基础题材 | 无留出轴 ✓ |
> | dev | 8 个（含**抗战谍战** 5 张） | 无「民国谍战」✓ |
> | eval | 7 个（含**民国谍战** 10 张） | 无「抗战谍战」✓ |
> | judge（三层） | 仅仙侠/古代武侠/现代都市 | 只出**有包可物化**的题材（G1 待造）✓ |
>
> 另验证：`--layer train --seed-base 20000` 被正确拒绝（exit 1，"seed 与层不一致"）。
>
> **出库产物可被下游消费（round-trip 实测）**：三模块各造一条样本 → `write_layer('eval', …)`
> → 读回 JSONL **与样本完全等价**、`manifest.sha256` 与 `file_digest` 一致、
> `frozen=True`、`OPEN_LOG.md` 追加计数行 ✓。
> 字段契约：extract `{input, existing, expect_empty, labels, …}`、
> judge `{material, narration, expect, category, speaker, pack, …}`、
> compress `{input, output, preserve_points, candidates, long_input, …}`
> —— 与 Task 9 `run_eval` 将直接取用的字段一致。
>
> ⚠️ **遗留一项（不阻塞，需决策）**：spec §3.3 要求"按 `plan-phase1-data.md` §4.4 矩阵出卡，
> **每格 ≥N 未达标不出库**"，但 `quota_gaps` 目前只落实了 **2/9 条轴**
> （题材占比 ≥10%、compress 长输入档 ≥20%）；其余轴（语体/实体类型/数值系统/关系动力/
> 违规形态/结构规模）未计数。且现有实现只检**偏斜**（份额）不检**绝对量**——
> 单条样本的单模块永远报不出缺口。§4.4 的配额是散文，需先转成常量表。
> **建议列入 Task 11 总装验收的待办，或另开 Task 8.1。**

---

### Task 9: 轨道 2 评测跑批 + 探针校准 + 定 X（决策 13，M4 验收项）

**Files:**
- Modify: `scripts/rubric_judge.py`（追加 `run_eval` 与 `main`）
- Test: `tests/test_scenario_factory.py`（追加）

- [x] **Step 1: 写失败测试（追加）**

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

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scenario_factory.py -q -k run_eval`
Expected: FAIL（`ImportError: cannot import name 'run_eval'`）

- [x] **Step 3: 实现（追加到 rubric_judge.py）**

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

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scenario_factory.py -q`
Expected: **59 passed**（Task 1~8 的 56 + Task 9 的 3）—— 本文件全部守卫

> `-k "run_eval or report"` 对本 Task 的 3 条准确，但与本计划既有约定一致，统一跑整文件 + 累计数。

- [ ] **Step 5: 定 X（决策 13 的 M4 验收项，人工+程序）** ⏸ **待真机执行**（本轮未做）

> **为什么没做**：本步需要 ① dev 层 compress **出库数据**（`main` 真机跑，花 API 钱）
> 与 ② 轨道 1 规则保全率对齐跑批 —— 属 M4 验收项，离线测不出来。
> 前置条件与命令见下方原步骤；执行后把 X 写回 spec §12.A 决策 13 行。

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

- [x] **Step 6: Commit**

```bash
git add scripts/rubric_judge.py tests/test_scenario_factory.py reports/rubric-x-calibration-*.md docs/plan-route-a-factory.md
git commit -m "feat(factory): 轨道 2 批跑与探针校准门；定 X 报告（Task 9，决策 13）"
```

> **执行记录（2026-09-12）✅ Steps 1~4/6 完成 · ⏸ Step 5 待真机**
>
> - Step 2 确认失败 ✓：`ImportError: cannot import name 'budget_policy'`（收集期报错）
> - Step 4 确认通过 ✓：整文件 **59 passed**；全量 399 → **402 passed**
>   —— 与 Task 11 预测的终值一致（本文件 59 条守卫全部就位）
> - 落地：`rubric_judge.py` 追加 `prompt_version` / `budget_policy` / `probe_positions` /
>   `_restore_points` / `run_eval` / `main`
> - **无计划缺陷**
>
> **功能性核验**（报告形态与批作废口径）：
>
> | 项 | 实测 |
> | --- | --- |
> | `prompt_version` | `a19110f4d60dddf9`（三个评委提示词的 sha 前 16 位） |
> | `budget_policy` | `budgets.py@b7a187c940d6` |
> | `probe_positions` | `n=10,rate=0.2 → [1,2]`；`n=30,rate=0.1 → [4,18,27]`；`n=3,rate=0.2 → [0]` |
> | 报告 keys | `scores/probes/probe_detection/probe_indices/batch_valid` + **四件指纹** ✓ |
> | 探针全检出 | `probe_detection=1.0`、`batch_valid=True`、`scores=8`（2 探针不混入）✓ |
> | 探针全漏 | `probe_detection=0.0`、`batch_valid=False` → `main` exit 1 并提示整批重评 ✓ |
>
> **⏸ Step 5（定 X）本轮未做** —— 它需要 dev 层 compress **出库数据**（真机跑 `main`）
> 与轨道 1 规则保全率对齐跑批，属 M4 验收项，离线测不出来。复选框已**故意留未勾**并注明。
>
> **顺带修掉一处测试文件卫生问题**：`tests/test_scenario_factory.py` 里
> `from scripts.rubric_judge import (...)` **重复了两份**（Task 6 加导入时插重的），
> 本轮合并为一份并把新符号并入。

---

### Task 10: 决策 15——EXTRACT_SYSTEM 提示词收紧实验（零 API 成本，需 GPU 机）

**Files:**
- Modify: `game_agent/memory.py:50-62`（EXTRACT_SYSTEM 全文替换）
- Test: `tests/test_extract_prompt.py`（追加 1 个守卫）

背景（spec §12.A 决策 15 取证定案）：14B 在 93 个正例 run 里 38.7% 恒输出「无」——
系统性保守判定（宁可不记），是提示词能治的病，不该先用 LoRA 治。本任务收紧提示词，
复测后再定 extract 训练量（必要时砍掉该训练批）。

- [x] **Step 1: 写失败测试（追加到 tests/test_extract_prompt.py）**　⚠️ 该守卫**已随实验未达标撤回**（见执行记录）

```python
def test_extract_prompt_has_decision_rule_against_conservatism():
    """决策 15（路线 A spec §12.A）：治「宁可不记」——判定式 + 「无」通路收窄。"""
    assert "判定式" in EXTRACT_SYSTEM
    assert "偏向输出" in EXTRACT_SYSTEM
    assert "仅当通篇没有任何上述内容" in EXTRACT_SYSTEM  # 「无」通路收窄
    assert "没有值得长期记住的事实就只输出" not in EXTRACT_SYSTEM  # 旧保守句已替换
```

- [x] **Step 2: 跑测试确认失败**（实测：1 failed / 4 passed，红在 `assert "判定式" in EXTRACT_SYSTEM`）

Run: `uv run pytest tests/test_extract_prompt.py -q`
Expected: 1 failed（新守卫），其余 4 个存量测试仍过

- [x] **Step 3: 替换 EXTRACT_SYSTEM（memory.py:50-62）**　⚠️ **已执行后撤回**：`prompt_version` `99668791ed5957fc` → `ad8beafe171e9aa9`，复测未达标 → `git checkout` 复原（下方代码块即本轮被测版本，留档）

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

- [x] **Step 4: 跑测试确认通过**（实测 16 passed，含新守卫 5 个 prompt 测试）

Run: `uv run pytest tests/test_extract_prompt.py tests/test_memory.py -q`
Expected: 全过（含新守卫 5 个 prompt 测试）

- [x] **Step 5: 复测（14B 在 GPU 机；flash 侧在本机——**两侧都必须跑**）**　❌ **两侧均未达标**（结论与逐例子句归因见执行记录）

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

- [x] **Step 6: Commit**　⚠️ 计划提交内容作废（引擎改动已撤回）；实际提交 `f65f84f` = 对照工具 + 复测证据，**不含 `game_agent/memory.py`**

```bash
git add game_agent/memory.py tests/test_extract_prompt.py
git commit -m "feat(memory): EXTRACT_SYSTEM 判定式收紧（Task 10，决策 15 实验）"
```

**执行记录（2026-09-12）**

**做了什么**：计划 6 步全部走完 —— ① 追加守卫、确认红（`1 failed / 4 passed`，红在 `assert "判定式" in EXTRACT_SYSTEM`）
→ ② 按 Step 3 **给出的原文**替换 `EXTRACT_SYSTEM`（计划措辞直接可用，`16 passed`）→ ③ 两侧真机复测
→ ④ 按封板判据逐条判定 → ⑤ **未达标 → 撤回改动**。端点：14B = VSCode 转发的本地 vLLM
（`local-14b`，`max_model_len=32768`，零 API 成本）；flash = 云端（本轮 ¥0.049）。

**复测结果**（同集 `eval-sets/extract/direct.yaml` · 42 例 = 31 正 + 11 负 · sha `b3d918e53d90` · `--repeat 3 --temperature 0`）

| 侧 | 模型 | 判据① C 类恒「无」 | 判据③ 召回（参考） | 判据② 负例 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 弱（本地 GPU） | `local-14b` | **36 → 7 run** ✅（上限 12） | **42 → 71**（45.2% → 76.3%）✅ | **33/33 → 30/33** ❌ | ❌ 未达标 |
| 强（现役/云端） | `deepseek-v4-flash` | **3 → 9 run** ❌ | **89 → 84** ❌ | **31/33 → 27/33** ❌ | ❌ 未达标 |

产物：`reports/extract-eval-20260912-231707.json`（14B）/ `…-231918.json`（flash）；
同集对照 `reports/extract-compare-14b-task10.json` / `…-flash-task10.json`；总报告 `reports/extract-task10-report.md`。

**根因（可指名到子句，非"效果不好"这类空话）**：新增的"出现…**数字或数值**…就必须输出"把**瞬时数量**
当长期事实 —— 两侧负例退化**同源**于此（`n_funds` 两侧、`p2_ticket` flash 侧，各 3/3 稳定复现）；
而两条真正的抑制源本轮**没动**：dedup 子句（"「已有事实」中已经存在的不要重复输出"被读成"该实体已知就整句弃记"）
→ `x_dedup`/`s_dedup`/`p2_dedup` 恒「无」；"剧情进展的瞬时状态不算"口径过宽 → `t_arm`（义肢黄铜齿轮转动）恒「无」。
即：**这一版治好了弱模型的"宁可不记"，却给两个模型都引入了"见数字就记"，且没碰真正的抑制源。**

**为什么撤回、而不是留档为"改进"**：判据明文"**不达标才回到必训**"，且对**现役** flash 三项全退化 ——
未达标物不得留在引擎里（"空 = 未知 ≠ 通过"）。撤回后 `pytest -q` = **407 passed**（引擎净改动为零）。

**结论（这才是本实验要回答的问题）**：
① 提示词**不能**替代 extract 训练 —— 14B 用**为它定制**的新提示词后仍只有 76.3%，**低于** flash 用**旧**提示词的 95.7%；
② extract **维持"必训"**，训练量不因本实验下调；
③ 训练数据应重点覆盖本轮实测暴露的两个真实短板（比提示词补丁稳）：**"已有事实的新增信息"（dedup ≠ 沉默）**与**"瞬时场面中的具体物品/参数"**。

**与计划的偏差（4 处）**：
1. Step 5(a) 原稿建议改 `.env` 三行指向本地端点 → 实际用 **PowerShell 作用域环境变量**（`load_dotenv()` 默认不覆盖已存在变量，
   同样生效且不脏 `.env`、不留痕）。
2. 计划命令写 `uv run pytest` → 本仓无 uv，实际用 `.venv\Scripts\python.exe -m pytest`（与既有 Task 一致）。
3. **计划外新增** `scripts/extract_compare.py` + `tests/test_extract_compare.py`（5 守卫）：计划要求"两侧对照"，
   但决策 20 的极性修正使两份**基线报告的 `summary` 口径不可比**（旧报告里 `n_funds` 还是正例）——
   不重切片就没法同尺对照。工具复用 `extract_eval.score_case`，不另立判分口径；守卫含"基线数字可复算"。
4. Step 6 的提交内容作废（见 Step 6 注），实际提交 `f65f84f` = 对照工具 + 复测证据。

**计数**：**402 → 407**（-1 撤回的 prompt 守卫，+5 对照工具守卫）；Task 11 Step 1 的期望值已同步。

**未拍板（遗留）**：同一份提示词在**弱/强两档模型上最优措辞方向相反**（弱模型要"多记"、强模型要"少触发"）
—— 要么把差异交给训练（=本轮的必训结论），要么在引擎里做**"按模型分别定稿"**（新决策，需拍板）。
另：第二轮的四条子句补丁已在 `reports/extract-task10-report.md` §4 **预注册但未执行**（含过拟合论证）；
若日后要为本地 14B 单独定稿，按"**预注册 → 一次性复测 → 不成即弃**"另开一轮，不要在同一轮里反复调措辞。

---

### Task 11: 总装验收 + 成本回填 + 文档回写

**Files:**
- Modify: `docs/plan-route-a-factory.md`（§12.C 追加执行记录）
- Create: `reports/route-a-cost-YYYYMMDD.md`

- [x] **Step 1: 全量测试**（实测 **417 passed**；计数拆解见下）

Run: `uv run pytest -q`
Expected: **342 存量** + 本计划新增 **59 个守卫**（Task 1~9：9+10+8+5+6+10+**2**+**6**+3）+ Task 10 的 1 个
prompt 守卫（**已随实验未达标撤回**）+ 计划外先落的 `evalmeta` 换行守卫 1 个
+ 对照工具 `extract_compare` 守卫 **5** 个 + Task 11 守卫 **10** 个
（用途标签 2 + `route_a_cost` 4 + `rubric_x_calibrate` 4）= **417 全绿**

> 计数口径（2026-09-12 实测）：`pytest --collect-only -q` 在**本计划开工前**是 **341**；
> 加上计划外先落的 `card_hook` 死字段守卫 1 条 = **342**（= 本表"存量"口径，见文首「进度」节）。
> 原稿写 279 是 Step 0 之后的旧数。
> 计划枚举的守卫数逐个数为 55（原稿写 30 也不对）。Task 2 加了 5 条守卫（anchors 不相交、
> corruption detail 可解析、judge 留出轴不漏+包必存在、recent 不撞 anchor、新值不撞事实）、
> Task 3 的夹具拆分把 4 变 5、Task 4 加了 confab 反向校验守卫、Task 5 加了质检员、
> Task 6 加了三条模板/负例守卫、Task 8 加了 judge 去重守卫、Task 9 加了报告指纹守卫。
> **Task 10 收尾后的净变化**：**-1**（计划内的 prompt 守卫随实验未达标撤回）+ **+5**
> （计划外的同集对照工具 `scripts/extract_compare.py`）→ 402 → **407**（实测 `pytest -q` = 407 passed）。

- [x] **Step 2: M1~M4 验收项核对（对照 spec §10.3 逐项打勾）**　→ 判定 **M1 ✅ / M2 ✅ / M3 ⚠️ / M4 ⚠️**（首次真机跑通：270 张卡 → 出库 210 条）

| 里程碑 | 验收项 | 证据 |
| --- | --- | --- |
| M1 | 卡 schema + 生成器 + 守卫测试 + 卡面出厂门禁 | Task 1/2 测试绿；**`python scripts/card_hook_check.py --gate`** 通过（注意：无 `[project.scripts]` 入口，不能写成 `card_hook_check --gate`；且它扫的是 `world-packs/` 的既有语料——**工厂卡的等价门禁是 `hook_gate`（Task 7），已接进 `build_judge_sample`**，两者口径同源） |
| M2 | 演绎器 + 要素校验 + 材料装配器（extract/judge） | Task 3/4 测试绿；校验①②③有对应用例 |
| M3 | compress 演绎 + 拒绝采样 + rubric 评委 | Task 5/6 测试绿；三档调用数断言在案 |
| M4 | 轨道 2 + 探针校准 + 三层出齐 + 出库 + **定 X** | Task 8/9；`rubric-x-calibration-*.md` 三要素齐 |

- [x] **Step 3: 成本回填**　→ 工厂批 **¥2.5035**（606 次调用）+ 轨道 2 评委 ¥0.0372；单位 **¥0.0119/样本** → 外推 2100 条 **≈¥25**（**未触 ¥90 门**）；报告 `reports/route-a-cost-20260912.md`

从 usage 记账汇总实际 token —— **记账来源已修正**：`complete_checked` **不接触** `UsageTracker`，
落盘只发生在 `LLMClient._record_usage`，且**只有构造时显式传了 `tracker=` 才有数据**
（该修法已落到 Task 8/9 的 `from_settings(..., tracker=UsageTracker("reports/usage-route-a.jsonl"))`）。

写 `reports/route-a-cost-YYYYMMDD.md`：与 spec §10.2 估算（默认档 **~¥62**）并列对照；
**超 ¥90 触发停批复盘**（spec §10.2 门限不变）。报告须附 `prompt_version` / `budget_policy` /
`endpoint` 三个指纹（spec §6.4）。

- [x] **Step 4: 文档回写 + Commit**

spec `docs/plan-route-a-factory.md` §12.C 追加：M1~M4 完成日期、各层出库 sha256、
X 取值报告路径、决策 15 复测结论。然后：

```bash
git add docs/plan-route-a-factory.md reports/route-a-cost-*.md
git commit -m "docs(factory): M1~M4 验收与成本回填（Task 11）"
```

**执行记录（2026-09-13）** —— 首次**真机**跑通全链路（此前 Task 1~10 只有离线守卫，`data/route-a/` 根本不存在）

**做了什么**：① 全量测试 **417 passed**；② 三次跑批（`--layer {train,dev,eval} --extract 30 --judge 30 --compress 30 --sampling long`，约 50 分钟 / 606 次调用）；
③ 轨道 2 评委跑 dev compress；④ 成本回填；⑤ 文档回写 + 打 eval 冻结 tag。

**验收判定**：**M1 ✅**（`card_hook_check --gate` 5 包撞卡 0）· **M2 ✅**（270 张卡真机演绎 → 出库 210 条，judge 材料由 3 个真实包物化）·
**M3 ⚠️**（拒绝采样跑通，但候选全杀率 20~33% 全因"超长"；rubric 评委探针检出 67% → 批作废）·
**M4 ⚠️**（三层出齐 + 出库 ✅；**「定 X」未完成**）。

**产物**：`data/route-a/{train,dev,eval}/`：出库 **69 / 72 / 69** 条，`manifest.json` 带 sha256（**9 个文件摘要全部复核一致**）；
extract 负例 **21% ≥ 15%**；决策 16 人读清单 7 条；`eval/OPEN_LOG.md` 冻结留痕 + tag **`eval-route-a-20260912`**。

**成本**：工厂批 **¥2.5035** + 轨道 2 评委 **¥0.0372**；单位 **¥0.0119/样本** → 外推生产规模 2,100 条 **≈¥25**
（**低于 §10.2 的 ~¥62**、**远低于 ¥90 停批复盘门限**）——但报告里明写：该"更低"是发现⑤的缺陷造成的**假便宜**，
长度缺陷修好必然上涨，故 **¥25 是下界**。

**六个发现（只记录、未修，修法方向见报告 §4）**：

| # | 发现 | 严重度 |
| --- | --- | --- |
| ① | **`card_id` 跨模块重复 → 质检剔除连坐**：`sc-{seed}-{seq}` 三模块同 id（train 69 条只有 29 个唯一 id），`bad_set` 按 id 全量过滤 → train/eval **实剔 3 条只报 1 条**（69 = 90−18−3，被点名 id 在三模块文件里同时消失）。**id 不能当主键** | **生产前必修**（静默丢数据） |
| ② | 轨道 2 探针检出 **67% < 90%** → 批作废 → **定 X 无数据基础**（"删要点"探针已删掉含「沈砚」的两条承诺句，评委仍给保真 2 = 按 anchor 字面在位放行） | 阻断 M4 |
| ③ | 评委三维饱和：保真 18/19、结构 19/19、流畅 19/19 满分 → 区分度 ≈ 0（根因：出库样本是"程序先杀幸存者"） | 生产前必修 |
| ④ | **compress 单模块丢弃率 20~33%**，被杀候选**全部因"超长"**（摘要中位 651 字 / 上限 800）；丢弃率与卡长无关 → 只跑 compress（生产批形态）即超 30% 门作废 | 生产前必修 |
| ⑤ | **「长输入档 20K」名不副实**：卡面 `target_tokens=20000`，实测**最长一次演绎输出仅 4,323 token**、出库历史仅 404~4,127 字；`long_input` 标记取自**卡面**而非文本 | 生产前必修 |
| ⑥ | **confab 幸存率仅 23%**（卡面 31/90 → 出库 7/60；同批 ooc 100% / setting 89%） | 影响类别配比 |

**与计划的偏差（3 处）**：
1. **计划外新增两个工具**（各带守卫）：`scripts/route_a_cost.py`（4 守卫，读盘重算成本——`UsageTracker.cost_report()` 只统计本实例内存，事后回填读不了文件）、
   `scripts/rubric_x_calibrate.py`（4 守卫，定 X 的程序部分；**批无效即拒绝产 X**，纪律写成代码）。
2. **计划外修一处实测缺口**：演绎/选优/质检的调用原先**全记 `purpose="aux"`** → §10.2 的分模块成本**回填不出来**。
   给各调用点加用途标签（`verbalize_extract/judge/compress`、`rubric_score/pairwise/select/quality`），
   并加 2 条守卫钉住"**只改标签、不改路由**"（标签不落在 judge/compress 两个真路由键上；思考开关与预算都不按 purpose 分派）。
3. **「定 X」未做**（计划列为本 Step 的验收项）：轨道 2 批作废 → 按 spec"先修评委提示词、整批重评"，**不硬凑一个 X**。
   计划里"X 取值报告路径"因此只能记成"未产出 + 阻断原因"。

**计数**：**417 passed** = 402（Task 10 收尾值）+ `extract_compare` 5 + 用途标签 2 + `route_a_cost` 4 + `rubric_x_calibrate` 4。

---

## Task 11 修复轮（验收发现 ①~⑥，2026-09-13）

**目标与顺序**（决策：先修生产侧再重跑，评委侧可在跑批期间并行）：①④⑤⑥ → **重跑三层验收批** → ②③ → 定 X → 文档/推送。

| # | 发现 | 修法（实测依据） | 守卫 |
| --- | --- | --- | --- |
| ① | `card_id` 跨模块重复（`sc-{seed}-{seq}` 不含模块，三模块共用 `seed_base+i`）→ 质检剔除按 id **连坐**（train/eval 实剔 3 条只报 1 条） | 新增 `cards.card_seed()`：层内按模块取**千位偏移**（extract +0 / judge +1000 / compress +2000，段宽 10000、生产规模不越界）；`write_layer` 加**层内 id 唯一硬校验**（撞车即拒写、不留半成品）；新增 `drop_flagged()` 返回**实际**剔除数（回退到 id 不唯一时多剔会**当场显示**） | 3 条（`test_card_seed_keeps_ids_unique_across_modules` / `test_write_layer_rejects_duplicate_ids` / `test_drop_flagged_reports_actual_removed_count`） |
| ④ | compress 候选被"超长"**硬杀**，单模块丢弃率 20~33% | 查实**生产端不检查摘要长度**（`game.py:_compress_history` 只看非空/未截断；`SUMMARY_MAX_TARGET` 注释即"提示词指导"）→ 工厂按 800 硬杀属**比生产更严**的误杀。改为**偏好**：优先选未超目标候选，**全超时取最短者并标 `over_target`**；硬杀（虚构/缺要点）**不放松** | 3 条（优先选达标 / 全超取最短不丢卡 / 全超**不得**豁免硬杀） |
| ⑤ | `long_input` 取自**卡面**（20K）而非文本 → 长输入档名义达标 | 新增 `_length_fields()`：记 `target_tokens` / `realized_chars`，`long_input` = 卡面属长档 **且** 实测字数 ≥ 目标 × `LENGTH_TOLERANCE(0.5)`；`quota_gaps` 的缺口文案**点出**"卡面属长档 N 张但实测未兑现（目标 X token，实测最长 Y 字）"。**不加重演**：实测模型对长度指令基本不响应（20K 目标产出 ≤4.3K token），重演只会烧钱 | 2 条（flag 看实测 / 缺口点名） |
| ⑥ | confab 幸存率仅 23% | 30 条真实叙事实测旧判据（任一 **2 字**重合）撞卡 **26/30 = 87%**，撞的全是虚词（`自己`×6 / `直接`×3 / `具体` / `的原因` / `自己是`）；同批 3-gram 10%（仍抓噪声）、4-gram 0/30 → 工厂门禁改判据为 **≥4 字连续重合**（≈ 逐字引用）；`card_hook_check.py` 的 2-gram 口径**不动**（服务于评测语料既有校准，用途不同） | 1 条重写（逐字引用必抓 + **只共享二字组不得误杀** + 非 confab 不查） |
| — | 诊断盲区：丢弃的卡**不留 id** | 新增 `BatchStats.drop()` + 出库 `dropped-{layer}.json`（按原因分列）——发现④⑥ 的诊断原先只能靠重跑花钱 | 1 条 |
| ② | 探针"删要点"漏检 → 批作废 | **探针侧**：原先只删「第一个 anchor」的句子，同一要点常有多处提及 → 删一处留一处 = 探针形同没坏却记成漏检；现删掉命中**任一 anchor** 的句子，**一处都删不掉就报错**；`run_eval` 把构造失败的探针剔出统计（`probes_invalid`），**无有效探针时检出率记「未知」并判批无效**。**判据侧**：`保真` 从"要点全在"改为"**仅凭摘要能否复原该要点**" | 2 条 |
| ③ | 评委三维饱和（区分度 ≈ 0） | 饱和**上报**：报告新增 `dim_stats`（四维分档计数/均值/`saturated`）+ `main` 打印告警——原先报告只显示一串 2，看不出"这批材料上评委没有区分度"（根因：出库样本是程序先杀的幸存者）。**材料侧的根治**留待本批数据出来后再判（④ 放开超长后幸存者构成变化，需重测） | 1 条 |

**提交**：`ca710d4`（①④⑤⑥ + 丢弃留档）· `3d381ac`（②③）。

**计数**：**417 → 429 passed**（① 3 + ④ 3 + ⑤ 2 + 丢弃留档 1 + ②③ 3；⑥ 为**重写**存量守卫、净增 0）。

**eval 冻结层的处置（决策 14 例外条款）**：`data/route-a/eval` 已冻结并打过 tag，而 ① 属**尺子自身缺陷**
（id 不能当主键）→ 按条款执行：OPEN_LOG 追加 **VOID 行**（作废 2026-09-13 那次写入 + 三个 sha256 + 修复提交号）、
**递增打开计数**、旧 tag `eval-route-a-20260912` **保留为作废留痕**，重跑后另打新 tag。

### 重跑结果（2026-09-13，修复后）

| 层 | 出库 | 丢弃 | confab 幸存 | id 唯一 | manifest 摘要 | 长输入（实测/卡面） |
| --- | --- | --- | --- | --- | --- | --- |
| train | **86**（原 69） | **4**（原 18） | 7/7 = 100% | ✓ | ✓ | 0/28（卡面 5 张） |
| dev | **87**（原 72） | **3**（原 18） | 7/7 = 100% | ✓ | ✓ | 1/29（卡面 14 张） |
| eval（冻结） | **88**（原 69） | **2**（原 18） | 11/11 = 100% | ✓ | ✓ | 0/28（卡面 11 张） |

合计 **261 条**（原 210，+24%）；`write_layer` 的 id 唯一硬校验**零拒绝**；9 个文件 sha256 全部复核一致；
`confab 撞卡` 由 6/8/10 归零；长输入档按实测口径如实报缺（发现⑤ 的目标形态）；
丢弃留档 `dropped-{layer}.json` 随批落盘。

### 轨道 2 复校（发现②③ 的真根因在 **spec 内部口径冲突**）

| 轮次 | 探针检出 | 处置 |
| --- | --- | --- |
| 初次（验收批） | 67% | 判"删要点"漏检 → 曾误判为"判官按 anchor 字面在位放行" |
| 第 2 轮（保真判据改"可复原"） | 33% | 更差：`结构` 维 26/26 饱和 → 「打乱结构」探针**注定漏检**（②③ 耦合） |
| 第 3 轮（判据改"整体缺失" + 结构坏法剥骨架） | 67% | 仍漏"删要点"：只删 anchor 句子时，**兄弟条目**仍承载该要点 → 判官给 1 分是**对的** |
| 第 4 轮（删到要点痕迹全无 + **探针分级**） | **100%** | 批有效 ✅ |

**真根因（更正上一份验收报告的归因）**：spec §7.2 定义 `保真` = "摘要是否引入源材料**不存在**的内容"，
并明写互补分工"**规则管要点在不在，rubric 管多出来的坏东西**"；而 §7.3 的探针表把考**覆盖**的
「删要点」挂在 `保真` 维上、要求判 0 分——**要求 rubric 判一个它设计上不判的东西**。spec §11 风险表
对这一类早有处方（"探针坏法分级，按级分别要求检出率"），据此：

1. **探针分级**：显性级（缺陷落在被度量的维度上：注入虚构→保真、结构崩坏→结构）计入门；覆盖级只作诊断并留档；
2. **坏法加固**：删要点删到"要点 anchor 与 4 字串**离线可证**全无"；打乱结构**剥除 Markdown 骨架**（摘要是分节要点表，仅打乱句子仍可能被读成"结构尚可"）；
3. **无效探针 ≠ 漏检**：构造失败剔出统计并留痕（`probes_invalid`）；无显性探针 → 检出率记**未知**、批无效；
4. **分层抽样**：保证每批 ≥1 个显性探针（原先逐位 `rng.choice`，3 抽全落覆盖级的概率不低 = 抽样运气废掉整批）。

**计数**：429 → **434 passed**（②③ 后续 5 条守卫）。

### 「定 X」（决策 13 · M4 最后一项）：三要素齐，但 **X = 100pp 不采纳**

报告 `reports/rubric-x-calibration-20260913.md`。协议字面算出 **X = 100pp**，但该值**退化**：
`diff` 分布是**两极**的（P50 = **0**、P95 = max = **100**），2/26 条撞满格把 P95 顶到上限 ⇒
`X = 100pp` 等于"冲突检测器常关"，与决策 13 立 X 的目的（冲突进人读）相反。

按决策 13 的冲突协议逐级排查（**先怀疑评委 → 探针已 100% 通过 → 再人工抽检规则代理**），抽检 **2/2** 判定：

| 样本 | 卡面要点 | 摘要实际写的 | 判定 |
| --- | --- | --- | --- |
| `sc-22019-0019` | 玩家的**装备**名为「旧书店」 | "灰雀号：**旧书店老板**，铺子前身是当铺" | **判官对**（按卡面所述确实复原不了） |
| `sc-22008-0008` | 玩家欠**密码本**五十两 | "玩家欠**罗九**五十两" | **判官对**（债权人名字对不上） |

**根因 = 新发现⑦（卡空间）**：`_NAME_POOL` 的名字**不区分语义槽**，任意名字可落进任意 `type`
（实测同批既有「装备名为**旧书店**」也有「欠**密码本**五十两」）→ 演绎器为写通顺文本**必然重新解释**
→ **标签（`preserve_points`）与产物（history/summary）语义分叉**；程序先杀只查 anchor 在位 → 放行；
轨道 2 查"能否复原"→ 如实给 0。两条轨道因此正面冲突 —— **两极分布不是评委的毛病，是标签的毛病**。
实测量级：`保真=0` 的样本 dev **2/26 = 8%**、train **0/25**（跨两批 **2/51 ≈ 4%**，与层无关）。
**这不是"少量坏样本"**：这类样本的**训练标签本身就是错的**，而它们已经进了出库批。

**处置**：X 待修 ⑦ 后重算（预期 0~10pp）；本批 2 条冲突样本已人工抽检完毕、**不进训练集**。
轨道 1 的工厂口径同时改为**必保全要点锚点**（spec §7.2"规则管要点在不在"）——实体口径是引擎真实轨迹的启发式，
在合成材料上会算出 100pp 废值（报告内已留档对照）。

### 发现⑦ 修复 + 第三轮定 X（2026-09-13，本目标第二步）

**改法**：名字按**槽位角色**分池（`NAME_POOLS` = item / person / place / code），`_FACT_TPL` 每条模板声明
所需池；`_take_name()` 按池取名且**全局不重名**（"同卡 anchors 互不相交"的前提）；情节骨架槽 n2/n3 同样按池取
（person / item）。旧机制（单一混池按**下标**发名字 + `FACT_SLOTS=8`）整体删除。

**守卫 3 条**：① 池间两两不重名且**无子串包含**（`断刃` ⊂ `断刃崖` 会让"命中哪个池"的判定失真）；
② 每条事实只用规定池里的名字（扫三层三模块 90 张卡）；③ 既有 `test_fact_anchors_are_pairwise_disjoint` 继续绿。

**修复效果（决定性证据）**：同一批 dev compress 上 `保真=0`（判官认为要点**整条不可复原**）
由 **2/26 = 8% → 0/24 = 0%**；抽查同两张卡：「装备名为**旧书店**」→「装备名为**黄铜齿轮**」、
「欠**密码本**五十两」→「欠**老樵夫**五十两」。

**重跑 dev 层**（X 的计算层，⑦ 修复后卡空间）：出库 **86 条**、丢弃 4（演绎丢弃 1 / 候选全杀 3）、
confab 人读清单 10 条；`write_layer` id 唯一校验通过。

**第三轮定 X**：`diff` P50 = 0、max = **50pp** → **X = 50pp**（决策 13 协议：P95 向上取整到 5pp）。
保真分布 0:0 / 1:3 / 2:21，剩下 3 条 50pp 差异全部是"**锚点全在**而评委判**一处轻微走样**"——
正是 spec §7.2 的**互补分工**（"规则管要点在不在，rubric 管多出来的坏东西"），属正常量程；
而 ⑦ 那类"锚点全在 vs 要点整条缺失"的平级矛盾 diff = **100pp > X** ⇒ 会被**正确标记为冲突**进人读。
**X = 50pp 采纳（本批口径）**，诚实边界：n = 24 时最近秩 P95 取"第 2 大值"、由 2~3 条边界样本决定 →
生产规模（300 条 / 30 探针）应**重算一次再冻结**。

**仍未清的前置（重要）**：`train` / `eval` 两层仍是 ⑦ 修复**前**的卡空间产物（含坏标签样本）——
**训练前必须重跑**（≈¥1.8 / 33 分钟）；本轮只重跑了 dev（X 的计算层）。

### 补齐：train / eval 两层重跑（同日，使三层同源）

| 层 | 出库 | 丢弃 | judge 类别（ooc/setting/confab） | confab 幸存 | 长输入（实测/卡面） | id 唯一 | 9 个 sha256 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| train | **87** | 3（演绎丢弃 2 / 历史演绎丢弃 1） | 12/8/8 | 8/8 = 100% | 0/29 vs 12 张 | ✓ | ✓ |
| dev | **86** | 4 | 11/8/10 | 10/10 = 100% | 0/27 vs 9 张 | ✓ | ✓ |
| eval（冻结） | **88** | 2（候选全杀） | 8/10/12 | 12/12 = 100% | 0/28 vs 6 张 | ✓ | ✓ |
| **合计** | **261 条** | 9 | — | — | — | — | — |

train 层是历次最干净的一次：**零"候选全杀"、零"confab 撞卡"**；丢弃留档带具体卡 id
（`sc-11010-0010` / `sc-11011-0011` 演绎丢弃、`sc-12019-0019` 历史演绎丢弃）。

**eval 层第二次触发决策 14 例外条款**（⑦ 属**标签级**缺陷：`preserve_points` 声称的内容在材料里不成立
→ 冻结层不可用于其用途）：OPEN_LOG 追加第 2 条 VOID 行（记录作废批 88 条的三个 sha256 + 修复提交 `13ced55`）、
递增打开计数、旧 tag `eval-route-a-20260913` 保留为作废留痕，重写后另打 **`eval-route-a-20260913b`**。
**风险记录**：例外条款**同日二次触发**——条目本身按设计工作（避免"用坏尺子决策"或"偷偷改不记"），
但它同时说明产线在**标签层**的成熟度不足（① id 撞车、⑦ 语义分叉都是"不报错但产出坏标签"的形态），
建议在 Phase-1 正式出数据前补一道**标签自检**（如：卡面事实与演绎文本的语义一致性抽检）。

**计数**：434 → **436 passed**（⑦ 守卫 2 条）。

**P2 标签自检（同日，防复发层 · spec §7.5）**：① 与 ⑦ 是同一形态（**不报错但产出坏标签**），
同日让决策 14 例外条款触发两次 → 补一道**语义抽检**（程序只查得出 anchor 字面在位）：
入库前抽检 20%，extract 送 `labels`、compress 送 `preserve_points`，judge 跳过（其标签由 anchors 程序保证）；
不成立即剔除 + 留档 `label-check-{layer}.json`；空/解析失败记**未判定**（≠ 通过）；
指纹独立 `label_check_version()`（不并入 `prompt_version()`，否则牵连已记录的 X 指纹）。
**双向验证**：阳性对照（⑦ 前批两张已知坏卡 `sc-22019-0019`/`sc-22008-0008`）**精确抓到**
`「装备名为「旧书店」」`/`「欠密码本五十两」` 且同卡正常标签不误报；阴性对照（⑦ 后批抽检 33 条）**0 违规**。
**计数**：436 → **439 passed**（+3 守卫）；并顺带把 `StubLLM` 扩成也记录 `messages`（守卫要断言"送进去的标签"）。

**待你拍板（未决）**：**长输入档 20K 的取舍**（(a) 降档到实测可达 / (b) 分块演绎真产出 20K / (c) 接受 0% 并写进决策 18 例外）
——它决定 compress 生产批怎么做，未决前不建议跑生产批量。

### 发现⑧：judge 批的标签校验缺口（2026-09-13，由"复核表里问题类型全是虚构事实吗"这一问追出来）

**追的过程**：确认那张复核表按设计只收 confab（决策 16 的不可程序化检查只对它成立）→ 于是问"那没进表的两类靠什么保证？"
→ 读码发现 **ooc 在工厂路径上没有任何校验**（`_corruption_swap` 对 ooc 返回 `hit_idx=None`、无引用值，
锚点校验与 OOC 无关；`hook_gate` 只查 confab）→ 而项目**另一条路径**（`build_judge_corpus.py`）的 ooc 是
**手工卡面锚定**的（逐条引用 boundaries/forbidden + 依据）⇒ 工厂这条路径少了这一步。

**改法**：新增 `JUDGE_LABEL_SYSTEM` + `check_judge_label()`（判"这段叙事是否**真的**呈现出该问题类型"），
`build_judge_sample` 随样本交付 `detail` 与 `speaker_card`（`_speaker_card_text()` 与 `hook_gate` 同源取 pack 角色卡），
judge 三类并入 §7.5 标签自检；`label_check_version()` 改为两条提示词合并（`prompt_version()` 不动，X 指纹不受牵连）。
守卫 2 条（样本自带角色卡与依据 / judge 样本进自检且送对载荷）。

**全量审计（87 条 judge 样本，非抽样；老样本缺字段 → 按确定性卡回放补齐载荷）**：

| 类别 | 总数 | 校验判"问题类型不成立" |
| --- | --- | --- |
| ooc | 31 | **17（55%）** |
| setting | 26 | **10（38%）** |
| confab | 30 | **9（30%）** |
| **合计** | **87** | **36（41%）** |

**校验器可信度对照（关键，否则这数只是"一家之言"）**：项目**精编语料**（手工锚定、逐条有依据）
的 ooc 判不成立 **0/40**，而工厂批 **17/31** ⇒ **不是尺子偏严**。（对照语料里 setting/confab 的少数告警
多为该脚本给的材料为空所致 = 脚本口径问题，非校验器问题。）

**机理（与 ⑦ 同家族）**：程序侧只做**字面**检查，标签却是**语义**的 ——
· `ooc`：需演绎器真的让**被声明的说话人**说出与角色卡冲突的语气；实测常写成别的角色在说话，
  且包内 NPC（角色卡来源）与名字池角色是**两套名字宇宙**；
· `setting`：程序只查"新值出现 + 原值消失"，新值可能被用在无关位置；
· `confab`：程序只查"anchor 在位"，叙事可能只是**转述/对话提及**而非"当作既成事实断言"。

**处置**：人读表 `reports/judge-manual-review-20260913.md`（三类共 87 条，**36 条疑似排最前**，含校验器理由）；
**不自动剔除**——confab 的"疑似"里含校验器偏严的可能（把对话中的断言判成非断言），**人读为准**。
**建议下一步（待拍板）**：把标签自检从"事后抽检"提升为**生成时门禁**（不成立即重演一次；预期 +¥1~2/批），
这才是"标签级缺陷"的根治——否则每次都要靠事后人读兜。

### ⑧ 的根治已落地：标签门禁前置到生成时（2026-09-13）

**实现**：`_label_gate()`（判"叙事/历史是否真的承载了标签所述内容"）+ `_verbalize_until_label_ok()`
（演绎 → 校验 → 不通过**重演一次** → 仍不通过丢卡）；`LABEL_GATE_ATTEMPTS = 2`；
未判定**不算通过**（丢卡，原因 `标签未判定`）；extract 负例卡无标签 → 不调用校验器；
三个构建器（extract/judge/compress）全部改走这条路，丢卡原因随 `dropped-{layer}.json` 留档。
守卫 +4 条（重演后通过 / 两次不成立丢卡 / 未判定不算通过 / 负例卡不调用）。

**夹具顺带改造**：`StubLLM` 新增 `label_verdicts` 开关与 `label_calls` —— 标签校验走**专线**
（不消耗主队列、不计入 `self.calls`），否则每条存量守卫的"调用次数/队列顺序"断言都会被这次额外调用打乱
（实测：改前 15 条守卫红）。默认一律判"通过"（现实默认），专门守卫用开关驱动判定。

**真机验证（dev 层重跑）**：

| 指标 | 门禁前 | 门禁后 |
| --- | --- | --- |
| judge 批判"问题类型不成立" | **36/87 = 41%** | **2/24 = 8%**（残余 = 校验器复查抖动，噪声底） |
| 其中 ooc | **17/31 = 55%** | **0/9 = 0%** ✅ |
| dev 出库 | 86 | 81（门禁丢 6 张救不回的 + 候选全杀 3） |
| 成本 | — | **+≈¥0.2/层**（`label_check` 单次 ¥0.00224 × 91 次）；整批 ¥0.881，与无门禁时持平 |

**顺带暴露的 X 协议问题（待拍板）**：重跑后重算 X，`diff` 又呈**两极**（P50 = 0、P95 = max = **100pp**）——
因为两条轨道测的**不是同一件事**：轨道 1 = anchor 在位、轨道 2 = 要点能否复原。
实测 `sc-22025-0025`：要点「玩家答应把黄铜齿轮转交柳明夷」，摘要里 `黄铜齿轮`/`柳明夷` **都在**，
却写着"青鳞尚未前往接触"（**承诺本身没写**）⇒ 轨道 1 给 1.00、轨道 2 给 0.00。
这类样本**恰恰应该被标记为冲突**，而 `X = 100pp` 让标记永不触发 ⇒
**建议 X 的定义从"P95 分位"改为"一档容差 = 50pp"**（保真三档折半后，一档正好 50pp；超过即"两级对立"= 真冲突），
并在 spec 里记明"P95 在双峰分布上退化"这一实测反例。**未获拍板前不改协议**（判据封板纪律）。

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

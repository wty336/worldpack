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

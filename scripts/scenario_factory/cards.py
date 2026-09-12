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

# 发现① 修复（2026-09-13 验收）：`card_id = sc-{seed}-{seq}` **不含模块**，
# 而三模块原先共用 `seed_base + i` → 同层里 extract/judge/compress 的第 i 张卡 **id 完全相同**
# （实测 train 69 条只有 29 个唯一 id；质检剔除按 id 过滤于是**连坐**，实剔 3 条只报 1 条）。
# 修法：在**本层段内**按模块取千位偏移 —— 段宽 10000，生产规模 extract 1000 / judge 800 /
# compress 300 各占不到一段，故偏移后仍稳定落在本层（决策 19 的种子空间纪律不破）。
MODULE_SEED_OFFSET = {"extract": 0, "judge": 1000, "compress": 2000}


def layer_of(seed: int) -> str:
    for name, space in SEED_SPACES.items():
        if seed in space:
            return name
    raise ValueError(f"seed 不在 train/dev/eval 任一空间: {seed}")


def card_seed(seed_base: int, i: int, module: str) -> int:
    """层内按模块错开 seed → 保证 `card_id` 在本层唯一（发现①）。

    调用方**统一走这里**（`assemble._build_module` 与 dry-run 同源）；偏移越界即报错，
    不静默回退——静默回退正是发现① 的成因。
    """
    if module not in MODULE_SEED_OFFSET:
        raise ValueError(f"未知模块（无 seed 偏移）：{module!r}")
    seed = seed_base + MODULE_SEED_OFFSET[module] + i
    if layer_of(seed) != layer_of(seed_base):
        raise ValueError(
            f"模块偏移把 seed 推出了本层段：seed={seed} 属 {layer_of(seed)} 层，"
            f"而 base={seed_base} 属 {layer_of(seed_base)} 层（模块 {module} 需换层段或缩批量）")
    return seed


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


# ---------------------------------------------------------------------------
# 轴空间与确定性生成器（Task 2）
# ---------------------------------------------------------------------------

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
# 去专名的原因：recent 虽不渲染进材料（只作 rank_facts/select_lore 的打分输入），
# 但若带上本卡专名，就会污染检索命中、且语义上与"材料代表最近玩家发言"不符；
# 对 confab 卡更是要保持"材料对该承诺零信号"。

# judge 卡必带 pack 的题材 → 该包现有 NPC（材料装配用；无映射的用中性占位）
NPC_BY_PACK = {"xianxia_wendao": "bai_zhi", "urban_neon": "lin_che",
               "ancient_jianghu": "shen_qingqiu"}
DEFAULT_JUDGE_NPC = "station_chief"  # G1 待造，先用占位（材料装配排在 G1 之后）


def judge_genres_for(layer: str) -> list[str]:
    """可出 judge 卡的题材 = **有真实包可物化**的题材（§4.1）+ 留出轴纪律（决策 19）。

    两条约束缺一不可：

    1. **包必须存在**：judge 卡的材料要由真实世界包物化，映射到不存在的包（如 G1 待造）
       只会在材料装配时炸 —— 出了卡也是废卡；
    2. **留出轴只对 eval 开放**：`PACK_BY_GENRE` 里含留出轴「民国谍战」，若把它放进
       train/dev 的重映射候选，约 14% 的 train judge 卡会变成留出轴（违反决策 19）。
       **G1 就绪前 eval 也拿不到**（第 1 条已把它滤掉）——judge 侧的留出轴覆盖等 G1。
    """
    avail = [g for g, p in PACK_BY_GENRE.items() if (REPO_ROOT / p).is_dir()]
    return avail if layer == "eval" else [g for g in avail if g != GENRE_EVAL_ONLY]


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
    等于撞上 facts[0] 的槽（见 generate_card 注释）。"""
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
        base["axes"] = axes.model_copy(update={"input_form": form})
        return ScenarioCard(
            facts=facts,
            old_summary="【剧情摘要】" + f"此前玩家已与{n2}相识，旧事略。" if form == "增量合并" else "",
            preserve_points=[PreservePoint(text=f.text, anchors=f.anchors) for f in facts[:2]],
            events=[f"事件{i}：玩家与{n2}周旋" for i in range(3)], **base)
    return ScenarioCard(facts=facts, **base)  # reflect（照造，不进第一批训练）

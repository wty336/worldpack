"""agent-first 第 5 件：事实图 Judge 的代码层（设计 docs/design-factgraph.md）。

两级判定：LLM 抽"既成事实断言"（claims）→ 代码查"缺席"（absence）。
- 图从**材料同源真值**构建（pack + state），不解析材料字符串；
- 只查"缺席"不查"矛盾"：confab 缺口 = "材料没有这条 → 属编造"，df 查表即可判；
- 不含 flags（引擎真值纪律）；goal 是引导不是真值，不入图；
- 失败静默：抽取调用异常/空 → 无违规（LLM 三态判定照常兜底）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .budgets import complete_with_empty_retry

FACTCHECK_MAX_TOKENS = 500

FACTCHECK_SYSTEM = (
    "你是事实断言抽取器。从给定的游戏叙事中，抽取关于「玩家」或已出场角色的"
    "**既成事实断言**——把某事当作已经成立的事实来陈述的内容：承诺/约定/债务/"
    "身份身世/属性能力/重要事件。只抽含具体专名或数字的断言；"
    "场景描写、氛围渲染、寒暄客套、比喻夸张、人物对白的套话都不算；"
    "**背景铺垫与顺带回忆也不算**（如『按师父留下的剑谱练剑』只是背景展开，"
    "不是新确立的承诺/事实）；**玩家当前的状态描述也不算**（身上有多少银两/"
    "当前属性——状态由引擎维护，不可能是「新确立的事实」）；"
    "**NPC 以第一人称自述的个人过往趣事/糗事（闲聊八卦）也不算**；"
    "但**关于外部世界或第三方的具体事件陈述算**（某处发生了什么/发生了什么变化，"
    "含地点与数量的，如『丹峰的丹炉今天熄过两次火』）。"
    "每条输出该断言的**内容要点**（专名/数字/实质内容，去掉「你」「答应」「曾经」"
    "等引导词与虚词），如「欠五十两，中秋前归还」「把密码本交给白鸮」；"
    "没有任何断言就只输出「无」。"
)

# 锚点排除词：高频功能词/表态动词——它们出现在任何断言里，不能当"接地"或"缺席"证据
STOP_TOKENS = {
    "玩家", "自己", "我们", "他们", "这个", "那个", "什么", "怎么", "可以",
    "需要", "必须", "已经", "曾经", "决定", "打算", "准备", "答应", "承诺",
    "约定", "今天", "明天", "现在", "此时", "这时", "然后", "于是", "突然",
    "发现", "知道", "想起", "记得", "开始", "继续", "一起", "以及", "因为",
    "所以", "但是", "如果", "不能", "不会", "不是", "没有", "还有", "只是",
}


def _tokens(text: str) -> list[str]:
    """2~4 字窗口 token（中文零依赖切词；数字无论中阿写都自然落在窗口里）。"""
    text = re.sub(r"\s+", "", text)
    out: list[str] = []
    for size in (2, 3, 4):
        out += [text[i : i + size] for i in range(len(text) - size + 1)]
    return out


def _anchor_tokens(text: str) -> list[str]:
    """接地锚点 = 数字 + 「」引用串 + 2~4 字窗口（排除停用词）。

    口径收敛（E1 首跑实测）：**接地用全部 2~4 字窗口**——「听雨」「东市」「银两」
    这类 2 字专名必须能接地（只收 3~4 字会把它们全判成缺席 → 误报）；而"缺席"
    判据是"**没有任何锚点接地**"，2 字碎片只会帮接地、不会自己触发违规，
    故不存在伪锚点误报问题。
    """
    text = re.sub(r"\s+", "", text)
    out: list[str] = list(re.findall(r"\d+(?:\.\d+)?", text))
    out += re.findall(r"「([^」]+)」", text)
    for size in (2, 3, 4):
        out += [text[i : i + size] for i in range(len(text) - size + 1)]
    return [t for t in out if t not in STOP_TOKENS]


@dataclass
class FactGraph:
    """关键事实图：材料同源真值 → token 集合（接地查表用）。"""

    facts: list[str] = field(default_factory=list)
    token_set: set[str] = field(default_factory=set)  # 全部 2~4 字窗口 + 「」串
    quoted: set[str] = field(default_factory=set)  # 「」引用串（专名锚点）

    def has(self, tok: str) -> bool:
        return tok in self.token_set or tok in self.quoted


def build_graph(pack, state, history: list[dict] | None = None) -> FactGraph:
    """从世界包 + 状态构建事实图（与 status_text 材料同源；不含 flags）。

    设计加固 A1：history 提供时纳入**压缩摘要**与**关键选择日志**——
    长局中早期事实只活在摘要里，不入图会被缺席判定误报为"虚构"
    （证据面"该有的"必须有，宁可少拦不误拦）。choice_log 的选项文本
    是代码结算过的既成剧情，同理接地。
    """
    facts: list[str] = []
    if history is not None:
        from .compression import summary_text  # 局部导入：compression 不依赖本模块，无环

        summary = summary_text(history)
        if summary:
            facts.append(summary)
        facts += [c.text for c in state.choice_log]
    if state.player_facts:
        facts += [m.fact for m in state.player_facts if not m.superseded]  # A5：取代者才接地
    for npc_id in state.present_npcs:
        if npc_id in state.npc_memories:
            facts += [m.fact for m in state.npc_memories[npc_id] if not m.superseded]
    if getattr(pack.world, "lore", None):
        facts += [e.text for e in pack.world.lore]
    for npc_id in state.present_npcs:
        if npc_id in pack.npcs:
            npc = pack.npcs[npc_id]
            facts.append(f"{npc.name}，{npc.identity}")
    if state.scene:
        facts.append(state.scene)
    # 数值真值入图：叙事引用好感/属性/天数等数字不算编造
    for npc_id in state.present_npcs:
        if npc_id in state.affections and npc_id in pack.npcs:
            facts.append(f"好感 {pack.npcs[npc_id].name} {state.affections[npc_id]:g}")
    for k, v in state.stats.items():
        if k in pack.schedule.stats:
            facts.append(f"{pack.schedule.stats[k].label} {v:g}")

    token_set: set[str] = set()
    quoted: set[str] = set()
    for f in facts:
        quoted |= set(re.findall(r"「([^」]+)」", f))
        token_set |= set(_tokens(f))
    return FactGraph(facts=facts, token_set=token_set, quoted=quoted)


def parse_claims(output: str) -> list[str]:
    """解析断言抽取输出：容忍编号/项目符号/噪声行；「无」哨兵 → 空。"""
    claims: list[str] = []
    for line in (output or "").splitlines():
        line = re.sub(r"^\s*[\d一二三四五六七八九十]+[.、)）:：]\s*", "", line).strip()
        line = line.lstrip("-*·•").strip()
        line = line.rstrip("。.！!，,；;：: ")
        if not line or line in ("无", "没有", "无断言"):
            continue
        claims.append(line[:60])
    return claims


def check_graph(llm, narration: str, graph: FactGraph) -> str | None:
    """两级判定：抽取断言 → 代码查缺席。返回违规描述或 None。

    违规判据（v1 口径，E1 首跑后收敛）：
    - 断言的接地锚点（数字/「」串/2~4 字窗口，排除停用词）**全部不在图内** → 违规：
      材料里没有这条 → 属编造（confab 缺席证据，纯代码可判）；
    - 任一锚点接地 → 通过（2 字窗口参与接地——「东市纸铺」经「东市」接地）；
    - 抽取器只收"新确立的承诺/债务/事实"，背景铺垫不算（防师父剑谱类误报）；
    - 失败静默：抽取调用异常 → None（LLM 三态判定照常兜底）。
    """
    try:
        output = complete_with_empty_retry(
            llm,
            [
                {"role": "system", "content": FACTCHECK_SYSTEM},
                {"role": "user", "content": f"<叙事>\n{narration}\n</叙事>"},
            ],
            purpose="factcheck",
            max_tokens=FACTCHECK_MAX_TOKENS,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        return None
    for claim in parse_claims(output):
        anchors = _anchor_tokens(claim)
        if anchors and not any(graph.has(t) for t in anchors):
            return (
                f"问题类型：虚构事实：断言「{claim}」中的内容"
                f"（{anchors[0]}等）在给定材料中不存在"
            )
    return None

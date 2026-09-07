"""记忆显式化（M2a）：玩家长期事实 + NPC 对玩家的记忆（plan-m2.md §2）。

设计决策（依据 M1.5 基线数据）：
- 写入路径：叙事模型通过 remember 工具提议 → 引擎校验（去重/长度/目标合法性）→ 写入；
- 注入路径（A1，P1）：检索式注入——三因子打分 + 常驻区 + top-K（见 rank_facts）；
  - 玩家事实（player_facts）不再全量常驻状态栏；
  - NPC 记忆（npc_memories）在该 NPC 出场时按检索注入角色卡区块；
- 淘汰（A2，P1）：满额按 (importance, round) 加权——低重要性先淘汰，同重要性按时间衰减；
- 冲突：v1 允许新旧事实共存（append-only，Mem0 v3 思路），由时间淘汰与语义校验兜底；
- 去重（A4，P1）：包含关系 + bigram 预筛 + 轻量模型语义判定（见 _is_duplicate）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .state import GameState, MemoryEntry
from .worldpack import WorldPack

if TYPE_CHECKING:
    from .llm import LLMClient

PLAYER_FACTS_LIMIT = 24  # 玩家事实存储上限（检索注入后上限可放宽，仍保留淘汰机制）
FACT_MAX_LEN = 120
IMPORTANCE_DEFAULT = 5.0  # A2：事实重要性缺省值（1~10）

# A1（P1）检索参数：score = α·recency + β·importance + γ·relevance
RETRIEVAL_ALPHA = 0.4
RETRIEVAL_BETA = 0.4
RETRIEVAL_GAMMA = 0.2
RETRIEVAL_K = 10  # 检索区条数上限（玩家事实与 NPC 记忆共用）
PINNED_K = 3  # R1：常驻区 = 最高重要性的 K 条恒注入（M2a「常驻可见才存活」）

# A4（P1）语义去重提示词
DEDUP_SYSTEM = (
    "你是记忆去重判定器。判断「候选事实」是否与「既有事实」中的某一条语义重复"
    "（含义相同，仅措辞不同）。只输出：重复 或 不重复。"
)
DEDUP_MAX_TOKENS = 50

# 确定性提取兜底（M2a 迭代 4）：不依赖模型主动 remember，引擎强制提炼
EXTRACT_SYSTEM = (
    "你是事实提炼器。从给定的游戏回合内容中，提炼关于「玩家」的长期事实"
    "（身份、来历、名字、剑名、师承、喜好、承诺、约定、托付等）。"
    "每条输出一行，格式：重要性|事实，重要性为 1~10 的整数"
    "（8-10：身份身世、生死承诺、命运级转折；5-7：重要关系进展与关键事件；"
    "1-4：日常喜好与琐事）。"
    "不要编号、不要解释；没有值得长期记住的事实就只输出「无」。"
    "日常琐事（吃了什么、天气如何）不算事实；剧情进展的瞬时状态也不算。"
    "「已有事实」中已经存在的（含近义改写）不要重复输出。"
)
EXTRACT_MAX_FACTS = 5

# A3（P1）反思层：把零散记忆合成关系洞察
REFLECT_SYSTEM = (
    "你是关系洞察合成器。根据某 NPC 对玩家的近期记忆，合成 1~2 句关于"
    "「该 NPC 对玩家的态度与关系走向」的高层洞察（如『沈清秋对玩家的态度正从"
    "客气转向信任』）。每行一条，格式：洞察|来源编号（多个编号用逗号分隔，"
    "编号对应给定记忆列表中的序号）。只依据给定记忆，不得编造；"
    "没有足够信息就只输出「无」。"
)
REFLECT_MAX_TOKENS = 200
REFLECT_MIN_MEMORIES = 8  # 记忆达到该数量才值得反思
REFLECT_MATERIAL = 12  # 合成素材 = 最近 N 条记忆
INSIGHT_CAP = 2  # 每 NPC 保留的洞察条数（新替旧）


def parse_insights(output: str) -> list[tuple[str, tuple[int, ...]]]:
    """解析反思输出为 (洞察文本, 来源编号) 列表。容忍噪声，非法行跳过。"""
    insights: list[tuple[str, tuple[int, ...]]] = []
    for line in output.splitlines():
        line = re.sub(r"^\s*[\d一二三四五]+[.、)）]\s*", "", line).strip()
        line = line.lstrip("-*·•").strip()
        core = line.rstrip("。.！!，,；;：: ")
        if not core or core == "无" or core == "没有":
            continue
        text, sep, src = core.partition("|")
        text = text.strip()
        if not text:
            continue
        indices: list[int] = []
        if sep:
            for part in re.split(r"[，,、\s]+", src.strip()):
                part = part.strip()
                if part.isdigit():
                    indices.append(int(part))
        insights.append((text, tuple(indices)))
    return insights


def parse_facts(output: str) -> list[tuple[str, float]]:
    """解析提炼输出为 (事实, 重要性) 列表。

    支持两种行格式：
    - A2 格式「重要性|事实」（如 8|我的剑名听雨；无前缀 → 重要性 5）；
    - 容忍编号/项目符号/空行/「无」哨兵及其标点变体（M2b #3）。
    """
    facts: list[tuple[str, float]] = []
    for line in output.splitlines():
        line = re.sub(r"^\s*[\d一二三四五]+[.、)）]\s*", "", line).strip()
        line = line.lstrip("-*·•").strip()
        core = line.rstrip("。.！!，,；;：: ")
        if not core or core == "无" or core == "没有":
            continue
        importance = IMPORTANCE_DEFAULT
        if "|" in core:
            head, _, rest = core.partition("|")
            head = head.strip()
            if head.isdigit() and 1 <= int(head) <= 10:
                importance = float(int(head))
                core = rest.strip()
        if not core:
            continue
        facts.append((core, importance))
        if len(facts) >= EXTRACT_MAX_FACTS:
            break
    return facts


class MemoryError(Exception):
    """记忆写入契约违反（结构化回传给模型）。"""


class MemorySystem:
    def __init__(self, pack: WorldPack, llm: "LLMClient | None" = None):
        self.pack = pack
        self.llm = llm  # A4：语义去重用轻量模型（None = 关闭，离线测试不受影响）

    def add(
        self,
        state: GameState,
        target: str,
        fact: str,
        importance: float | None = None,
    ) -> str:
        """校验并写入一条记忆。target = 'player' 或 NPC id。返回结果消息（回传模型）。

        A2：importance 1~10（缺省 5），淘汰与检索加权。
        A4：字符串去重未命中时做语义去重（bigram 预筛 + 轻量模型，失败静默）。
        """
        fact = (fact or "").strip()
        if not fact:
            raise MemoryError("fact 不能为空")
        if len(fact) > FACT_MAX_LEN:
            raise MemoryError(f"fact 过长（{len(fact)} 字，上限 {FACT_MAX_LEN}）")

        if importance is None:
            importance = IMPORTANCE_DEFAULT
        if isinstance(importance, bool) or not isinstance(importance, (int, float)):
            raise MemoryError(f"importance 必须是 1~10 的数字，当前为 {importance!r}")
        if not 1 <= importance <= 10:
            raise MemoryError(f"importance 必须在 1~10 之间，当前为 {importance:g}")
        importance = float(importance)

        if target == "player":
            bucket = state.player_facts
            limit = PLAYER_FACTS_LIMIT
            label = "玩家事实"
        elif target in self.pack.schedule.affections:
            bucket = state.npc_memories.setdefault(target, [])
            limit = self.pack.npcs[target].memory_limit
            label = f"{self.pack.npcs[target].name} 的记忆"
        else:
            raise MemoryError(
                f"未知目标 '{target}'（可用：player 或 "
                f"{', '.join(sorted(self.pack.schedule.affections))}）"
            )

        # 去重：包含关系（v1）+ 语义判定（A4）
        for m in bucket:
            if m.fact in fact or fact in m.fact:
                return f"[记忆跳过] 与既有记忆重复（{m.fact}）"
        if self._is_semantic_duplicate(bucket, fact):
            return "[记忆跳过] 与既有记忆语义重复"

        entry = MemoryEntry(fact=fact, day=state.day, round=state.turn_count,
                            importance=importance)
        bucket.append(entry)

        # 满额淘汰（A2）：低重要性先淘汰，同重要性按时间衰减（保留最新 limit 条）
        if len(bucket) > limit:
            bucket.sort(key=lambda m: (m.importance, m.round))
            removed_count = len(bucket) - limit
            del bucket[:removed_count]
            return (
                f"[记忆已写入] {label}：{fact}（重要性 {importance:g}）"
                f"（满额，按重要性+时间淘汰 {removed_count} 条旧记忆）"
            )
        return f"[记忆已写入] {label}：{fact}（重要性 {importance:g}）"

    # ------------------------------------------------------------------
    # A4：语义去重
    # ------------------------------------------------------------------

    def _is_semantic_duplicate(self, bucket: list[MemoryEntry], fact: str) -> bool:
        """字符串去重未命中后的语义判定。bigram 预筛 + 轻量模型；失败静默（不判重）。"""
        if self.llm is None or not bucket:
            return False
        candidate = _bigrams(fact)
        similar = [m for m in bucket if _bigrams(m.fact) & candidate]
        if not similar:  # 无任何字符二元组重叠 → 语义重复概率极低，跳过调用省成本
            return False
        try:
            output = self.llm.complete(
                [
                    {"role": "system", "content": DEDUP_SYSTEM},
                    {
                        "role": "user",
                        "content": "<既有事实>\n" + "\n".join(m.fact for m in similar)
                        + f"\n</既有事实>\n\n<候选事实>\n{fact}\n</候选事实>",
                    },
                ],
                max_tokens=DEDUP_MAX_TOKENS,
                temperature=0.0,
                purpose="dedup",
            )
        except Exception:  # noqa: BLE001
            return False  # 失败静默降级：视为不重复，写入路径不因去重失败而中断
        return "重复" in output and "不重复" not in output


def _bigrams(text: str) -> set[str]:
    """字符二元组集合（中文无分词，二元组是零依赖的相似度代理）。"""
    text = re.sub(r"\s+", "", text)
    return {text[i : i + 2] for i in range(len(text) - 1)}


# ---------------------------------------------------------------------------
# A1：检索式注入
# ---------------------------------------------------------------------------


def rank_facts(
    entries: list[MemoryEntry],
    context: str,
    now_round: int,
    k: int = RETRIEVAL_K,
    pinned: int = PINNED_K,
    alpha: float = RETRIEVAL_ALPHA,
    beta: float = RETRIEVAL_BETA,
    gamma: float = RETRIEVAL_GAMMA,
) -> list[MemoryEntry]:
    """检索式注入（A1）：常驻区（top importance）+ 检索区（三因子打分 top-K）。

    score = α·recency + β·importance + γ·relevance
    - recency：1 / (1 + 回合龄)，新近事实得分高；
    - importance：1~10 归一化到 0~1；
    - relevance：与上下文字符二元组重叠率（v1 关键词近似，v2 换 embedding）。
    返回顺序：常驻区在前（按重要性降序），检索区在后（按分数降序）。
    """
    if not entries:
        return []
    ctx_bigrams = _bigrams(context) if context else set()
    now = max(now_round, 0)

    def relevance(m: MemoryEntry) -> float:
        mb = _bigrams(m.fact)
        if not ctx_bigrams or not mb:
            return 0.0
        return len(mb & ctx_bigrams) / len(mb)

    def score(m: MemoryEntry) -> float:
        recency = 1.0 / (1.0 + max(0, now - m.round))
        imp = m.importance / 10.0
        return alpha * recency + beta * imp + gamma * relevance(m)

    pinned_entries = sorted(entries, key=lambda m: (-m.importance, -m.round))[:pinned]
    pinned_ids = {id(m) for m in pinned_entries}
    rest = sorted(
        (m for m in entries if id(m) not in pinned_ids),
        key=score,
        reverse=True,
    )[:k]
    return pinned_entries + rest

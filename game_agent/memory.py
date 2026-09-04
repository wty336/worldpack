"""记忆显式化（M2a）：玩家长期事实 + NPC 对玩家的记忆（plan-m2.md §2）。

设计决策（依据 M1.5 基线数据）：
- 写入路径：叙事模型通过 remember 工具提议 → 引擎校验（去重/长度/目标合法性）→ 写入；
- 注入路径：
  - 玩家事实（player_facts）**常驻状态栏**——基线中被强化的事实存活、一次性事实被埋没，
    常驻注入就是"强化"的机制化复刻（f2/f3 存活的原理）；
  - NPC 记忆（npc_memories）在该 NPC 出场时注入角色卡区块；
- 淘汰：满额按时间衰减保留最新（v1 不做"被引用次数"加权——语义判断留给 M2b 的 Judge）；
- 冲突：v1 允许新旧事实共存（append-only，Mem0 v3 思路），由时间淘汰与 M2b 语义校验兜底；
- 去重：包含关系视为重复（"剑名听雨" ⊂ "我的剑名听雨，师父传的"）。
"""

from __future__ import annotations

from .state import GameState, MemoryEntry
from .worldpack import WorldPack

PLAYER_FACTS_LIMIT = 8  # 玩家事实常驻上限（状态栏区块，不宜过长）
FACT_MAX_LEN = 120


class MemoryError(Exception):
    """记忆写入契约违反（结构化回传给模型）。"""


class MemorySystem:
    def __init__(self, pack: WorldPack):
        self.pack = pack

    def add(self, state: GameState, target: str, fact: str) -> str:
        """校验并写入一条记忆。target = 'player' 或 NPC id。返回结果消息（回传模型）。"""
        fact = (fact or "").strip()
        if not fact:
            raise MemoryError("fact 不能为空")
        if len(fact) > FACT_MAX_LEN:
            raise MemoryError(f"fact 过长（{len(fact)} 字，上限 {FACT_MAX_LEN}）")

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

        # 去重：包含关系视为重复（v1 合并策略）
        for m in bucket:
            if m.fact in fact or fact in m.fact:
                return f"[记忆跳过] 与既有记忆重复（{m.fact}）"

        entry = MemoryEntry(fact=fact, day=state.day, round=state.turn_count)
        bucket.append(entry)

        # 满额淘汰：时间衰减，保留最新 limit 条
        if len(bucket) > limit:
            bucket.sort(key=lambda m: m.round)
            removed_count = len(bucket) - limit
            del bucket[:removed_count]
            return (
                f"[记忆已写入] {label}：{fact}"
                f"（满额，按时间衰减淘汰 {removed_count} 条旧记忆）"
            )
        return f"[记忆已写入] {label}：{fact}"

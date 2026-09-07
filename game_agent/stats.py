"""数值系统（W3）：change_stat 契约——LLM 提议，代码校验执行（design.md §5.2）。

两条路径严格分离：
- apply_change：LLM 提议路径。受增量上限约束、reason 必填、越界拒绝（不静默截断）；
- apply_effects：世界包作者定义的效果（日程/事件/关键选择结算），只做声明校验。
  D3（P2）：效果值支持收益曲线 {base/spread/decay_every/decay_step}。
  越界语义（C-6 统一口径）：世界包效果路径一律**饱和**——先截断 delta
  （delta = 边界 - before）再算 after，记录实际生效 delta 与「已达边界」标注；
  审计不变量 after == before + delta 恒成立。
所有变更都写入 stat_log（append-only），供零偏差审计回放。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .state import GameState, StatChangeRecord
from .worldpack import EffectSpec, ScheduleSpec

# 引擎级增量上限（仅约束 LLM 提议路径）：防注入刷数值与"一高兴加 50 好感"
AFFECTION_DELTA_MAX = 5  # 好感单次增减幅度上限
STAT_DELTA_MAX = 10  # 玩家属性（含银两）单次增减幅度上限


class StatChangeError(Exception):
    """change_stat 契约违反。信息会被结构化回传给 LLM 作为失败的工具结果。"""


@dataclass(frozen=True)
class StatChangeResult:
    ok: bool
    message: str
    value: float | None = None


class StatsSystem:
    """绑定一个世界包的数值真值系统。所有读写都经过它。"""

    def __init__(self, spec: ScheduleSpec):
        self.spec = spec

    # ------------------------------------------------------------------
    # LLM 提议路径
    # ------------------------------------------------------------------

    def apply_change(
        self,
        state: GameState,
        target: str,
        stat: str,
        delta: float,
        reason: str,
    ) -> StatChangeResult:
        """change_stat 工具契约。失败抛 StatChangeError（而非静默修正）。"""
        # 1) reason 必填（参数作 checklist：倒逼 LLM 想清楚为什么加）
        if not reason or not reason.strip():
            raise StatChangeError("reason 不能为空：必须说明这次数值变化的原因")
        reason = reason.strip()

        # 2) 定位目标容器与增量上限
        if target == "player":
            if stat not in self.spec.stats:
                raise StatChangeError(
                    f"未知属性 '{stat}'；可用属性: {', '.join(self.spec.stats)}"
                )
            limit = STAT_DELTA_MAX
            container = state.stats
            spec_entry = self.spec.stats[stat]
            stat_key = stat
        else:
            if target not in self.spec.affections:
                raise StatChangeError(
                    f"未知好感对象 '{target}'；可用对象: {', '.join(self.spec.affections)}"
                )
            if stat != "affection":
                raise StatChangeError("对 NPC 只能修改好感（stat 应为 'affection'）")
            limit = AFFECTION_DELTA_MAX
            container = state.affections
            spec_entry = self.spec.affections[target]
            stat_key = target  # 好感容器以 NPC id 为键

        # 3) delta 合法性
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            raise StatChangeError(f"delta 必须是数字，当前为 {delta!r}")
        if delta == 0:
            raise StatChangeError("delta 不能为 0")
        if abs(delta) > limit:
            raise StatChangeError(
                f"单次变化幅度超过上限（|delta| ≤ {limit}），当前为 {delta}"
            )

        # 4) 边界：拒绝而非静默截断（参数保真原则，design.md §10）
        before = container[stat_key]
        after = before + delta
        if after > spec_entry.max or after < spec_entry.min:
            raise StatChangeError(
                f"数值越界：{spec_entry.label} 当前 {before:g}，变化 {delta:+g} 后为 {after:g}，"
                f"允许范围 [{spec_entry.min:g}, {spec_entry.max:g}]"
            )

        # 5) 执行 + 审计（append-only）
        container[stat_key] = after
        state.stat_log.append(
            StatChangeRecord(
                day=state.day,
                target=target,
                stat=stat,
                delta=float(delta),
                reason=reason,
                before=before,
                after=after,
            )
        )
        return StatChangeResult(
            ok=True,
            message=f"{spec_entry.label} {before:g} → {after:g}（{delta:+g}，理由：{reason}）",
            value=after,
        )

    # ------------------------------------------------------------------
    # 代码路径：世界包定义的确定性效果
    # ------------------------------------------------------------------

    def _resolve_delta(self, value: Any, current: float, rng: random.Random) -> float:
        """解析一个效果值为实际 delta。

        - 普通数字：确定性，直接返回；
        - 收益曲线（EffectSpec/dict）：base 经边际递减 + ±spread 随机，符号截断
          （正收益 ≥0、负收益 ≤0，无负循环）。
        越界由 apply_effects 统一饱和（世界包效果是游戏机制，不是玩家声明）。
        """
        if not isinstance(value, (dict, EffectSpec)):
            return float(value)
        spec = value if isinstance(value, EffectSpec) else EffectSpec(**value)
        base = spec.base
        if spec.decay_every > 0:
            penalty = (current // spec.decay_every) * spec.decay_step
            if base >= 0:
                base = max(0.0, base - penalty)
            else:
                base = min(0.0, base + penalty)
        if spec.spread > 0:
            base += rng.uniform(-spec.spread, spec.spread)
        delta = round(base, 1)
        if spec.base >= 0:
            delta = max(0.0, delta)
        else:
            delta = min(0.0, delta)
        return delta

    def apply_effects(
        self, state: GameState, effects: dict, rng: random.Random | None = None
    ) -> list[str]:
        """应用 effects 字典（stats/affections/flags），返回变更描述列表。

        只用于作者定义的效果（日程/事件/关键选择），不做增量上限约束。
        越界统一**饱和**（记录实际生效 delta）——世界包效果是游戏机制，饱和优于
        让玩家一次送礼在 97 好感时炸档；LLM 提议路径（apply_change）仍严格拒绝。
        """
        rng = rng or random.Random()
        notes: list[str] = []
        for stat, value in effects.get("stats", {}).items():
            entry = self.spec.stats.get(stat)
            if entry is None:
                raise StatChangeError(f"效果引用了未声明的属性 '{stat}'")
            delta = self._resolve_delta(value, state.stats[stat], rng)
            before = state.stats[stat]
            capped = False
            if before + delta > entry.max:
                delta, capped = entry.max - before, True
            elif before + delta < entry.min:
                delta, capped = entry.min - before, True
            if delta == 0:
                continue  # 已在边界，无实际变化
            after = before + delta  # after == before + delta 恒成立（审计不变量）
            state.stats[stat] = after
            state.stat_log.append(
                StatChangeRecord(
                    state.day, "player", stat, float(delta), "worldpack effect", before, after
                )
            )
            notes.append(f"{entry.label} {delta:+g}" + ("（已达边界）" if capped else ""))

        for npc_id, value in effects.get("affections", {}).items():
            entry = self.spec.affections.get(npc_id)
            if entry is None:
                raise StatChangeError(f"效果引用了未声明的好感对象 '{npc_id}'")
            delta = self._resolve_delta(value, state.affections[npc_id], rng)
            before = state.affections[npc_id]
            capped = False
            if before + delta > entry.max:
                delta, capped = entry.max - before, True
            elif before + delta < entry.min:
                delta, capped = entry.min - before, True
            if delta == 0:
                continue
            after = before + delta
            state.affections[npc_id] = after
            state.stat_log.append(
                StatChangeRecord(
                    state.day, npc_id, "affection", float(delta), "worldpack effect",
                    before, after,
                )
            )
            notes.append(f"{entry.label} 好感 {delta:+g}" + ("（已达边界）" if capped else ""))

        for flag, value in effects.get("flags", {}).items():
            if flag not in self.spec.flags:
                raise StatChangeError(f"效果引用了未声明的 flag '{flag}'")
            state.flags[flag] = bool(value)
            notes.append(f"flag {flag} → {value}")

        return notes

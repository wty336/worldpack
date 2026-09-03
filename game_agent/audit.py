"""数值零偏差审计（W8）：回放 stat_log，与初始值和最终状态三方对账。

原理（design.md §11 + W8 验收）：
- 初始值 = 世界包 schedule 定义；
- stat_log 只增不改，每条记录满足 after == before + delta；
- 按日志顺序回放，末值必须与 state 当前值完全一致。
任何偏差都意味着有未经过数值系统的写入（篡改/绕过），返回偏差清单。
"""

from __future__ import annotations

from .state import GameState
from .worldpack import WorldPack


def audit_stats(pack: WorldPack, state: GameState) -> list[str]:
    deviations: list[str] = []

    expected_stats = {k: float(v.initial) for k, v in pack.schedule.stats.items()}
    expected_aff = {k: float(v.initial) for k, v in pack.schedule.affections.items()}

    for r in state.stat_log:
        if r.after != r.before + r.delta:
            deviations.append(f"日志记录内部不一致: {r}")
            continue
        if r.target == "player":
            if r.stat not in expected_stats:
                deviations.append(f"日志引用了未声明属性 '{r.stat}': {r}")
            else:
                expected_stats[r.stat] = r.after
        else:
            if r.target not in expected_aff:
                deviations.append(f"日志引用了未声明好感对象 '{r.target}': {r}")
            else:
                expected_aff[r.target] = r.after

    if expected_stats != state.stats:
        deviations.append(f"属性零偏差失败: 回放结果 {expected_stats} != 实际 {state.stats}")
    if expected_aff != state.affections:
        deviations.append(f"好感零偏差失败: 回放结果 {expected_aff} != 实际 {state.affections}")

    return deviations

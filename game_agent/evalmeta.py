"""评测元数据：小样本比例的可信区间 + 用例集指纹。

两个真实教训（2026-09-12）：

1. **点估计陷阱**：normal 误报率写成 `0/18 = 0%` 被读作"完美"，但 18 个样本的
   95% Wilson 区间是 **0~18%** —— 真值可能是 1/6 的误报率。门禁判据（≤10%）
   在 18 个样本上根本不可判，必须报区间。（这就是 judge 要把 normal 补到 ≥40 的原因。）
2. **数字要自证**：报告只记"跑过 confab"，改语料后就分不清这批数字来自哪一版、哪些用例。
   现在每次报告落 `case_set_digest`（实际跑到的用例 id 集的散列）。

用法：
    from game_agent.evalmeta import rate_with_interval, case_set_digest
    rate_with_interval(0, 18)   # {'hits': 0, 'n': 18, 'rate': 0.0, 'lo': 0.0, 'hi': 0.185, ...}
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

WILSON_Z = 1.96  # 95% 置信
DIGEST_LEN = 16


def wilson_interval(hits: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """比例 p = hits/n 的 Wilson score 区间。

    比正态近似的 Wald 区间更适合小样本与极端比例（如 0/18）：
    Wald 会给出 [0, 0]（假装确定），Wilson 给 [0, 0.185]（如实承认不确定）。
    ``n == 0`` → ``(0.0, 1.0)``（一无所知）。
    """
    if n <= 0:
        return 0.0, 1.0
    if hits < 0 or hits > n:
        raise ValueError(f"hits 越界: {hits}/{n}")
    p = hits / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / denom
    return max(0.0, center - half), min(1.0, center + half)


def rate_with_interval(hits: int, n: int, *, digits: int = 3) -> dict:
    """落盘用：点估计 + 区间（向上取整到 digits 位小数）。"""
    lo, hi = wilson_interval(hits, n)
    return {
        "hits": hits,
        "n": n,
        "rate": round(hits / n, digits) if n else None,
        "ci95_lo": round(lo, digits),
        "ci95_hi": round(hi, digits),
    }


def case_set_digest(case_ids: Iterable[str]) -> str:
    """对"实际跑到的用例 id 集合"取散列：同一批用例 → 同一指纹（顺序无关）。

    与 `eval-sets/digests.json`（文件内容指纹）互补：那个锁**尺子文件**，
    这个锁**这一次实际跑了哪些用例**（部分类别运行、增量扩语料时尤其重要）。
    """
    joined = "\n".join(sorted({str(c) for c in case_ids}))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:DIGEST_LEN]


def interval_is_conclusive(hits: int, n: int, threshold: float, *, side: str = "upper") -> bool:
    """判据在给定样本量下**可判**吗：区间是否整体落在门限的一侧。

    - ``side="upper"``（误报率类，要求 ≤ threshold）：区间上界 ≤ threshold 才算达标；
    - ``side="lower"``（拦截率类，要求 ≥ threshold）：区间下界 ≥ threshold 才算达标。
    否则样本量不足，应报"不可判"而不是"通过/不通过"。
    """
    lo, hi = wilson_interval(hits, n)
    return hi <= threshold if side == "upper" else lo >= threshold

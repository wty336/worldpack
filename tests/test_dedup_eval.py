"""dedup 评测的统计逻辑（离线单测）：正例拦截率 + **负例误拦率**。

背景：旧 `scripts/dedup_test.py` 只有 10 组正例——"全部判重复"的模型也能满分。
新口径必须同时看误拦率（`eval-sets/MANIFEST.md` §2 设计要点 1）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dedup_test.py"


def _load():
    spec = importlib.util.spec_from_file_location("dedup_test_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pairs(pos: int, neg: int) -> list[dict]:
    return (
        [{"id": f"p{i}", "expect_duplicate": True} for i in range(pos)]
        + [{"id": f"n{i}", "expect_duplicate": False} for i in range(neg)]
    )


def test_all_blocked_passes_intercept_but_fails_false_block():
    """极端情形：全部判重复 → 拦截率 100%，但误拦率 100% → 门禁必须失败。"""
    mod = _load()
    pairs = _pairs(10, 10)
    s = mod.evaluate(pairs, [True] * 20)
    assert s["intercept_rate"] == 1.0
    assert s["false_block_rate"] == 1.0
    assert s["gate_ok"] is False  # ← 旧口径下这里会"满分通过"


def test_healthy_mixed_results():
    mod = _load()
    pairs = _pairs(10, 10)
    intercepted = [True] * 9 + [False] + [False] * 9 + [True]  # 9/10 拦 + 1/10 误拦
    s = mod.evaluate(pairs, intercepted)
    assert s["intercept_rate"] == 0.9
    assert s["false_block_rate"] == 0.1
    assert s["gate_ok"] is True


def test_gate_boundaries():
    mod = _load()
    # 正好 0.80 拦截 / 正好 0.10 误拦 → 通过（阈值含端点）
    pairs = _pairs(10, 10)
    intercepted = [True] * 8 + [False] * 2 + [False] * 9 + [True]
    s = mod.evaluate(pairs, intercepted)
    assert (s["intercept_rate"], s["false_block_rate"], s["gate_ok"]) == (0.8, 0.1, True)
    # 拦截率 0.7 → 不通过
    s2 = mod.evaluate(pairs, [True] * 7 + [False] * 3 + [False] * 10)
    assert s2["gate_ok"] is False

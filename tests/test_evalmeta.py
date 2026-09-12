"""评测元数据（离线）：Wilson 区间 / 用例集指纹 / 判据可判性。

动机（2026-09-12）：
- `0/18` 被当作"误报率 0%，完美"，但它的 95% Wilson 区间是 **0~18.5%**，门限 10% 根本不可判；
- 报告需要自证"这批数字是哪些用例跑出来的"（增量扩语料 / 部分类别运行后尤其重要）。
"""

from __future__ import annotations

import pytest

from game_agent.evalmeta import (
    case_set_digest,
    interval_is_conclusive,
    rate_with_interval,
    wilson_interval,
)


def test_wilson_refuses_certainty_on_zero():
    """0/18 不是"完美 0%"：区间上界约 18.5%，横跨 10% 门限 → 不可判。"""
    lo, hi = wilson_interval(0, 18)
    assert lo == 0.0
    assert 0.17 < hi < 0.20
    assert interval_is_conclusive(0, 18, 0.10, side="upper") is False

    # 0/40 才勉强能判（上界 ~8.8% < 10%）
    lo40, hi40 = wilson_interval(0, 40)
    assert hi40 < 0.10
    assert interval_is_conclusive(0, 40, 0.10, side="upper") is True


def test_wilson_brackets_point_estimate():
    lo, hi = wilson_interval(18, 20)  # 90%
    assert lo < 0.90 < hi
    assert 0.65 < lo < 0.70  # 小样本下端明显更低
    assert interval_is_conclusive(18, 20, 0.80, side="lower") is False
    assert interval_is_conclusive(20, 20, 0.80, side="lower") is True  # 满分才勉强过


def test_wilson_edge_cases():
    assert wilson_interval(0, 0) == (0.0, 1.0)  # 一无所知
    with pytest.raises(ValueError):
        wilson_interval(5, 3)


def test_rate_with_interval_shape():
    r = rate_with_interval(0, 18)
    assert r == {"hits": 0, "n": 18, "rate": 0.0, "ci95_lo": 0.0, "ci95_hi": 0.176}
    assert rate_with_interval(0, 0)["rate"] is None


def test_case_set_digest_is_order_independent_and_sensitive():
    a = case_set_digest(["c1", "c2", "c3"])
    b = case_set_digest(["c3", "c1", "c2", "c2"])  # 顺序无关、重复无害
    assert a == b and len(a) == 16
    assert case_set_digest(["c1", "c2"]) != a  # 少一条就变

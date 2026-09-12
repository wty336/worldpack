"""`scripts/route_a_cost.py` 的守卫测试（离线，零 API）。

Task 11 Step 3 的成本回填要回答"花了多少钱、花在哪"，两条性质必须钉住：

1. **钱不能从表里消失**：认不出的用途标签必须进「其他」并列出，绝不静默丢弃
   （静默丢弃 = 成本表比实际低，而 ¥90 停批复盘门限正是靠这张表）；
2. **口径与引擎同源**：单条费用按 `game_agent.usage` 的价格表算，缓存命中/未命中分价，
   没有缓存字段时整体按未命中价（与 `UsageTracker.cost_report()` 逐项一致）。
"""

from __future__ import annotations

import pytest

from game_agent.usage import _price_of
from scripts.route_a_cost import COMPONENT_OF, entry_cost, format_md, summarize

# 产线上真的会被写入的用途标签（Task 11 给各调用点加的 purpose=）
PIPELINE_LABELS = (
    "verbalize_extract", "verbalize_judge", "verbalize_compress",
    "compress", "rubric_select", "rubric_quality", "rubric_score", "rubric_pairwise",
)


def test_every_pipeline_label_has_a_component_row():
    """标签 → §10.2 组件行必须**全覆盖**（漏一个就会掉进「其他」，分模块成本回填不出来）。"""
    missing = [lab for lab in PIPELINE_LABELS if lab not in COMPONENT_OF]
    assert not missing, f"这些标签没有组件归类：{missing}"
    # aux 是加标签之前的早期记录（演绎/质检混记），必须显式留档而不是当成正常分类
    assert "未分类" in COMPONENT_OF["aux"]


@pytest.mark.parametrize("model", ["deepseek-v4-flash"])
def test_entry_cost_matches_engine_price_table(model):
    """单条费用：命中/未命中分价；无缓存字段时整体按未命中价（与 usage.py 同口径）。"""
    with_cache = {"model": model, "purpose": "compress",
                  "prompt_tokens": 1000, "completion_tokens": 500,
                  "cache_hit_tokens": 400, "cache_miss_tokens": 600}
    expect = (400 * _price_of(model, "cache_hit") + 600 * _price_of(model, "cache_miss")
              + 500 * _price_of(model, "output")) / 1_000_000
    assert entry_cost(with_cache) == pytest.approx(expect)

    no_cache = {"model": model, "purpose": "compress",
                "prompt_tokens": 1000, "completion_tokens": 500}
    expect2 = (1000 * _price_of(model, "cache_miss")
               + 500 * _price_of(model, "output")) / 1_000_000
    assert entry_cost(no_cache) == pytest.approx(expect2)
    # 现场证据：本批全部未命中（缓存命中 0）——公式不能把未命中价算成命中价
    assert entry_cost(no_cache) > entry_cost({**no_cache, "cache_hit_tokens": 1000,
                                              "cache_miss_tokens": 0})


def test_summarize_keeps_unknown_labels_and_totals_every_row():
    """认不出的标签进「其他」；总计 = 逐行之和（**钱不能少算**）。"""
    rows = [
        {"model": "deepseek-v4-flash", "purpose": "verbalize_extract",
         "prompt_tokens": 100, "completion_tokens": 200},
        {"model": "deepseek-v4-flash", "purpose": "some_future_label",
         "prompt_tokens": 100, "completion_tokens": 200},
    ]
    s = summarize(rows)
    components = {k[0] for k in s["by_key"]}
    assert "其他·未分类（认不出的标签）" in components
    assert s["total"]["calls"] == 2
    assert s["total"]["prompt"] == 200 and s["total"]["completion"] == 400
    assert s["total"]["cost"] == pytest.approx(sum(entry_cost(r) for r in rows))


def test_format_md_reports_unit_cost_and_extrapolation():
    """报告要给"单位成本 × 生产规模"这一行 —— 全量批的成本判断靠它（而不是拍脑袋）。"""
    rows = [{"model": "deepseek-v4-flash", "purpose": "compress",
             "prompt_tokens": 10_000, "completion_tokens": 2_000} for _ in range(10)]
    md = format_md(summarize(rows), samples=10, target_samples=2100,
                   fingerprints={"budget_policy": "budgets.py@abc", "endpoint": "deepseek-v4-flash"})
    total = summarize(rows)["total"]["cost"]
    assert f"¥{total / 10:.5f}/样本" in md
    assert f"¥{total / 10 * 2100:.2f}" in md
    assert "budget_policy" in md and "endpoint" in md

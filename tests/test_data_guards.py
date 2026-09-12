"""数据守卫（离线）：配额 / 轴覆盖 / 近似去重。

这三个守卫都是为了回答两个问题而写的：
- **训练数据会不会又聚焦到少数世界包？** → `axis_coverage_check`（T∩E 按轴不相交 + 声明必须与事实一致）
- **评测数据能不能真的判？** → `eval_quota_check`（样本量够不够下判据）+ `near_dup_check`（防泄漏/模板同质）

2026-09-12 首次运行就各抓到真问题：配额 6/6 包未达标；结构轴 `mainline_band` 在 T 与 E
共享 `4-6`（未留出）；`ancient`/`xianxia` 两条 identity 用例是**同一句模板换人名**（相似度 0.84）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


quota = _load("eval_quota_check_under_test", "eval_quota_check.py")
axis = _load("axis_coverage_check_under_test", "axis_coverage_check.py")
near = _load("near_dup_check_under_test", "near_dup_check.py")


# ---------------------------------------------------------------- 配额守卫


def test_quota_flags_undersized_categories():
    problems = quota.check_pack({"ooc": 12, "setting": 20, "confab": 20, "normal": 14}, t1=9, confab_n=20)
    assert any(p.startswith("ooc 12/20") for p in problems)
    assert any(p.startswith("normal 14/40") for p in problems)
    assert any("对抗合计 52/60" in p for p in problems)
    assert any("T1 占比 45% < 50%" in p for p in problems)


def test_quota_passes_when_all_targets_met():
    counts = {"ooc": 20, "setting": 20, "confab": 20, "normal": 40}
    assert quota.check_pack(counts, t1=12, confab_n=20) == []


def test_quota_requires_half_t1():
    counts = {"ooc": 20, "setting": 20, "confab": 20, "normal": 40}
    assert quota.check_pack(counts, t1=9, confab_n=20) != []  # 45% < 50%
    assert quota.check_pack(counts, t1=10, confab_n=20) == []


# ------------------------------------------------------------ 轴覆盖守卫


FACTS = {
    "pack_a": {"era": "唐风", "stats": ["a"], "npc_count": 1, "mainline_nodes": 3,
               "npc_band": "1-2", "mainline_band": "4-6"},
    "pack_b": {"era": "未来", "stats": ["b"], "npc_count": 6, "mainline_nodes": 9,
               "npc_band": "6+", "mainline_band": "8-12"},
}


def test_axis_passes_when_holdout_disjoint():
    table = {
        "holdout_axes": ["era", "npc_band", "mainline_band"],
        "packs": {
            "pack_a": {"split": "train", **FACTS["pack_a"]},
            "pack_b": {"split": "eval", **FACTS["pack_b"]},
        },
    }
    assert axis.check(FACTS, table) == []


def test_axis_detects_holdout_overlap():
    """真实案例：结构轴两侧都是 4-6 → 该轴其实没留出。"""
    facts = {k: dict(v) for k, v in FACTS.items()}
    facts["pack_b"]["mainline_band"] = "4-6"
    table = {
        "holdout_axes": ["mainline_band"],
        "packs": {
            "pack_a": {"split": "train", **FACTS["pack_a"]},
            "pack_b": {"split": "eval", **facts["pack_b"]},
        },
    }
    problems = axis.check(facts, table)
    assert any("T∩E" in p and "4-6" in p for p in problems)


def test_axis_detects_claim_mismatch_and_unlisted_pack():
    table = {
        "holdout_axes": ["era"],
        "packs": {"pack_a": {"split": "train", "era": "唐风", "stats": ["a"], "npc_count": 99,
                             "mainline_nodes": 3}},
    }
    problems = axis.check(FACTS, table)
    assert any("npc_count" in p for p in problems)  # 声明 99 ≠ 实际 1
    assert any("未收录" in p for p in problems)  # pack_b 没声明


def test_axis_rejects_unknown_split():
    table = {"holdout_axes": ["era"],
             "packs": {"pack_a": {"split": "unknown", **FACTS["pack_a"]}}}
    assert any("split" in p for p in axis.check(FACTS, table))


# ------------------------------------------------------------ 近似去重守卫


def test_jaccard_basics():
    assert near.jaccard("把钥匙给你", "把钥匙给你") == 1.0
    assert near.jaccard("把钥匙给你", "明天去书店") == 0.0
    assert 0.5 < near.jaccard("沈清秋幽幽一叹：乞讨为生", "苏晚晴幽幽一叹：乞讨为生") < 1.0


def test_near_dup_reports_cross_source_and_same_source_high():
    """跨来源 ≥0.60 报；同来源 ≥0.85 也报（防"同句换人名"）；dedup 同源一律不比。"""
    items = [
        {"source": "judge:a", "id": "1", "text": "你把手套摘下来搭在机床上，袖口蹭了一道黑机油。"},
        {"source": "judge:a", "id": "2", "text": "你把手套摘下来搭在机床上，袖口蹭了一道黑机油。"},
        {"source": "extract", "id": "x", "text": "（日常）我把手套摘下来搭在机床上，袖口蹭了一道黑。"},
    ]
    hits = near.find_near_dups(items, 0.6)
    pairs = {(a["source"], b["source"]) for _, a, b in hits}
    assert ("judge:a", "judge:a") in pairs  # 同源 1.0 → 报（模板同质）
    assert ("judge:a", "extract") in pairs  # 跨来源 → 报


def test_near_dup_ignores_dedup_same_source():
    """dedup 集合的改写对是设计使然 → 同源不比。"""
    items = [
        {"source": "dedup", "id": "p1:fact", "text": "你欠李三的债还没还清。"},
        {"source": "dedup", "id": "p1:similar", "text": "你欠李三的债还没还清。"},
    ]
    assert near.find_near_dups(items, 0.6) == []


def test_near_dup_same_source_below_high_threshold():
    """同源但在 0.85 以下（如同钩子不同措辞）不报——允许重复覆盖同一卡面字段。"""
    items = [
        {"source": "judge:a", "id": "1", "text": "沈清秋当街拉住你的衣袖高声道：『非君不嫁！全长安都可作个见证！』"},
        {"source": "judge:a", "id": "2", "text": "沈清秋在东市的茶摊前拉住你的手，对商贩朗声说明白此事。"},
    ]
    assert near.find_near_dups(items, 0.6) == []


def test_near_dup_respects_threshold():
    items = [
        {"source": "s1", "id": "1", "text": "车间里机油味很重，手套搭在机床上。"},
        {"source": "s2", "id": "2", "text": "车间里机油味很重，手套搭在机床上。"},
    ]
    assert near.find_near_dups(items, 0.99) != []
    assert near.find_near_dups(items, 0.6) != []

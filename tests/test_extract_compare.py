"""`scripts/extract_compare.py` 的守卫测试（离线，零 API）。

这个工具存在的理由是**决策 20 的副作用**：极性修正把 `n_funds` 从正例改成负例，
于是"修正前跑的"两份基线报告（14B / flash）**不能直接读 summary 做前后对照**。
本文件守两件事：

1. **重切片语义**：判分只认**当前**标签 + 报告里的 `raw_output`，
   **绝不**信报告自带（旧口径）的 `passed` 字段；
2. **基线数字可复算**：两份基线报告在当前标签下必须重切出 spec §12.C 记录的数
   （14B `36/93` C 类、`42/93` 召回、负例 `33/33`）——数字对不上就是尺子又换了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.extract_compare import compare, format_md, load_cases, rescore, verdict

BASELINE_14B = Path("reports/extract-eval-20260912-141241.json")
BASELINE_FLASH = Path("reports/extract-eval-20260912-133829.json")

# 合成小集：一正一负，足以覆盖 A/B/C/N 四类判定
CASES = {
    "pos1": {
        "id": "pos1", "genre": "仙侠", "axis_kind": "物品与装备", "expect_empty": False,
        "must_recall": ["听雨"], "must_not_output": [], "importance_range": [1, 10],
        "turn": "玩家把佩剑改名为听雨", "existing": [],
    },
    "neg1": {
        "id": "neg1", "genre": "仙侠", "axis_kind": "无事实", "expect_empty": True,
        "must_recall": [], "must_not_output": [], "importance_range": [1, 10],
        "turn": "玩家喝了口茶，看了看天", "existing": [],
    },
}


def _report(rows: list[tuple[str, str]], passed_claim: bool = False) -> dict:
    """造一份假报告。`passed_claim=True` 时塞进旧口径的 passed 字段（应被忽略）。

    未列出的用例补「无」——`rescore` 要求**逐例对齐**（防静默缩分母），
    所以假报告也得是完整的一轮。
    """
    given = dict(rows)
    runs = [{"id": cid, "raw_output": given.get(cid, "无")} for cid in CASES for _ in range(3)]
    if passed_claim:
        for r in runs:
            r["passed"] = True
            r["expect_empty"] = not CASES[r["id"]]["expect_empty"]  # 旧标签：正负颠倒
    return {"cases": runs}


def test_rescore_ignores_stale_passed_field():
    """重切片只认当前标签 + raw_output：旧报告的 passed/expect_empty 一律不信。"""
    # 负例抽出事实 = 违规；即便旧报告自称 passed=True
    stats = rescore(_report([("neg1", "5|玩家喝了口茶")] * 3, passed_claim=True), CASES)
    assert stats["neg_runs"] == 3
    assert stats["neg_ok"] == 0
    assert stats["neg_facts"] == 3
    # 负例输出「无」= 正确
    assert rescore(_report([("neg1", "无")] * 3, passed_claim=True), CASES)["neg_ok"] == 3


def test_rescore_classifies_c_b_and_a():
    """正例 run 分类：0 条= C（保守判定）、有输出但漏项= B、全中= A。"""
    # 三 run 全「无」→ 该用例进 C 类清单
    s = rescore(_report([("pos1", "无")] * 3), CASES)
    assert s["pos_c_runs"] == 3 and s["pos_c_cases"] == ["pos1"]
    # 有输出但漏「听雨」→ B
    s = rescore(_report([("pos1", "3|玩家换了件衣服")] * 3), CASES)
    assert s["pos_b_runs"] == 3 and s["pos_c_runs"] == 0
    # 命中 → A，且两条封板判据同时成立才算达标（负例这一轮是干净的）
    s = rescore(_report([("pos1", "8|玩家把佩剑改名为听雨")] * 3), CASES)
    assert s["pos_hits"] == 3 and s["pos_c_runs"] == 0
    old = rescore(_report([("pos1", "无")] * 3), CASES)
    v = verdict(old, s)
    assert v["c_ok"] and v["recall_up"] and v["neg_ok"] and v["pass"]


def test_compare_reports_cleared_c_cases_and_markdown():
    """对照输出必须点名"治好了哪些恒「无」用例"——这是决定训练量的直接依据。"""
    old = rescore(_report([("pos1", "无")] * 3), CASES)
    new = rescore(_report([("pos1", "8|玩家把佩剑改名为听雨")] * 3), CASES)
    cmp = compare(old, new)
    assert cmp["c_cleared"] == ["pos1"] and cmp["c_new"] == []
    md = format_md(cmp)
    assert "C 类 run（输出 0 条）" in md and "pos1" in md
    assert "达标（进入「再定训练量」）" in md


def test_rescore_fails_loudly_on_bad_input():
    """缺 raw_output / 用例对不齐 → 报错，绝不静默缩分母（否则百分比会"变好看"）。"""
    with pytest.raises(ValueError, match="raw_output"):
        rescore({"cases": [{"id": "pos1"}]}, CASES)
    # 报告里多出冻结集没有的 id（分母会变大）
    ghost = {"cases": [{"id": cid, "raw_output": "无"} for cid in (*CASES, "ghost") for _ in range(3)]}
    with pytest.raises(ValueError, match="对不齐"):
        rescore(ghost, CASES)
    # 报告少了冻结集里的用例（分母会变小）——手工构造，_report 会自动补齐
    short = {"cases": [{"id": "pos1", "raw_output": "无"} for _ in range(3)]}
    with pytest.raises(ValueError, match="对不齐"):
        rescore(short, CASES)


def test_rescore_reproduces_recorded_baselines():
    """两份基线报告在当前标签下必须重切出 spec §12.C 记录的数（尺子没换的证据）。"""
    cases = load_cases()
    assert len(cases) == 42
    assert sum(1 for c in cases.values() if c["expect_empty"]) == 11

    b14 = rescore(_load(BASELINE_14B), cases)
    assert (b14["pos_runs"], b14["pos_hits"], b14["pos_c_runs"]) == (93, 42, 36)
    assert (b14["neg_ok"], b14["neg_runs"]) == (33, 33)
    assert len(b14["pos_c_cases"]) == 12  # 12 用例 × 3 run = 36

    bf = rescore(_load(BASELINE_FLASH), cases)
    assert (bf["pos_runs"], bf["pos_hits"], bf["pos_c_runs"]) == (93, 89, 3)
    assert (bf["neg_ok"], bf["neg_runs"]) == (31, 33)


def _load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))

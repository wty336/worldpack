"""extract 直接评测的判分逻辑（离线单测）。

判分规则（`docs/plan-phase1-data.md` §3 的 B 块）：
- **召回**：`must_recall` 的关键实体必须被抽出的事实覆盖（同义表述容忍 → 实体子串判定）
- **去重纪律**：`must_not_output`（已有事实）不得被重复输出
- **负例**：`expect_empty=true` 的回合应输出「无」→ `parse_facts` 得空列表
- **重要性**：承载关键实体的事实，其重要性应落在 `importance_range` 内
- **疑似编造**（诊断项，不作为门禁）：抽出事实与输入的大二元组重合率过低 → 计一条

本文件只测纯函数；真机跑分见 `scripts/extract_eval.py`。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "extract_eval.py"


def _load():
    spec = importlib.util.spec_from_file_location("extract_eval_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CASE = {
    "id": "t_case",
    "genre": "太空科幻",
    "axis_kind": "债务与人情",
    "expect_empty": False,
    "existing": ["玩家是退役领航员"],
    "turn": "（第 5 日）你用一块旧能源电池抵了 1200 信用点的债务。",
    "must_recall": ["1200"],
    "must_not_output": ["领航员"],
    "importance_range": [5, 9],
}


def test_score_case_full_hit():
    mod = _load()
    facts = [("玩家用旧能源电池抵了 1200 信用点的债务", 7.0)]
    r = mod.score_case(CASE, facts)
    assert r["recalled"] is True and r["missing"] == []
    assert r["dedup_ok"] is True and r["dedup_violations"] == []
    assert r["importance_ok"] is True
    assert r["suspected_fabrication"] == []


def test_score_case_missing_entity():
    mod = _load()
    facts = [("玩家用旧能源电池抵了一笔债务", 7.0)]
    r = mod.score_case(CASE, facts)
    assert r["recalled"] is False and r["missing"] == ["1200"]


def test_score_case_dedup_violation():
    mod = _load()
    facts = [("玩家是退役领航员", 6.0), ("玩家抵了 1200 信用点债务", 7.0)]
    r = mod.score_case(CASE, facts)
    assert r["dedup_ok"] is False and r["dedup_violations"] == ["领航员"]


def test_score_case_paraphrase_with_new_info_is_not_violation():
    """带新信息的复述是合法新事实（不算重复）——按引擎包含语义判（memory.py:181）。

    反例：逐字复述既有事实 → 判违规。
    """
    mod = _load()
    case = dict(CASE, existing=["玩家的佩剑名叫听雨"], must_not_output=["玩家的佩剑名叫听雨"])
    ok = mod.score_case(case, [("听雨剑已伴随玩家三年", 4.0)])
    assert ok["dedup_ok"] is True and ok["dedup_violations"] == []
    bad = mod.score_case(case, [("玩家的佩剑名叫听雨", 5.0)])
    assert bad["dedup_ok"] is False


def test_score_case_importance_out_of_range():
    """重要性是优先级口径（诊断项），不影响通过判定（2026-09-12 首跑后定）。"""
    mod = _load()
    facts = [("玩家抵了 1200 信用点债务", 2.0)]  # 期望 5~9
    r = mod.score_case(CASE, facts)
    assert r["importance_ok"] is False
    assert r["importance_out_of_range"] is True
    assert r["passed"] is True  # 召回与去重都合规 → 通过


def test_score_case_suspected_fabrication():
    mod = _load()
    facts = [("玩家抵了 1200 信用点债务", 7.0), ("玩家与洛桑约定教他读星图", 6.0)]
    r = mod.score_case(CASE, facts)
    assert len(r["suspected_fabrication"]) == 1  # 第二条与输入几乎无重合


def test_score_case_empty_expected():
    mod = _load()
    case = dict(CASE, id="t_none", expect_empty=True, must_recall=[],
                must_not_output=[], turn="（闲谈）今日天气不错。")
    assert mod.score_case(case, [])["passed"] is True
    bad = mod.score_case(case, [("玩家看过天气", 3.0)])
    assert bad["passed"] is False and bad["empty_violation"] is True


def test_summarize_rates():
    mod = _load()
    results = [
        {"recalled": True, "dedup_ok": True, "importance_ok": True, "passed": True,
         "expect_empty": False, "missing": [], "dedup_violations": [],
         "suspected_fabrication": []},
        {"recalled": False, "dedup_ok": True, "importance_ok": True, "passed": False,
         "expect_empty": False, "missing": ["x"], "dedup_violations": [],
         "suspected_fabrication": []},
        {"recalled": True, "dedup_ok": True, "importance_ok": True, "passed": True,
         "expect_empty": True, "missing": [], "dedup_violations": [],
         "suspected_fabrication": []},
    ]
    s = mod.summarize(results)
    assert s["n"] == 3 and s["passed"] == 2
    assert s["pass_rate"] == round(2 / 3, 3)
    assert s["recall_hits"] == 1 and s["recall_total"] == 2  # 负例不进召回分母

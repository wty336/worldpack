"""A5（runtime 平台化 ①）守卫测试：冲突判定门禁（离线）。"""

from __future__ import annotations

from scripts.conflict_gate import LABELS, load_cases, summarize


def test_corpus_loads_with_balanced_classes():
    cases = load_cases()
    assert len(cases) == 24
    assert len({c["id"] for c in cases}) == len(cases)
    for label in LABELS:
        assert sum(1 for c in cases if c["expect"] == label) == 8, label
    assert all(c["existing"] and c["new"].strip() for c in cases)


def test_summarize_per_class_accuracy():
    results = [
        {"id": "a", "expect": "取代", "majority": "取代"},
        {"id": "b", "expect": "取代", "majority": "并存"},
        {"id": "c", "expect": "并存", "majority": "并存"},
        {"id": "d", "expect": "无冲突", "majority": "无冲突"},
    ]
    s = summarize(results)
    assert s["取代"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert s["并存"]["accuracy"] == 1.0 and s["无冲突"]["accuracy"] == 1.0


def test_summarize_empty_class_is_none_accuracy():
    s = summarize([{"id": "x", "expect": "取代", "majority": "取代"}])
    assert s["并存"]["n"] == 0 and s["并存"]["accuracy"] is None

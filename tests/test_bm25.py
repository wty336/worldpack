"""agent-first 第 3 件守卫测试：BM25 词面检索（替换 v1 字符 bigram 重叠，离线）。

契约（`game_agent/memory.py` A1 v2）：
- 词项 = 单字 + 二元组：单字扛召回（v1 的"剑叫 vs 剑名"零重叠在此命中）、二元组扛区分；
- 桶内局部 IDF：稀有词权重高、常用词被压制；
- 长度归一化 + 分数桶内归一化（与 γ 同阶）；
- ``rank_facts`` 的 ``relevance_fn`` 是可插拔接缝（embedding 同签名接入）；
- 去重预筛（_bigrams）**不属于**本改动，语义不变。
"""

from __future__ import annotations

from game_agent.memory import MemoryEntry, bm25_scores, rank_facts, _terms


def _entries(facts: list[str]) -> list[MemoryEntry]:
    return [
        MemoryEntry(fact=f, day=1, round=i, importance=5.0) for i, f in enumerate(facts)
    ]


# ---------------------------------------------------------------------------
# 词项与打分
# ---------------------------------------------------------------------------


def test_terms_include_unigrams_and_bigrams():
    assert set(_terms("听雨")) == {"听", "雨", "听雨"}


def test_rare_term_beats_common_term():
    """IDF 压制常用词：只命中「玩家」的文档得分 >0，但远低于额外命中稀有词「剑」的文档。"""
    docs = ["玩家甲", "玩家的剑名是听雨"]
    s = bm25_scores("玩家的剑", docs)
    assert s[1] > s[0] > 0


def test_no_shared_term_scores_zero():
    assert bm25_scores("听雨", ["玩家每天都在长安闲逛"]) == [0.0]


def test_paraphrase_recall_v1_could_not():
    """v1 二元组口径「我的剑叫什么」vs「剑名是听雨」零重叠；v2 单字词项补上召回。"""
    docs = ["玩家的剑名是听雨", "她爱吃桂花糕", "长安的雨下了三天"]
    s = bm25_scores("我的剑叫什么", docs)
    assert s[0] == max(s) and s[0] > 0  # 剑名事实胜过无关事实


def test_no_match_scores_zero_and_shape_holds():
    assert bm25_scores("完全无关的查询", ["甲乙丙", "丁戊己"]) == [0.0, 0.0]
    assert bm25_scores("", ["a"]) == [0.0]  # 空查询不炸
    assert bm25_scores("q", []) == []  # 空桶不炸


# ---------------------------------------------------------------------------
# rank_facts 集成：归一化 + 可插拔接缝
# ---------------------------------------------------------------------------


def test_rank_facts_relevance_normalized_keeps_blend_semantics():
    """分数桶内归一化：无相关命中的桶里 relevance 恒 0 → 按 recency/importance 排，不炸。"""
    entries = _entries(["事实一", "事实二", "事实三"])
    out = rank_facts(entries, "毫无交集的上下文", now_round=10, k=2, pinned=0)
    assert len(out) == 2  # 选得出来，无 NaN 干扰排序


def test_rank_facts_relevance_fn_seam():
    """可插拔接缝：自定义 ranker 同签名接入即生效（embedding 换装点）。"""

    def fake_relevance(query: str, docs: list[str]) -> list[float]:
        return [1.0 if "目标" in d else 0.0 for d in docs]

    entries = _entries(["普通甲", "目标乙", "普通丙"])
    out = rank_facts(
        entries, "任意查询", now_round=10, k=1, pinned=0, relevance_fn=fake_relevance
    )
    assert out[0].fact == "目标乙"


def test_pinned_zone_untouched_by_rank_change():
    """常驻区纪律不变：最高重要性恒注入，且排在前。"""
    entries = _entries([f"事实{i}" for i in range(20)])
    entries += [
        MemoryEntry(fact="核心身世", day=1, round=0, importance=10.0),
        MemoryEntry(fact="生死承诺", day=1, round=1, importance=9.0),
    ]
    out = rank_facts(entries, "", now_round=30, k=10, pinned=3)
    assert out[0].fact == "核心身世"
    assert any(m.fact == "生死承诺" for m in out)
    assert len(out) <= 13

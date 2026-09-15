"""① confab 材料重渲染的守卫：只改该改的、别的一个字不动。

这次操作**动的是已发布的数据**，所以守卫的重点不是"功能对不对"，而是
**"有没有越界"**：非 confab 样本不许碰、叙事/标签/质检判定不许变、
预览不许写盘、被断言的事实不许泄进材料。
"""
from __future__ import annotations

import json

from scripts.scenario_factory import worklog
from scripts.scenario_factory.cards import generate_card
from scripts.scenario_factory.fix_confab_material import fix_layer, patched_material


def _judge_sample(i: int, *, confab: bool) -> dict:
    """取一张真卡做样本（卡面确定性生成 → 用真卡才能测出真材料）。"""
    card = generate_card(11000 + i, i, "judge")
    want = "confab" if confab else card.corruptions[0].category
    return card, want


def _seed_journal(tmp_path, picks):
    j = worklog.Journal.open(tmp_path, "train", "judge", {"journal": 1})
    for i, card, cat in picks:
        j.append({"i": i, "status": worklog.KEPT, "card_id": card.card_id,
                  "sample": {"id": card.card_id, "module": "judge", "category": cat,
                             "genre": card.axes.genre, "material": "旧材料（无事实段）",
                             "narration": f"叙事 {i}"}})
        j.append({"i": i, "status": worklog.KEPT, "card_id": card.card_id, "quality": 2,
                  "sampled": True,
                  "sample": {"id": card.card_id, "module": "judge", "category": cat,
                             "genre": card.axes.genre, "material": "旧材料（无事实段）",
                             "narration": f"叙事 {i}"}})
    return j


def _find_confab(n=60):
    for i in range(n):
        card = generate_card(11000 + i, i, "judge")
        if card.corruptions and card.corruptions[0].category == "confab" and len(card.facts) > 1:
            return i, card
    raise AssertionError("找不到多事实的 confab 卡")


def test_only_confab_with_missing_facts_section_is_touched(tmp_path):
    ci, ccard = _find_confab()
    si = next(i for i in range(60)
              if generate_card(11000 + i, i, "judge").corruptions
              and generate_card(11000 + i, i, "judge").corruptions[0].category == "setting")
    scard = generate_card(11000 + si, si, "judge")
    j = _seed_journal(tmp_path, [(ci, ccard, "confab"), (si, scard, "setting")])
    rep = fix_layer(tmp_path, "train", apply=True)
    assert rep["changed"] == 1, "只该改那一条 confab"
    j2 = worklog.read_journal(j.path)
    conf = j2.entries[ci]["sample"]
    sett = j2.entries[si]["sample"]
    assert "关键事实" in conf["material"], "confab 材料没补上事实段"
    assert sett["material"] == "旧材料（无事实段）", "非 confab 样本被改了"
    # 叙事/标签/质检判定一字不动
    assert conf["narration"] == f"叙事 {ci}" and conf["category"] == "confab"
    assert j2.entries[ci]["quality"] == 2 and j2.entries[ci]["sampled"] is True


def test_dry_run_touches_nothing(tmp_path):
    ci, ccard = _find_confab()
    j = _seed_journal(tmp_path, [(ci, ccard, "confab")])
    before = j.path.read_text(encoding="utf-8")
    rep = fix_layer(tmp_path, "train", apply=False)
    assert rep["changed"] == 1
    assert j.path.read_text(encoding="utf-8") == before, "预览竟然写了盘"


def test_asserted_fact_never_enters_the_material(tmp_path):
    ci, ccard = _find_confab()
    _seed_journal(tmp_path, [(ci, ccard, "confab")])
    fix_layer(tmp_path, "train", apply=True)
    j = worklog.read_journal(worklog.journal_path(tmp_path, "train", "judge"))
    new = j.entries[ci]["sample"]["material"]
    tgt = ccard.corruptions[0].target_fact
    for a in ccard.facts[tgt].anchors:
        assert a not in new, f"被断言的事实的 anchor 泄进材料: {a}"
    for k in range(len(ccard.facts)):
        if k != tgt:
            for a in ccard.facts[k].anchors:
                assert a in new, f"非目标事实的 anchor 没补进材料: {a}"


def test_already_fixed_sample_is_left_alone(tmp_path):
    """幂等：已经补过的样本不该被反复改（材料里已有事实段 → 不动）。"""
    ci, ccard = _find_confab()
    j = _seed_journal(tmp_path, [(ci, ccard, "confab")])
    fix_layer(tmp_path, "train", apply=True)
    rep2 = fix_layer(tmp_path, "train", apply=True)
    assert rep2["changed"] == 0 and rep2["stats"].get("已有事实段") == 1


def test_patched_material_reports_why_it_skips():
    si = next(i for i in range(60)
              if generate_card(11000 + i, i, "judge").corruptions
              and generate_card(11000 + i, i, "judge").corruptions[0].category == "setting")
    card = generate_card(11000 + si, si, "judge")
    txt, why = patched_material({"id": card.card_id, "category": "setting", "material": "x"})
    assert txt is None and why == "非 confab"
    txt, why = patched_material({"id": card.card_id, "category": "confab",
                                 "material": "…关键事实：…"})
    assert txt is None and why == "已有事实段"

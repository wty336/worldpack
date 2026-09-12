"""场景卡工厂守卫测试（离线；StubLLM 见 Task 4）。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.scenario_factory.cards import (
    GENRE_EVAL_ONLY,
    PACK_BY_GENRE,
    ScenarioCard,
    generate_card,
    layer_of,
)


def _judge_card(**over):
    base = {
        "card_id": "sc-10231-0001", "seed": 10231, "module": "judge",
        "pack": "world-packs/xianxia_wendao",
        "axes": {"genre": "仙侠", "style": "口语叙事", "entity_forms": ["人名"],
                 "stat_system": "灵石", "relation": "师门", "scale": {"nodes": 6, "npcs": 2}},
        "facts": [
            {"type": "物品与装备", "importance": 8, "text": "玩家的剑名为「听雨」",
             "anchors": ["听雨"], "in_material": True},
        ],
        "corruptions": [
            {"category": "setting", "target_fact": 0, "method": "近义改写",
             "detail": "剑名「听雨」演绎为「听风」", "expect": "问题类型：设定矛盾"},
        ],
        "material": {"day": 3, "scene": "山门", "present": ["bai_zhi"],
                     "affections": {"bai_zhi": 45}},
        "history_spec": {"turns": 1, "target_tokens": 600, "noise": "寒暄"},
    }
    base.update(over)
    return base


def test_valid_judge_card_passes():
    ScenarioCard(**_judge_card())


def test_card_id_must_match_seed():
    with pytest.raises(ValidationError, match="card_id"):
        ScenarioCard(**_judge_card(card_id="sc-99999-0001"))


def test_target_fact_out_of_range():
    bad = _judge_card()
    bad["corruptions"][0]["target_fact"] = 5
    with pytest.raises(ValidationError, match="target_fact"):
        ScenarioCard(**bad)


def test_setting_requires_in_material_true():
    bad = _judge_card()
    bad["facts"][0]["in_material"] = False
    with pytest.raises(ValidationError, match="setting"):
        ScenarioCard(**bad)


def test_confab_requires_false_and_all_false_and_present():
    good = _judge_card()
    good["facts"][0]["in_material"] = False
    good["corruptions"][0].update(category="confab", expect="问题类型：虚构事实")
    ScenarioCard(**good)  # 全 false + present 非空 → 通过
    bad = _judge_card(facts=[
        {"type": "物品与装备", "importance": 8, "text": "t", "anchors": ["a"], "in_material": False},
        {"type": "目标与线索", "importance": 5, "text": "b", "anchors": ["b"], "in_material": True},
    ])
    bad["corruptions"][0].update(category="confab", expect="问题类型：虚构事实")
    with pytest.raises(ValidationError, match="全部"):
        ScenarioCard(**bad)


def test_expect_must_align_with_judge_categories():
    bad = _judge_card()
    bad["corruptions"][0]["expect"] = "问题类型：时间线错误"
    with pytest.raises(ValidationError, match="设定矛盾"):
        ScenarioCard(**bad)


def test_judge_requires_pack_material_and_single_corruption():
    with pytest.raises(ValidationError, match="pack"):
        ScenarioCard(**_judge_card(pack=None))
    with pytest.raises(ValidationError, match="恰好 1 条"):
        ScenarioCard(**_judge_card(corruptions=[]))


def test_extract_card_accepts_existing_for_dedup_discipline():
    """契约：existing 是生产模板的一半（`已有事实：{existing}`），必须可入卡。"""
    card = ScenarioCard(**_judge_card(module="extract", pack=None, material=None,
                                      existing=["玩家的佩剑名叫听雨"], facts=[],
                                      corruptions=[]))
    assert card.existing == ["玩家的佩剑名叫听雨"] and card.facts == []


def test_ooc_target_fact_may_be_null():
    card = _judge_card()
    card["corruptions"][0].update(category="ooc", target_fact=None,
                                  expect="问题类型：OOC")
    ScenarioCard(**card)


# --- Task 2：轴空间与确定性生成器 -----------------------------------------


def test_layer_of():
    assert layer_of(10231) == "train"
    assert layer_of(20231) == "dev"
    assert layer_of(30231) == "eval"
    with pytest.raises(ValueError):
        layer_of(999)


def test_generate_is_deterministic_and_seed_spaced():
    a = generate_card(10231, 6, "extract")   # seq%10==6 → 正例卡（⑦⑧ 是负例位，见生成器）
    b = generate_card(10231, 6, "extract")
    assert a == b  # 同 seed+seq+module → 同一张卡（可复现）
    assert a.card_id == "sc-10231-0006"
    assert a.module == "extract" and len(a.facts) >= 3 and a.events


def test_eval_only_axis_never_leaks_to_train_dev():
    for seq in range(60):
        assert generate_card(10000 + seq, seq, "extract").axes.genre != "民国谍战"
        dev_card = generate_card(20000 + seq, seq, "extract")
        assert dev_card.axes.genre != "民国谍战"  # dev 只许孪生「抗战谍战」
        assert dev_card.axes.style != "书信体"
    genres = {generate_card(30000 + s, s, "extract").axes.genre for s in range(60)}
    assert "民国谍战" in genres  # eval 空间抽得到留出轴


def test_judge_card_genre_must_have_pack():
    for seq in range(40):
        c = generate_card(10231, seq, "judge")
        assert c.axes.genre in PACK_BY_GENRE  # 无包映射的题材不出 judge 卡


def test_extract_dedup_discipline_cards_exist_and_carry_existing():
    """spec §4.2.1 硬要求：负例必配，其中「已有事实的近义改写」型须带 existing。"""
    cards = [generate_card(10231, s, "extract") for s in range(80)]
    dedup = [c for c in cards if c.existing]
    assert dedup, "生成器必须产出「已有事实」型卡片（考去重纪律）"
    for c in dedup:
        assert c.facts == []          # 近义改写型 → 标签「无」（无新事实）
        assert c.events               # 仍需情节骨架供演绎器写复述型回合
    plain_neg = [c for c in cards if not c.existing and not c.facts]
    assert plain_neg, "生成器必须产出「纯寒暄」型负例（无 existing、无 facts）"
    assert len([c for c in cards if not c.facts]) / len(cards) >= 0.15  # 负例 ≥15%


def test_fact_anchors_are_pairwise_disjoint():
    """反向校验（Task 4）的前提：同一张卡上不同事实的 anchors 不得相交——
    否则 setting 卡的「原 anchor 不得出现」永远不成立，该类卡永久产出不了样本。"""
    for seq in range(40):
        for mod in ("extract", "judge", "compress"):
            anchors = [a for f in generate_card(10231, seq, mod).facts for a in f.anchors]
            assert len(anchors) == len(set(anchors)), f"{mod}#{seq} anchors 相交: {anchors}"


def test_corruption_detail_quotes_are_parseable():
    """Task 4 的 `_corruption_swap` 用 `「([^」]+)」` 解析 detail：
    嵌套「」（如把整条 text 引进去）会解出残缺串 → 该卡**恒被丢弃**，
    而 confab 占 judge 配额 ≥40% —— 是静默良率损失，必须钉住。"""
    for seq in range(40):
        c = generate_card(10231, seq, "judge")
        cor = c.corruptions[0]
        assert cor.detail.count("「") == cor.detail.count("」"), cor.detail
        quoted = re.findall(r"「([^」]+)」", cor.detail)
        # 不得嵌套引用：嵌套会让 Task 4 的正则解出残缺串 → 该卡恒被丢弃
        assert all("「" not in q and "」" not in q for q in quoted), cor.detail
        if cor.category == "setting":
            assert len(quoted) == 2, f"setting 需「原值」「新值」两引（反向校验依赖）: {cor.detail}"
        elif cor.category == "confab":
            assert len(quoted) == 1, f"confab 只应引被断言的 anchor 一个「」: {cor.detail}"
        else:  # ooc：改写语气/底线，不涉及具体值 → 无引用
            assert quoted == [], cor.detail


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_judge_genre_never_leaks_holdout_and_pack_must_exist():
    """judge 分支的题材重映射有两条硬约束（原版只按 PACK_BY_GENRE 的键随机取 → 两条都破）：

    1. **留出轴只对 eval 开放**（决策 19）：`PACK_BY_GENRE` 含留出轴「民国谍战」，
       若把它放进 train/dev 的重映射候选，约 14% 的 train judge 卡会变成留出轴；
    2. **包必须存在**：judge 卡材料要由真实包物化，映射到 G1（待造）只会让该卡在
       材料装配时炸 —— 出了卡也是废卡。
    """
    for layer, base in (("train", 10000), ("dev", 20000), ("eval", 30000)):
        for seq in range(60):
            card = generate_card(base + seq, seq, "judge")
            genre = card.axes.genre
            if layer != "eval":
                assert genre != GENRE_EVAL_ONLY, f"{layer} 的 judge 卡漏了留出轴: {card.card_id}"
            assert (REPO_ROOT / PACK_BY_GENRE[genre]).is_dir(), (
                f"{card.card_id} 映射到不存在的包: {PACK_BY_GENRE[genre]}")


def test_judge_recent_carries_no_fact_anchor():
    """`material.recent` 不得带本卡任何事实的 anchor。

    recent 虽不渲染进材料（只作 rank_facts / select_lore 的打分输入），但带上本卡专名会
    污染检索命中，语义上也与"材料代表最近玩家发言"不符；对 confab 卡更需保持
    "材料对该承诺零信号"。原版 `recent=f"玩家向{n2}问起{n3}"` 而 n2/n3 正是 facts[0] 的槽。
    """
    for seq in range(60):
        card = generate_card(10231, seq, "judge")
        anchors = [a for f in card.facts for a in f.anchors]
        assert not any(a in card.material.recent for a in anchors), (
            f"{card.card_id} 的 recent 撞了 anchors: {card.material.recent}")


def test_corruption_new_value_does_not_collide_with_card_entities():
    """corruption 的「新值」不得落在任何事实的文本/anchors 里。

    否则材料里同时出现"原值"与"新值"，"材料说 A、叙述说 B"的陷阱纯度被稀释。
    原版 20% 的 setting 卡中招：facts[0] 恰为「承诺与约定」模板时 n0/n1 都被用上，
    而重映射的 n3 = pool[1] 正是 facts[0] 的第二个槽。
    """
    seen_setting = 0
    for seq in range(60):
        card = generate_card(10231, seq, "judge")
        cor = card.corruptions[0]
        if cor.category != "setting":
            continue
        seen_setting += 1
        quoted = re.findall(r"「([^」]+)」", cor.detail)   # [原值, 新值]
        assert quoted[0] != quoted[1], f"{card.card_id}: 原值 = 新值: {cor.detail}"
        new_val = quoted[-1]
        for f in card.facts:
            assert new_val not in f.text, f"{card.card_id}: 新值 {new_val} 已在事实文本里"
            assert all(new_val not in a for a in f.anchors), card.card_id
    assert seen_setting, "样本里没有 setting 卡，守卫没生效"

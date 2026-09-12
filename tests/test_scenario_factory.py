"""场景卡工厂守卫测试（离线；StubLLM 见 Task 4）。"""
from __future__ import annotations

import random
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from game_agent.worldpack import load_worldpack
from scripts.scenario_factory.cards import (
    GENRE_EVAL_ONLY,
    NPC_BY_PACK,
    PACK_BY_GENRE,
    ScenarioCard,
    generate_card,
    layer_of,
)
from scripts.scenario_factory.materialize import (
    MaterializeError,
    _precheck,
    build_material,
)
from scripts.scenario_factory.verbalize import verbalize_card
from scripts.rubric_judge import (
    make_probe,
    pairwise,
    probe_detected,
    quality_sample,
    score,
    select,
)
from scripts.scenario_factory.cards import PreservePoint


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


# --- Task 3：材料装配器（校验①②③） ---------------------------------------


def _setting_card(**over):
    """setting 卡：facts 全 in_material=true —— 专用来测校验②（在位）。"""
    base = _judge_card()          # 夹具本身即 setting 卡（听雨 → 听风）
    base.update(over)
    return ScenarioCard(**base)


def _confab_card(**over):
    """confab 卡：facts **全** in_material=false（Task 1 硬规则）—— 专用来测校验③（泄漏）。"""
    base = _judge_card()
    base["facts"][0]["in_material"] = False
    base["corruptions"][0].update(category="confab", expect="问题类型：虚构事实")
    base.update(over)
    return ScenarioCard(**base)


def test_setting_material_materializes_facts():
    """校验②：in_material=true 的事实须物化进「关键事实」区（全部 anchors 在位）。"""
    text = build_material(_setting_card())
    assert "听雨" in text and "白芷" in text      # 事实 + 在场角色卡


def test_confab_material_excludes_the_claim():
    """校验③：confab 的断言事实不得进材料（该卡 facts 全 false → 关键事实区为空）。"""
    assert "听雨" not in build_material(_confab_card())


def test_material_leak_raises():
    card = _confab_card()
    card.material.memories = {"bai_zhi": ["玩家提到佩剑听雨"]}  # 人为泄漏
    with pytest.raises(MaterializeError, match="泄漏"):
        build_material(card)


def test_missing_in_material_anchor_raises():
    card = _setting_card()
    card.facts[0].anchors = ["不存在的专名xyz"]
    with pytest.raises(MaterializeError, match="未在材料中"):
        build_material(card)


def test_present_npc_must_be_in_pack():
    card = _setting_card()
    card.material.present = ["ghost_npc"]
    with pytest.raises(MaterializeError, match="不在包里"):
        build_material(card)


def test_present_npc_without_affection_track_raises():
    """present NPC 必须在包的 `schedule.affections` 里（评审指出、已实测）。

    否则 `status_text` 会：① 好感行**整体略过**该 NPC；② 在场角色卡的**语气档**按
    `state.affections.get(npc.id, 0.0)` 兜底 —— 材料"卡里声明过 45、却按 0 档渲染语气"，
    与卡片意图矛盾 = **坏标签**（T2 语气冲突卡的整条通路就是好感档）。

    用假包做**单元级前置校验**：真实三包目前都满足 `npcs ⊆ affections`，构造不出来。
    """
    pack = SimpleNamespace(npcs={"ghost": SimpleNamespace(name="鬼")},
                           schedule=SimpleNamespace(affections={"real": None}))
    card = _setting_card()
    card.material.present = ["ghost"]
    with pytest.raises(MaterializeError, match="好感轨"):
        _precheck(card, pack)


def test_material_affection_override_must_be_declared():
    """卡声明的好感覆写必须能落地：旧实现 `if k in state.affections:` 会**静默跳过**。"""
    card = _setting_card()
    card.material.affections = {"nobody_in_schedule": 45.0}
    with pytest.raises(MaterializeError, match="没有好感轨"):
        build_material(card)


def test_judge_npc_mapping_targets_have_affection_tracks():
    """`NPC_BY_PACK` 的映射目标必须在对应包的好感轨与角色卡里 ——
    否则整批该题材的 judge 卡都会被 `_precheck` 丢弃（静默良率归零）。"""
    for nick, npc in NPC_BY_PACK.items():
        pack_dir = REPO_ROOT / "world-packs" / nick
        if not pack_dir.is_dir():
            continue                     # G1 待造，跳过
        pack = load_worldpack(pack_dir)
        assert npc in pack.npcs, f"{nick} 的映射目标 {npc} 没有角色卡"
        assert npc in pack.schedule.affections, f"{nick} 的映射目标 {npc} 没有好感轨"


# --- Task 4：演绎器（anchors 在位 + 反向校验 + 重演丢弃） --------------------


class StubLLM:
    """budgets._complete 的轻量替身协议：complete_with_meta(messages, **kw)。

    **队列语义：末条粘滞**（取到最后一条后重复使用）。原因是有两处会多吃一条队列，
    夹具若给"刚好够"的条数就会 `IndexError: pop from empty list`：
      ① `complete_checked` 在空响应/截断（`finish_reason="length"`）时会用
         **升级预算重试一次**（`budgets.py:70-81`）；
      ② `verbalize_card` 自身有 `MAX_ATTEMPTS` 循环。
    调用次数仍由 `self.calls` 精确可数，故断言不受影响。
    """

    def __init__(self, texts, finish="stop"):
        self.texts = list(texts)
        self.finish = finish
        self.calls = []

    def complete_with_meta(self, messages, **kw):
        self.calls.append(kw)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return SimpleNamespace(text=text, finish_reason=self.finish)


def _extract_card():
    return generate_card(10231, 6, "extract")   # 正例位（必有 anchors 可校验）


def test_verbalize_ok_first_try():
    card = _extract_card()
    anchors = [a for f in card.facts for a in f.anchors]
    llm = StubLLM(["渡口茶棚里，" + "，".join(anchors) + "，闲谈收尾。"])
    r = verbalize_card(llm, card)
    assert not r.dropped and r.attempts == 1 and llm.calls[0]["temperature"] == 0.9


def test_verbalize_retry_then_drop_when_anchor_missing():
    card = _extract_card()
    llm = StubLLM(["没有专名的文本", "还是没有"])
    r = verbalize_card(llm, card)
    assert r.dropped and r.attempts == 2


def test_verbalize_truncated_discards():
    llm = StubLLM(["半截文本"], finish="length")
    assert verbalize_card(llm, _extract_card()).dropped


def _setting_card_from_generator():
    """取一张 setting 卡：反向校验（原词不得出现）只对它有意义。"""
    return next(c for s in range(20)
                if (c := generate_card(10231, s, "judge")).corruptions[0].category == "setting")


def test_verbalize_corruption_reverse_check():
    card = _setting_card_from_generator()
    hit, rest = card.facts[0], card.facts[1:]
    quoted = re.findall(r"「([^」]+)」", card.corruptions[0].detail)  # [原值, 新值]
    rest_anchors = [a for f in rest for a in f.anchors]

    # 好样本：其余事实 anchors 在位 + corruption 新值在位 + 被命中事实的**原 anchor 不在**
    good = "叙事：" + "、".join(rest_anchors + [quoted[-1]])
    assert not verbalize_card(StubLLM([good]), card).dropped

    # 坏样本：把原 anchor 写回去 → 必须丢弃（这就是反向校验）
    bad = "叙事：" + "、".join(rest_anchors + [quoted[-1], hit.anchors[0]])
    assert verbalize_card(StubLLM([bad]), card).dropped


def test_verbalize_confab_requires_the_fabricated_anchor():
    """confab：叙事**必须**把编造的 anchor 说出来 —— 与 setting 的"原词不得出现"方向相反。

    早先一版把 confab 也按 setting 处理（同一条 anchor 既要求在位、又列为不得出现），
    结果 confab 卡恒被丢弃；而 confab 占 judge 配额 ≥40%，是重大静默损失。
    """
    card = next(c for s in range(20)
                if (c := generate_card(10231, s, "judge")).corruptions[0].category == "confab")
    cor = card.corruptions[0]
    assert cor.target_fact is not None
    wanted = re.findall(r"「([^」]+)」", cor.detail)      # 被断言的 anchor
    rest = [a for i, f in enumerate(card.facts) if i != cor.target_fact for a in f.anchors]

    good = "叙事：" + "、".join(rest + wanted)            # 说出了编造 → 收下
    assert not verbalize_card(StubLLM([good]), card).dropped

    missing_claim = "叙事：" + "、".join(rest)            # 没说出编造 → 丢弃
    assert verbalize_card(StubLLM([missing_claim]), card).dropped


# --- Task 5：rubric 评委四模式 + 质检员 -------------------------------------


class MsgLLM(StubLLM):
    """StubLLM + 记录 messages（验证 pairwise 的位置交换）。"""

    def __init__(self, texts, finish="stop"):
        super().__init__(texts, finish)
        self.msgs = []

    def complete_with_meta(self, messages, **kw):
        self.msgs.append(list(messages))
        return super().complete_with_meta(messages, **kw)


_PP = [PreservePoint(text="玩家的剑名为「听雨」", anchors=["听雨"])]


def test_score_parses_json_and_validates_range():
    llm = StubLLM(['{"保真": 2, "简洁": 1, "结构": 2, "流畅": 2}'])
    s = score(llm, summary="摘要", material="材料", preserve_points=_PP)
    assert s == {"保真": 2, "简洁": 1, "结构": 2, "流畅": 2}
    assert llm.calls[0]["temperature"] == 0.0  # 评委一律 temp=0（spec §7.3）


def test_score_extracts_json_from_noisy_output():
    llm = StubLLM(['好的，评分如下：{"保真": 1, "简洁": 2, "结构": 1, "流畅": 2} 完毕'])
    assert score(llm, summary="s", material="m", preserve_points=_PP)["保真"] == 1
    bad = StubLLM(["无法评分"])
    with pytest.raises(ValueError, match="JSON"):
        score(llm=bad, summary="s", material="m", preserve_points=_PP)
    out = StubLLM(['{"保真": 3, "简洁": 0, "结构": 0, "流畅": 0}'])
    with pytest.raises(ValueError, match="越界"):
        score(llm=out, summary="s", material="m", preserve_points=_PP)


def test_pairwise_swaps_positions_and_majority_rules():
    # 评委永远偏好"先看到的那份"：A位/B位各半 → 映射回原始后 a 全胜
    llm = MsgLLM(['{"winner": "A"}', '{"winner": "B"}'] * 3)
    w = pairwise(llm, a="摘要甲", b="摘要乙", material="材料", preserve_points=_PP)
    assert w == "A" and len(llm.msgs) == 6  # 位置交换 ×3 重复 = 6 次（spec §10.2 口径）
    users = [m[-1]["content"] for m in llm.msgs]
    assert users[0].index("摘要甲") < users[0].index("摘要乙")  # 第 1 次 a 在前
    assert users[1].index("摘要乙") < users[1].index("摘要甲")  # 第 2 次已交换
    tie = MsgLLM(['{"winner": "A"}'] * 6)  # 恒定判 A 位 → 3:3 → tie
    assert pairwise(tie, a="甲", b="乙", material="m", preserve_points=_PP) == "tie"


def test_select_picks_highest_total():
    llm = StubLLM([
        '{"保真": 1, "简洁": 1, "结构": 1, "流畅": 1}',  # 总 4
        '{"保真": 2, "简洁": 2, "结构": 2, "流畅": 1}',  # 总 7 ←
        '{"保真": 1, "简洁": 2, "结构": 1, "流畅": 1}',  # 总 5
    ])
    assert select(llm, candidates=["c0", "c1", "c2"], material="m",
                  preserve_points=_PP) == 1


def test_make_probe_kinds_and_detection():
    clean = "玩家的剑名为「听雨」。他答应中秋前还银。渡口无事。"
    rng = random.Random(1)
    p1 = make_probe(clean, _PP, kind="删要点", rng=rng)
    assert "听雨" not in p1
    p2 = make_probe(clean, _PP, kind="注入虚构", rng=rng)
    assert "听雨" in p2 and len(p2) > len(clean)
    p3 = make_probe(clean, _PP, kind="打乱结构", rng=rng)
    assert sorted(p3) == sorted(clean) and p3 != clean
    assert probe_detected({"保真": 0, "简洁": 1, "结构": 1, "流畅": 1}, "删要点")
    assert not probe_detected({"保真": 1, "简洁": 1, "结构": 0, "流畅": 1}, "删要点")


def test_quality_sample_flags_template_like_output():
    """spec §7.4 质检员：自然度 <1 的样本须被剔除（抽检是常驻关卡，不是一次性检查）。"""
    llm = StubLLM(['{"自然度": 0}', '{"自然度": 2}'])
    rows = [{"id": "a", "input": "填空式模板文本"}, {"id": "b", "input": "自然叙事"}]
    assert quality_sample(llm, rows, rate=1.0, seed=1) == ["a"]

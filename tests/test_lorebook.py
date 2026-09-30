"""B1 Lorebook 测试（离线，P3）：按需注入、预算、静态前缀隔离、schema 校验。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from game_agent.compression import est_tokens
from game_agent.context import (
    LORE_BUDGET_CAP,
    LORE_BUDGET_FALLBACK,
    LORE_BUDGET_RATIO,
    ContextBuilder,
    LORE_BUDGET,
    resolve_lore_budget,
    select_lore,
)
from game_agent.state import GameState
from game_agent.worldpack import LoreSpec, WorldPackError, load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _lore(
    id_: str,
    keys: list[str],
    text: str | None = None,
    secondary_keys: list[str] | None = None,
    logic: str = "AND_ANY",
    case_sensitive: bool = True,
    constant: bool = False,
    ignore_budget: bool = False,
    exclude_from_recursion: bool = False,
    no_recursion_trigger: bool = False,
) -> LoreSpec:
    return LoreSpec(
        id=id_,
        keys=keys,
        text=text or f"{id_} 的设定文本",
        secondary_keys=secondary_keys or [],
        logic=logic,
        case_sensitive=case_sensitive,
        constant=constant,
        ignore_budget=ignore_budget,
        exclude_from_recursion=exclude_from_recursion,
        no_recursion_trigger=no_recursion_trigger,
    )


def _only(selected: list[LoreSpec]) -> str:
    """断言只命中一条并返回其 id（A-2 四值逻辑用：每次只放一条候选，二值结果清晰）。"""
    assert len(selected) == 1, [e.id for e in selected]
    return selected[0].id


# ---------------------------------------------------------------------------
# select_lore
# ---------------------------------------------------------------------------


def test_select_lore_matches_keys_and_orders_by_hits():
    lore = [
        _lore("a", ["东市"]),
        _lore("b", ["东市", "胡商"]),
        _lore("c", ["西市"]),
    ]
    selected = select_lore(lore, "你在东市闲逛，看见胡商卸货")
    assert [e.id for e in selected] == ["b", "a"]  # 命中数降序；c 未命中


def test_select_lore_budget_cap():
    lore = [_lore(f"e{i}", ["东市"], text="长" * 600) for i in range(30)]
    selected = select_lore(lore, "东市", budget=1500)
    assert len(selected) <= 2  # 每条约 600+ 字，1500 预算最多 2 条
    assert sum(len(e.text) for e in selected) <= 1500


def test_select_lore_empty_context_returns_empty():
    assert select_lore([_lore("a", ["东市"])], "") == []


# ---------------------------------------------------------------------------
# A-2：次级关键词与四值逻辑（SillyTavern selectiveLogic）
# ---------------------------------------------------------------------------


def test_secondary_and_any_passes_with_one_secondary_hit():
    entry = _lore("a", ["东市"], secondary_keys=["胡商", "驼队"], logic="AND_ANY")
    assert _only(select_lore([entry], "东市的胡商在卸货")) == "a"


def test_secondary_and_any_blocks_when_no_secondary_hits():
    entry = _lore("a", ["东市"], secondary_keys=["胡商", "驼队"], logic="AND_ANY")
    assert select_lore([entry], "你只在东市转了一圈") == []


def test_secondary_and_all_passes_only_when_every_secondary_hits():
    entry = _lore("a", ["东市"], secondary_keys=["胡商", "驼队"], logic="AND_ALL")
    assert _only(select_lore([entry], "东市的胡商牵着驼队")) == "a"
    assert select_lore([entry], "东市的胡商在卸货") == []  # 缺「驼队」


def test_secondary_not_any_passes_when_no_secondary_hits():
    entry = _lore("a", ["东市"], secondary_keys=["胡商", "驼队"], logic="NOT_ANY")
    assert _only(select_lore([entry], "你只在东市转了一圈")) == "a"
    assert select_lore([entry], "东市的胡商在卸货") == []  # 提到胡商即不注入


def test_secondary_not_all_passes_when_any_secondary_misses():
    entry = _lore("a", ["东市"], secondary_keys=["胡商", "驼队"], logic="NOT_ALL")
    assert _only(select_lore([entry], "东市的胡商在卸货")) == "a"  # 驼队未提
    assert select_lore([entry], "东市的胡商牵着驼队") == []  # 全提则不注入


def test_secondary_logic_changes_outcome_for_same_context():
    """同一 keys/次键，仅 logic 不同必须产生相反结果（防止四值被实现成同一个分支）。"""
    context = "东市的胡商牵着驼队"
    and_all = _lore("a", ["东市"], secondary_keys=["胡商", "驼队"], logic="AND_ALL")
    not_all = _lore("a", ["东市"], secondary_keys=["胡商", "驼队"], logic="NOT_ALL")
    assert _only(select_lore([and_all], context)) == "a"
    assert select_lore([not_all], context) == []


def test_secondary_empty_is_plain_primary_match():
    """次键为空时 logic 不参与判定——退化为纯主键命中。"""
    for logic in ("AND_ANY", "AND_ALL", "NOT_ANY", "NOT_ALL"):
        entry = _lore("a", ["东市"], secondary_keys=[], logic=logic)
        assert _only(select_lore([entry], "东市的胡商在卸货")) == "a"


def test_secondary_hit_count_becomes_score_for_ordering():
    """A-2：评分 = 主键命中数 + 次键命中数；评分高者先注入。"""
    few = _lore("few", ["东市"], secondary_keys=["胡商"], logic="AND_ANY")
    many = _lore("many", ["东市"], secondary_keys=["胡商", "驼队"], logic="AND_ANY")
    selected = select_lore([few, many], "东市的胡商牵着驼队卸货")
    assert [e.id for e in selected] == ["many", "few"]


# ---------------------------------------------------------------------------
# A-3：正则键与大小写敏感
# ---------------------------------------------------------------------------


def test_regex_key_matches_pattern_not_literal():
    """`/…/` 按正则匹配：正则命中的串本身不必是字面量。"""
    entry = _lore("a", ["/(沈府|曲江池|第\\d+天)/"])
    assert _only(select_lore([entry], "第3天你到了东市")) == "a"
    assert select_lore([entry], "你只是在东市闲逛") == []


def test_regex_key_supports_alternation():
    entry = _lore("a", ["/(沈府|曲江池)/"])
    assert _only(select_lore([entry], "你走向曲江池")) == "a"
    assert select_lore([entry], "你走向东市") == []


def test_case_sensitive_default_blocks_case_variant():
    entry = _lore("a", ["Eldoria"])
    assert select_lore([entry], "welcome to eldoria") == []


def test_case_insensitive_matches_case_variant():
    entry = _lore("a", ["Eldoria"], case_sensitive=False)
    assert _only(select_lore([entry], "welcome to eldoria")) == "a"


def test_case_sensitive_applies_to_secondary_keys():
    entry = _lore("a", ["Market"], secondary_keys=["Spice"], logic="AND_ANY")
    assert select_lore([entry], "market with spice") == []
    entry_ci = _lore("a", ["Market"], secondary_keys=["Spice"], logic="AND_ANY", case_sensitive=False)
    assert _only(select_lore([entry_ci], "market with spice")) == "a"


def test_invalid_regex_key_raises_at_load(tmp_path):
    """非法正则必须在加载期报错，而不是运行时静默不命中。"""
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [{"id": "bad", "keys": ["/(未闭合/"], "text": "x"}]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError, match="正则"):
        load_worldpack(pack_dir)


def test_invalid_regex_in_secondary_keys_raises(tmp_path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [
        {"id": "bad", "keys": ["甲"], "secondary_keys": ["/(未闭合/"], "logic": "AND_ANY", "text": "x"}
    ]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError, match="正则"):
        load_worldpack(pack_dir)


def test_slash_without_closing_slash_is_literal():
    """只有 `/…/` 成对才算正则；单个斜杠仍是字面量（避免误伤含斜杠的词）。"""
    entry = _lore("a", ["/路径"])
    assert _only(select_lore([entry], "走这条 /路径 过去")) == "a"


def test_token_budget_caps_by_token_not_char():
    """A-1：预算以 token（`est_tokens` 口径）计，长文条目按 token 数被挡在预算外。"""
    one = _lore("one", ["东市"], text="长" * 900)  # est_tokens=900，超过 800 预算
    assert select_lore([one], "东市", budget=800) == []
    assert _only(select_lore([one], "东市", budget=1000)) == "one"


def test_lore_budget_scales_with_context_window():
    """A-1：预算按窗口比例推导，且受上限截断（大窗口不无界增长）。"""
    assert resolve_lore_budget(0) == LORE_BUDGET_FALLBACK  # 未声明窗口 → 回退常量
    assert resolve_lore_budget(4000) == int(4000 * LORE_BUDGET_RATIO)
    assert resolve_lore_budget(1_000_000) == LORE_BUDGET_CAP  # 被上限截断


def test_window_limits_injected_lore_without_touching_prefix():
    """A-1 端到端：同一世界包在小窗口下注入更少 lore，但**静态前缀逐字节不变**。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.scene = "长安城·东市"
    recent = "（玩家）想去曲江池看看七夕诗会"
    big = ContextBuilder.from_pack(pack, window_tokens=200_000)
    small = ContextBuilder.from_pack(pack, window_tokens=1000)
    assert big.system_message["content"] == small.system_message["content"]  # 前缀与窗口无关
    big_lore = big.status_text(state, None, recent=recent)
    small_lore = small.status_text(state, None, recent=recent)
    assert big_lore.count("- 【") > small_lore.count("- 【")


def test_ignore_budget_entry_injected_even_when_over_budget():
    """A-1：`ignore_budget` 条目在预算耗尽后仍注入（引擎强制接管的白名单口子）。"""
    filler = _lore("filler", ["东市"], text="填" * 5000)
    forced = LoreSpec(id="forced", keys=["东市"], text="必须注入", ignore_budget=True)
    selected = select_lore([filler, forced], "东市", budget=10)
    assert [e.id for e in selected] == ["forced"]


def test_injected_lore_respects_token_budget():
    """注入总量不得超过按窗口推导的预算（分项账目真实生效）。"""
    pack = load_worldpack(PACK_PATH)
    builder = ContextBuilder.from_pack(pack, window_tokens=6000)
    state = GameState.from_pack(pack)
    state.scene = "长安城·东市"
    text = builder.status_text(state, None, recent="曲江池 诗会 沈府 后山 西市")
    budget = resolve_lore_budget(6000)
    used = sum(
        est_tokens(f"- 【{e.id}】{e.text}") + 1
        for e in pack.world.lore
        if f"- 【{e.id}】" in text
    )
    assert used <= budget


# ---------------------------------------------------------------------------
# A-4：常驻注入与递归激活
# ---------------------------------------------------------------------------


def test_constant_entry_injected_without_any_keyword():
    entry = _lore("c", ["灵石"], text="灵石是通用货币", constant=True)
    assert _only(select_lore([entry], "你在山道上走着")) == "c"


def test_constant_is_candidate_not_free_of_budget():
    """常驻 ≠ 无限：`constant` 只免"关键词判定"，仍受预算约束。"""
    entry = _lore("c", ["灵石"], text="长" * 900, constant=True)
    assert select_lore([entry], "你在山道上走着", budget=100) == []
    assert _only(select_lore([entry], "你在山道上走着", budget=1000)) == "c"


def test_ignore_budget_overrides_constant_budget_limit():
    """两个维度独立：`ignore_budget` 免预算，`constant` 免关键词，可各自单独生效。"""
    entry = _lore("c", ["灵石"], text="长" * 900, constant=True, ignore_budget=True)
    assert _only(select_lore([entry], "你在山道上走着", budget=10)) == "c"


def test_recursion_default_off():
    a = _lore("a", ["青云仙宗"], text="青云仙宗下辖剑峰")
    b = _lore("b", ["剑峰"], text="剑峰下是洗剑池")
    assert [e.id for e in select_lore([a, b], "你到访青云仙宗")] == ["a"]  # max_recursion 默认 0


def test_recursion_activates_secondary_knowledge():
    """A→B 二级知识：命中 A 的正文带出 B 的关键词，递归激活 B。"""
    a = _lore("a", ["青云仙宗"], text="青云仙宗下辖剑峰与丹峰")
    b = _lore("b", ["剑峰", "洗剑池"], text="剑峰下是洗剑池")
    selected = select_lore([a, b], "你到访青云仙宗", max_recursion=1)
    assert [e.id for e in selected] == ["a", "b"]  # 二级命中出现在一级之后


def test_recursion_limited_by_depth():
    a = _lore("a", ["甲地"], text="甲地通往乙地")
    b = _lore("b", ["乙地"], text="乙地通往丙地")
    c = _lore("c", ["丙地"], text="丙地有古碑")
    assert [e.id for e in select_lore([a, b, c], "你到了甲地", max_recursion=1)] == ["a", "b"]
    assert [e.id for e in select_lore([a, b, c], "你到了甲地", max_recursion=2)] == ["a", "b", "c"]


def test_exclude_from_recursion_still_directly_matchable():
    a = _lore("a", ["甲地"], text="甲地通往乙地")
    b = _lore("b", ["乙地"], text="乙地的设定", exclude_from_recursion=True)
    assert [e.id for e in select_lore([a, b], "你到了甲地", max_recursion=1)] == ["a"]
    assert [e.id for e in select_lore([a, b], "你到了乙地")] == ["b"]  # 直接命中仍生效


def test_no_recursion_trigger_does_not_feed_next_round():
    a = _lore("a", ["甲地"], text="甲地通往乙地", no_recursion_trigger=True)
    b = _lore("b", ["乙地"], text="乙地的设定")
    assert [e.id for e in select_lore([a, b], "你到了甲地", max_recursion=1)] == ["a"]


def test_recursion_respects_budget():
    a = _lore("a", ["甲地"], text="甲地通往乙地")
    b = _lore("b", ["乙地"], text="长" * 900)
    assert [e.id for e in select_lore([a, b], "你到了甲地", max_recursion=1, budget=100)] == ["a"]


def test_recursion_returns_each_entry_once():
    """已注入条目不再重复注入（递归多轮叠加后不得出现重复项）。"""
    a = _lore("a", ["甲地"], text="甲地通往乙地")
    b = _lore("b", ["乙地"], text="乙地又通甲地")
    selected = select_lore([a, b], "你到了甲地", max_recursion=3)
    assert [e.id for e in selected] == ["a", "b"]


# ---------------------------------------------------------------------------
# 注入与静态前缀隔离
# ---------------------------------------------------------------------------


def test_lore_injected_only_on_match():
    pack = load_worldpack(PACK_PATH)
    builder = ContextBuilder.from_pack(pack)
    state = GameState.from_pack(pack)
    state.scene = "长安城·东市"
    text = builder.status_text(state, None, recent="")
    assert "<lore>" in text and "东市是长安最热闹" in text
    # 未命中场景：西市 lore 不注入
    assert "西市多胡商店铺" not in text


def test_lore_triggered_by_recent_text():
    pack = load_worldpack(PACK_PATH)
    builder = ContextBuilder.from_pack(pack)
    state = GameState.from_pack(pack)
    state.scene = "长安城·沈府"  # 场景只命中沈家
    text = builder.status_text(state, None, recent="（玩家）想去曲江池看看七夕诗会")
    assert "七夕诗会是长安一年一度" in text  # 由近对话触发
    assert "曲江池是长安胜景" in text


def test_lore_not_in_static_prefix():
    """lore 不进静态前缀——前缀定稿后字节级不变（KV Cache 纪律）。"""
    pack = load_worldpack(PACK_PATH)
    system = ContextBuilder.from_pack(pack).system_message["content"]
    for lore in pack.world.lore:
        assert lore.text not in system


def test_static_prefix_under_budget_with_30_lore(tmp_path):
    """构造含 30+ 条 lore 的包：静态前缀仍 ≤ 8K（design.md §4.6 预算）。"""
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [
        {"id": f"lore{i:02d}", "keys": [f"地点{i:02d}"], "text": f"第{i}条设定的文本。" * 8}
        for i in range(35)
    ]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    pack = load_worldpack(pack_dir)
    system = ContextBuilder.from_pack(pack).system_message["content"]
    assert len(system) <= 8000
    assert all(f"第{i}条设定" not in system for i in range(35))  # lore 零入前缀


# ---------------------------------------------------------------------------
# A-5：动态区内部的放置与排序语义
# ---------------------------------------------------------------------------


def test_status_bar_is_last_block_of_dynamic_zone():
    """A-5：状态栏是动态区最后一个区块——它是引擎真值的权威表述，
    最靠近生成点（design.md §4.4「每轮末尾追加」）。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.scene = "长安城·东市"
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="曲江池 诗会")
    assert text.rstrip().endswith("</agent_status>")
    assert text.index("<scene>") < text.index("<agent_status>")


def test_lore_injected_before_status_bar():
    """A-5：lore 是补充知识，位置在场景/在场角色之后、状态栏之前。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.scene = "长安城·东市"
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent="曲江池 诗会")
    assert "<lore>" in text  # 本场景确有命中，断言才有意义
    assert text.index("<lore>") < text.index("<agent_status>")
    assert text.index("</lore>") < text.index("<agent_status>")


# ---------------------------------------------------------------------------
# schema 校验
# ---------------------------------------------------------------------------


def test_lore_duplicate_id_raises(tmp_path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [
        {"id": "same", "keys": ["甲"], "text": "x"},
        {"id": "same", "keys": ["乙"], "text": "y"},
    ]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError, match="lore 条目 id 重复"):
        load_worldpack(pack_dir)


def test_lore_secondary_keys_without_logic_raises(tmp_path):
    """A-2：有次键就必须显式给 logic——默认值会把 AND_ANY 静默强加给作者。"""
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [{"id": "bad", "keys": ["甲"], "secondary_keys": ["乙"], "text": "x"}]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError, match="必须显式声明 logic"):
        load_worldpack(pack_dir)


def test_lore_invalid_logic_raises(tmp_path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [
        {"id": "bad", "keys": ["甲"], "secondary_keys": ["乙"], "logic": "AND_SOME", "text": "x"}
    ]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError):
        load_worldpack(pack_dir)


def test_lore_empty_secondary_key_raises(tmp_path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [
        {"id": "bad", "keys": ["甲"], "secondary_keys": ["乙", " "], "logic": "AND_ANY", "text": "x"}
    ]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError, match="secondary_keys"):
        load_worldpack(pack_dir)


def test_negative_max_recursion_raises(tmp_path):
    """A-4：递归层数必须是 >= 0 的整数——负数会让递归扫描逻辑失去定义。"""
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["max_recursion"] = -1
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError, match="max_recursion"):
        load_worldpack(pack_dir)


def test_lore_empty_keys_raises(tmp_path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [{"id": "bad", "keys": [], "text": "x"}]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError, match="keys 不能为空"):
        load_worldpack(pack_dir)

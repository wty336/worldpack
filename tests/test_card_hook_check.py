"""confab 卡中性筛查（离线）：把"拆掉第二条通路"这条语料设计纪律钉进 CI。

背景（2026-09-12 实证）：confab 家族靠"材料中不存在该承诺"这一条**缺席证据**通路拦截；
promise 一旦撞上说话人角色卡（底线/禁忌/说话风格），判官多一条"设定矛盾"短路，
拦截率虚高、跨包不可比。实测 P1/P2 初版 7/10 种子撞卡 → 14B 拦 88%；
改成卡中性后同一批用例只拦 27.5%（v3 语料，reports/judge_sensitivity_20260912-15*.json）。
"""

from __future__ import annotations

import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "card_hook_check.py"

_spec = importlib.util.spec_from_file_location("card_hook_check_under_test", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# 2026-09-12 v1 语的词面撞卡（当时记录：xianxia 2 / urban 6）已在 v4 全部清掉
# （重写 seed + 补 T1 种子），现在三个包都应为 0 —— 若再出现非 0，说明新种子又撞卡了。
KNOWN_HOOKS = {"ancient_jianghu": 0, "xianxia_wendao": 0, "urban_neon": 0}


def test_detects_boundary_collision():
    """「稿子」撞底线「不替他人的稿子署名」——真实案例的形态（词面通路）。"""
    card = "身份：文学社主编 底线：不替他人的稿子署名；不把社里的稿子外传"
    hooked = mod.card_hook("林夏说：『周三广播的稿子我替你留了一栏，你署自己的名字。』", card)
    assert "稿子" in hooked and hooked
    # 注意：本条只共享「稿子」——「署自己的名字」与「代他人署名」是语义撞卡而非词面，
    # 本筛查抓不到（见模块 docstring 的必要条件声明）。


def test_neutral_promise_has_no_word_overlap():
    """修好后的形态：借书/约书店 —— 与角色卡零共同二字。"""
    card = "身份：文学社主编 底线：不替他人的稿子署名；不把社里的稿子外传 风格：语速偏慢"
    assert mod.card_hook("林夏低声道：『那本《城南旧事》我再留两天，周五一定还你。』", card) == []


def test_new_packs_are_card_neutral():
    """P1/P2 是跨包对照的"难例同难"基准：必须全中性（否则对照失效）。"""
    for name in ("P1_school_letters", "P2_era_dual"):
        rows = mod.scan_pack(REPO_ROOT / "world-packs" / name)
        assert rows, f"{name} 没有生成 confab 用例"
        assert [r["id"] for r in rows if r["hooked"]] == [], f"{name} 出现词面撞卡"


def test_known_packs_hook_counts_are_recorded():
    """旧三包原语料的词面撞卡条数是文档取数口径，改动语料必须同步改文档。"""
    for name, expected in KNOWN_HOOKS.items():
        rows = mod.scan_pack(REPO_ROOT / "world-packs" / name)
        assert sum(1 for r in rows if r["hooked"]) == expected


def test_pack_without_corpus_is_skipped():
    """world-packs/ 下存在非包的目录（如 baseline_probe）——不得炸。"""
    assert mod.scan_pack(REPO_ROOT / "world-packs" / "baseline_probe") == []

"""评测集冻结守卫（离线）：指纹一致 + 生成器可复现 + 数据自洽。

纪律（`eval-sets/MANIFEST.md` §4）：改评测集 = 改生成器种子 → 重跑 → 同步 digests，
一次显式动作。本文件把这条路口堵死——手改 `eval-sets/*.yaml` 会在这里失败。

本文件不做的事：不测评测集的**效果**（那是真机脚本的事，见 scripts/dedup_test.py、
scripts/extract_eval.py）；只测"尺子本身没被偷偷改过、且结构自洽"。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "eval-sets"
GENERATOR = REPO_ROOT / "scripts" / "build_eval_sets.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("build_eval_sets_under_test", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_yaml(rel: str) -> dict:
    return yaml.safe_load((EVAL_DIR / rel).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 冻结：指纹 + 生成器可复现
# ---------------------------------------------------------------------------


def test_eval_set_files_match_digests():
    """磁盘上的评测文件必须与 digests.json 完全一致（防手改/防漂移）。

    摘要口径 = `game_agent.evalmeta.file_digest`（**换行归一化**后取 sha256）：
    原先直接对 `read_bytes()` 取摘要，会让同一文件在 Windows(CRLF) 与 Linux(LF) 上
    得到两个不同的 sha，本守卫因此在 Linux 上必红（2026-09-12 修复）。
    """
    from game_agent.evalmeta import file_digest

    digests = json.loads((EVAL_DIR / "digests.json").read_text(encoding="utf-8"))
    assert digests["files"], "digests.json 为空——请跑 scripts/build_eval_sets.py --digests"
    for rel, expected in digests["files"].items():
        actual = file_digest(REPO_ROOT / rel)
        assert actual == expected, f"{rel} 与冻结指纹不符：手改了评测集？请改生成器种子并重跑"


def test_generator_reproduces_frozen_files():
    """生成器（种子）必须能原样复现磁盘文件——保证"冻结"可重建、不是黑盒。"""
    gen = _load_generator()
    for rel, builder in gen.TARGETS.items():
        rendered = gen._dump(builder())
        on_disk = (EVAL_DIR / rel).read_text(encoding="utf-8")
        assert rendered == on_disk, f"{rel} 与生成器渲染结果不一致"


# ---------------------------------------------------------------------------
# 结构自洽：规模、极性、覆盖
# ---------------------------------------------------------------------------


def test_dedup_set_has_both_polarities_and_enough_cases():
    pairs = _load_yaml("dedup/pairs.yaml")["pairs"]
    pos = [p for p in pairs if p["expect_duplicate"]]
    neg = [p for p in pairs if not p["expect_duplicate"]]
    assert len(pairs) >= 100, f"规模不足：{len(pairs)}"
    assert len(pos) >= 50, f"正例不足：{len(pos)}"
    assert len(neg) >= 50, f"负例不足：{len(neg)}（旧集全正例的缺陷不能再犯）"
    # 历史 10 组必须保留（新旧数字衔接）
    ids = {p["id"] for p in pairs}
    assert {f"hist_{i:02d}" for i in range(1, 11)} <= ids, "历史 10 组未保留"
    # 每条必须有 a/b/kind 且 a != b
    for p in pairs:
        assert p["a"] and p["b"] and p["kind"] and p["genre"]
        assert p["a"] != p["b"], f"{p['id']} 的 a/b 相同"


def test_dedup_set_spans_genres():
    genres = {p["genre"] for p in _load_yaml("dedup/pairs.yaml")["pairs"]}
    assert len(genres) >= 5, f"题材覆盖不足：{genres}"


def test_extract_set_structure():
    cases = _load_yaml("extract/direct.yaml")["cases"]
    assert len(cases) >= 30, f"规模不足：{len(cases)}"
    genres = {c["genre"] for c in cases}
    assert len(genres) >= 5, f"题材覆盖不足：{genres}"
    for c in cases:
        assert c["turn"] and c["axis_kind"]
        assert c["existing"] is not None
        # 负例与正例互斥且完备
        if c["expect_empty"]:
            assert not c["must_recall"], f"{c['id']} 标为负例却又给了 must_recall"
        else:
            assert c["must_recall"], f"{c['id']} 是正例却没有 must_recall"
        lo, hi = c["importance_range"]
        assert 1 <= lo <= hi <= 10


def test_extract_expectations_are_reachable():
    """自检：`must_recall` 的实体必须字面出现在 turn（或 existing）里——
    否则期望根本不可能达成（写错了的期望会永远判失败）。"""
    cases = _load_yaml("extract/direct.yaml")["cases"]
    for c in cases:
        haystack = c["turn"] + "".join(c["existing"])
        for ent in c["must_recall"]:
            assert ent in haystack, f"{c['id']}: 期望实体「{ent}」不在 turn/existing 中，期望不可达"
        for ent in c["must_not_output"]:
            assert ent in "".join(c["existing"]), (
                f"{c['id']}: must_not_output「{ent}」不在 existing 里（去重纪律无从谈起）"
            )

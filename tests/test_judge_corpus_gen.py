"""judge 语料扩域守卫（离线）：生成物可复现 + 结构自洽 + 合并口径。

Phase 1 · Step 1：每包对抗语料 6 → 20（`scripts/build_judge_corpus.py`）。
手写资产 `judge_corpus.yaml` 与机器产物 `judge_corpus.gen.yaml` **分离**，由
`game_agent.judge_corpus.load_corpus` 合并；本文件守住"生成物没被手改""id 不撞名"
"计数达标""对抗类必须有 note（可审计）"。
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from game_agent.judge_corpus import load_corpus

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "build_judge_corpus.py"
PACKS = ("ancient_jianghu", "xianxia_wendao", "urban_neon", "P1_school_letters")

TARGETS = {"setting": 20, "confab": 20, "ooc": 6, "normal": 12}


def _load_generator():
    spec = importlib.util.spec_from_file_location("build_judge_corpus_under_test", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_files_reproducible():
    """生成物必须能由种子原样重建（否则"扩域"不可复现）。"""
    gen = _load_generator()
    for pack, data in gen.targets().items():
        path = REPO_ROOT / "world-packs" / pack / "judge_corpus.gen.yaml"
        assert path.exists(), f"{pack} 缺生成文件——请跑 scripts/build_judge_corpus.py"
        assert gen._dump(data) == path.read_text(encoding="utf-8"), f"{pack} 生成物与种子不一致"


def test_merged_corpus_meets_targets():
    """合并后计数达标（含手写 6/6/6/12）：setting/confab ≥20。"""
    for pack in PACKS:
        cases = load_corpus(REPO_ROOT / "world-packs" / pack)
        counts = Counter(c.category for c in cases)
        for cat, minimum in TARGETS.items():
            assert counts[cat] >= minimum, f"{pack} 的 {cat} 只有 {counts[cat]} 条（要求 ≥{minimum}）"
        assert len(cases) >= 60, f"{pack} 合计仅 {len(cases)} 条"


def test_generated_cases_are_well_formed():
    """对抗类必须 expected=false 且有 note（可审计）；normal 必须 expected=true。"""
    for pack in PACKS:
        path = REPO_ROOT / "world-packs" / pack / "judge_corpus.gen.yaml"
        import yaml

        for c in yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]:
            assert c["category"] in ("ooc", "setting", "confab", "normal")
            assert c["narration"].strip()
            if c["category"] == "normal":
                assert c["expected"] is True, f"{c['id']} 正常样本应 expected=true"
            else:
                assert c["expected"] is False, f"{c['id']} 对抗样本应 expected=false"
                assert c.get("note"), f"{c['id']} 缺 note（对抗样本必须写明违规依据）"


def test_ids_unique_across_hand_written_and_generated():
    for pack in PACKS:
        cases = load_corpus(REPO_ROOT / "world-packs" / pack)
        ids = [c.id for c in cases]
        assert len(ids) == len(set(ids)), f"{pack} 存在重复 id"


def test_duplicate_id_between_sources_raises(tmp_path):
    """手写与生成撞名必须显式报错，而不是悄悄改变口径。"""
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    case = {
        "id": "dup_01", "category": "ooc", "narration": "沈清秋说：『绝绝子。』",
        "expected": False, "note": "测试用",
    }
    import yaml

    (pack_dir / "judge_corpus.yaml").write_text(
        yaml.safe_dump({"cases": [case]}, allow_unicode=True), encoding="utf-8"
    )
    (pack_dir / "judge_corpus.gen.yaml").write_text(
        yaml.safe_dump({"cases": [case]}, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="id 重复"):
        load_corpus(pack_dir)

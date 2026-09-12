"""真实轨迹 normal 导出（离线）：loader 三源合并 / stats 还原 / 采集管线。

背景：评测集 normal（判官误报率的分母）要来自**真实轨迹**，因此新增
`world-packs/<pack>/judge_corpus.real.yaml`（判官质检通过的真轨迹）与
`scripts/harvest_normals.py`。真轨迹的状态与包初始值不同（属性/好感/记忆都在变），
所以语料 schema 增加 `stats` 字段并在 `build_materials` 里还原——否则状态栏与真实不符。
"""

from __future__ import annotations

import importlib.util
import pathlib

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

from game_agent.judge_corpus import JudgeCase, build_materials, load_corpus  # noqa: E402
from game_agent.worldpack import load_worldpack  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "harvest_normals_under_test", REPO_ROOT / "scripts" / "harvest_normals.py"
)
harvest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harvest)

P1 = REPO_ROOT / "world-packs" / "P1_school_letters"


def _write_corpus(path: pathlib.Path, cases: list[dict], **top) -> None:
    path.write_text(
        yaml.safe_dump({"version": 1, **top, "cases": cases}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_load_corpus_merges_three_sources(tmp_path):
    """手写 + gen + real 三源合并，id 全局唯一。"""
    _write_corpus(
        tmp_path / "judge_corpus.yaml",
        [{"id": "hand_1", "category": "ooc", "narration": "手写", "expected": False}],
    )
    _write_corpus(
        tmp_path / "judge_corpus.gen.yaml",
        [{"id": "gen_1", "category": "confab", "narration": "生成", "expected": False}],
        content_version="v9-test",
    )
    _write_corpus(
        tmp_path / "judge_corpus.real.yaml",
        [{"id": "real_normal_01", "category": "normal", "narration": "真轨迹", "expected": True,
          "stats": {"money": 123.0}}],
        content_version="real-test",
    )

    cases = {c.id: c for c in load_corpus(tmp_path)}

    assert set(cases) == {"hand_1", "gen_1", "real_normal_01"}
    assert cases["real_normal_01"].stats == {"money": 123.0}
    assert cases["real_normal_01"].category == "normal"


def test_build_materials_restores_stats():
    """真轨迹用例若不还原 stats，状态栏就与真实不符（属性会退回包初始值）。"""
    pack = load_worldpack(P1)
    base = JudgeCase(id="x", category="normal", narration="n", expected=True)
    with_stats = JudgeCase(id="y", category="normal", narration="n", expected=True,
                           stats={"money": 999.0})

    assert "999" in build_materials(pack, with_stats)
    assert "999" not in build_materials(pack, base)


def test_offline_harvest_plumbing(tmp_path):
    """离线桩跑通采集：候选必须带齐重建材料所需的全部状态字段。"""
    cases, _ = harvest.collect(str(P1), turns=3, seed=1, days=2, offline=True)

    assert cases, "离线采集应至少产出 1 条候选"
    need = {"turn", "day", "scene", "present", "affections", "facts", "npc_memories",
            "stats", "material", "narration"}
    assert need <= set(cases[0])
    assert cases[0]["narration"].strip()
    assert "<scene>" in cases[0]["material"]


def test_real_file_is_not_regenerated_by_corpus_builder():
    """真实轨迹文件由 harvest 质检产出，生成器不得写它（否则互相覆盖）。"""
    src = (REPO_ROOT / "scripts" / "build_judge_corpus.py").read_text(encoding="utf-8")
    assert "judge_corpus.real.yaml" not in src

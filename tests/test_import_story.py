"""B（素材导入工具）离线测试：物化/修复/语料结构管线 + 冒烟 profile 回退。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORT = REPO_ROOT / "scripts" / "import_story.py"
SMOKE = REPO_ROOT / "scripts" / "worldpack_smoke.py"
SOURCE = REPO_ROOT / "samples" / "star_ring_source.md"

from game_agent.judge_corpus import load_corpus
from game_agent.worldpack import load_worldpack


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(IMPORT), *args],
        capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT,
    )


def test_offline_import_repairs_and_produces_valid_pack(tmp_path: Path):
    """离线管线：坏草稿触发校验错误 → 修复轮 → 最终包可加载（修复路径的确定性回归）。"""
    r = _run(
        [str(SOURCE), "--name", "offline_world", "--pack-dir", str(tmp_path), "--offline"]
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "修复" in r.stdout  # 坏草稿确实触发了修复轮
    assert "check-worldpack 通过" in r.stdout
    pack = load_worldpack(tmp_path / "offline_world")  # 不抛错
    assert pack.world.name == "离线测试世界"
    assert (tmp_path / "offline_world" / "smoke_profile.yaml").exists()


def test_offline_import_with_corpus_passes_structure(tmp_path: Path):
    """--with-corpus：内嵌最小语料集（5/5/5/10）通过结构校验并落盘。"""
    r = _run(
        [
            str(SOURCE), "--name", "offline_world2", "--pack-dir", str(tmp_path),
            "--offline", "--with-corpus",
        ]
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "语料" in r.stdout and "结构通过" in r.stdout
    corpus = load_corpus(tmp_path / "offline_world2")
    assert len(corpus) == 25


def test_offline_import_rejects_illegal_name(tmp_path: Path):
    r = _run([str(SOURCE), "--name", "a b", "--pack-dir", str(tmp_path), "--offline"])
    assert r.returncode != 0


def test_smoke_profile_fallback(tmp_path: Path):
    """worldpack_smoke 的包内 profile 回退：有 smoke_profile.yaml 读文件，无则 None。"""
    spec = importlib.util.spec_from_file_location("worldpack_smoke", SMOKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path / "some_pack"
    root.mkdir()
    fake_pack = SimpleNamespace(root=root)
    assert mod._profile_from_file(fake_pack) is None
    (root / "smoke_profile.yaml").write_text(
        "picks: {a: 0}\naction: work\ndays: 8\nlines: [\"你好\"]\n"
        "forbidden_scan: [魔法]\ntarget: 测试\n", encoding="utf-8"
    )
    profile = mod._profile_from_file(fake_pack)
    assert profile["action"] == "work" and profile["picks"] == {"a": 0}


def test_import_source_exists():
    """测试素材（原创太空歌剧大纲）随仓库存在，供真机导入使用。"""
    assert SOURCE.exists() and len(SOURCE.read_text(encoding="utf-8")) > 500

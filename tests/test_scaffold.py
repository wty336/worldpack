"""E3 脚手架测试（离线，P3）：生成即过校验、幂等冲突、模板完整性。"""

from __future__ import annotations

import pytest

from game_agent.scaffold import TEMPLATES, init_worldpack
from game_agent.worldpack import load_worldpack


def test_init_worldpack_passes_validation(tmp_path):
    """脚手架生成的骨架必须立即可通过 load_worldpack（模板自身过质量门）。"""
    target = init_worldpack("demo_world", tmp_path)
    assert (target / "world.yaml").exists()
    assert (target / "schedule.yaml").exists()
    assert (target / "mainline.yaml").exists()
    assert (target / "events.yaml").exists()
    assert (target / "endings.yaml").exists()
    assert (target / "npcs" / "a_jiu.yaml").exists()
    pack = load_worldpack(target)
    assert pack.world.name == "demo_world"
    assert len(pack.world.lore) == 1  # B1 样板
    assert any(a.check is not None for a in pack.schedule.actions)  # P2 检定样板


def test_init_worldpack_existing_dir_raises(tmp_path):
    init_worldpack("demo_world", tmp_path)
    with pytest.raises(FileExistsError):
        init_worldpack("demo_world", tmp_path)


def test_init_worldpack_rejects_illegal_names(tmp_path):
    """C-2（M7）：路径穿越/盘符/空格/中文名一律拒绝。"""
    for bad in ("../evil", "C:/evil", "a b", "名字", "a/b", "", "a.b"):
        with pytest.raises(ValueError, match="非法世界包名"):
            init_worldpack(bad, tmp_path)
    assert not (tmp_path / "evil").exists()


def test_templates_are_commented_manual():
    """模板即注释手册：六个骨架都带规范指引（指向独立作者手册）。"""
    assert "docs/worldpack-manual.md" in TEMPLATES["world.yaml"]
    assert "critical_effects" in TEMPLATES["schedule.yaml"]  # P2 特性说明
    assert "completion" in TEMPLATES["mainline.yaml"]
    assert "kind:" in TEMPLATES["events.yaml"]
    assert "auto" in TEMPLATES["endings.yaml"]
    assert "affection_stages" in TEMPLATES["npcs/a_jiu.yaml"]

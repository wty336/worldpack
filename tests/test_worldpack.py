"""W2 验收测试：世界包 schema 校验 + 交叉校验。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from game_agent.worldpack import WorldPackError, load_worldpack

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_PATH = REPO_ROOT / "world-packs" / "ancient_jianghu"


def _make_minimal_pack(tmp_path: Path) -> Path:
    """构造一个只有骨架、通过 schema 的最小世界包（world 内容缺字段故意留空）。"""
    (tmp_path / "npcs").mkdir()
    (tmp_path / "world.yaml").write_text(
        "name: 测试\n", encoding="utf-8"  # 缺 era → 应触发 schema 错误
    )
    (tmp_path / "schedule.yaml").write_text(
        "stats: {}\naffections: {}\n", encoding="utf-8"
    )
    (tmp_path / "mainline.yaml").write_text("nodes: []\n", encoding="utf-8")
    (tmp_path / "events.yaml").write_text("events: []\n", encoding="utf-8")
    (tmp_path / "endings.yaml").write_text("endings: []\n", encoding="utf-8")
    return tmp_path


def test_load_ancient_jianghu():
    """真实世界包《江湖旧梦》应加载成功且字段完整。"""
    pack = load_worldpack(PACK_PATH)
    assert pack.world.name == "江湖旧梦"
    assert set(pack.schedule.stats) == {"charm", "martial", "silver"}
    assert set(pack.schedule.affections) == {"shen_qingqiu"}
    assert "shen_qingqiu" in pack.npcs
    assert pack.npcs["shen_qingqiu"].name == "沈清秋"
    assert len(pack.mainline.nodes) == 2
    assert len(pack.events.events) == 2
    assert len(pack.endings.endings) == 2
    # 好感阶段区间升序且覆盖 0~100
    stages = pack.npcs["shen_qingqiu"].affection_stages
    assert stages[0].range[0] == 0 and stages[-1].range[1] == 100


def test_missing_field_raises(tmp_path: Path):
    """schema 缺失字段（world.yaml 缺 era）必须报错。"""
    root = _make_minimal_pack(tmp_path)
    with pytest.raises(WorldPackError, match="era"):
        load_worldpack(root)


def test_undeclared_flag_raises(tmp_path: Path):
    """引用了 schedule 未声明的 flag 必须报错（防拼写错误）。"""
    shutil.copytree(PACK_PATH, tmp_path / "pack")
    endings = tmp_path / "pack" / "endings.yaml"
    text = endings.read_text(encoding="utf-8").replace("poetry_top3", "poetry_top_3")
    endings.write_text(text, encoding="utf-8")
    with pytest.raises(WorldPackError, match="未声明的 flag"):
        load_worldpack(tmp_path / "pack")


def test_affection_without_card_raises(tmp_path: Path):
    """好感对象缺少角色卡必须报错。"""
    shutil.copytree(PACK_PATH, tmp_path / "pack")
    schedule = tmp_path / "pack" / "schedule.yaml"
    text = schedule.read_text(encoding="utf-8").replace(
        "shen_qingqiu: {label: 沈清秋, min: 0, max: 100, initial: 5}",
        "shen_qingqiu: {label: 沈清秋, min: 0, max: 100, initial: 5}\n"
        "  gu_changge: {label: 顾长歌, min: 0, max: 100, initial: 0}",
    )
    schedule.write_text(text, encoding="utf-8")
    with pytest.raises(WorldPackError, match="缺少对应角色卡"):
        load_worldpack(tmp_path / "pack")


def test_bad_event_trigger_raises(tmp_path: Path):
    """事件日程触发引用了不存在的行动必须报错。"""
    shutil.copytree(PACK_PATH, tmp_path / "pack")
    events = tmp_path / "pack" / "events.yaml"
    text = events.read_text(encoding="utf-8").replace("action: cultivate", "action: none")
    events.write_text(text, encoding="utf-8")
    with pytest.raises(WorldPackError, match="不存在的日程行动"):
        load_worldpack(tmp_path / "pack")


def test_node_present_unknown_npc_raises(tmp_path: Path):
    """节点 on_enter.present 引用了不存在的 NPC 必须报错。"""
    shutil.copytree(PACK_PATH, tmp_path / "pack")
    mainline = tmp_path / "pack" / "mainline.yaml"
    text = mainline.read_text(encoding="utf-8").replace(
        "present: [shen_qingqiu]", "present: [nobody]"
    )
    mainline.write_text(text, encoding="utf-8")
    with pytest.raises(WorldPackError, match="不存在的 NPC"):
        load_worldpack(tmp_path / "pack")


def test_completion_flag_unreachable_raises(tmp_path: Path):
    """completion 要求的 flag 没有任何代码路径可写 → 加载期拒绝（防节点永远卡死）。"""
    shutil.copytree(PACK_PATH, tmp_path / "pack")
    schedule = tmp_path / "pack" / "schedule.yaml"
    text = schedule.read_text(encoding="utf-8").replace(
        "poetry_join: false", "poetry_join: false\n  phantom: false"
    )
    schedule.write_text(text, encoding="utf-8")
    mainline = tmp_path / "pack" / "mainline.yaml"
    text = mainline.read_text(encoding="utf-8").replace(
        "flags: {poetry_resolved: true}", "flags: {phantom: true}"
    )  # 只替换 completion 行（选项效果里的 poetry_resolved 保持原样）
    mainline.write_text(text, encoding="utf-8")
    with pytest.raises(WorldPackError, match="没有任何代码路径"):
        load_worldpack(tmp_path / "pack")

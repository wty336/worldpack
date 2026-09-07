"""B1 Lorebook 测试（离线，P3）：按需注入、预算、静态前缀隔离、schema 校验。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from game_agent.context import ContextBuilder, LORE_BUDGET, select_lore
from game_agent.state import GameState
from game_agent.worldpack import LoreSpec, WorldPackError, load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _lore(id_: str, keys: list[str], text: str | None = None) -> LoreSpec:
    return LoreSpec(id=id_, keys=keys, text=text or f"{id_} 的设定文本")


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


def test_lore_empty_keys_raises(tmp_path):
    pack_dir = tmp_path / "pack"
    shutil.copytree(PACK_PATH, pack_dir)
    world = pack_dir / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    data["lore"] = [{"id": "bad", "keys": [], "text": "x"}]
    world.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WorldPackError, match="keys 不能为空"):
        load_worldpack(pack_dir)

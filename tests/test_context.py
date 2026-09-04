"""W4 验收测试（离线）：上下文组装 KV Cache 三铁律 + tone 注入 + 渐进式披露。"""

from __future__ import annotations

from pathlib import Path

from game_agent.context import ContextBuilder
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _builder_and_state():
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.present_npcs = ["shen_qingqiu"]
    return pack, state, ContextBuilder.from_pack(pack)


def test_system_prefix_stable():
    """静态前缀字节级冻结：不同轮次组装，system 消息必须完全一致（KV Cache 铁律 1）。"""
    pack, state, b = _builder_and_state()
    m1 = b.build_messages(state, [])
    m2 = b.build_messages(state, [], node=pack.mainline.nodes[0])
    assert m1[0] == m2[0] == b.system_message
    content = b.system_message["content"]
    assert "【禁用】" in content and "手机" in content  # 世界观核心注入
    assert "沈清秋：沈家嫡女" in content  # NPC 目录常驻
    assert "【引擎协议】" in content


def test_messages_layout():
    """消息布局：system 前缀 → 历史 → 状态栏（user 角色、末尾追加，铁律 2）。"""
    pack, state, b = _builder_and_state()
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "……"}]
    msgs = b.build_messages(state, history)
    assert msgs[0]["role"] == "system"
    assert msgs[1] == history[0] and msgs[2] == history[1]
    assert msgs[-1]["role"] == "user"
    assert "<agent_status>" in msgs[-1]["content"]
    assert "<scene>" in msgs[-1]["content"]


def test_status_bar_contents():
    """状态栏必须由代码提炼显式状态：数值、好感、节点目标、场景。"""
    pack, state, b = _builder_and_state()
    node = pack.mainline.nodes[0]
    text = b.status_text(state, node)
    assert "魅力 10" in text and "银两 50" in text
    assert "沈清秋 5/100" in text
    assert f"「{node.title}」" in text and node.goal in text
    assert f"第 {state.day} 天" in text and state.scene in text


def test_tone_follows_affection():
    """角色卡 tone 按当前好感注入（章 8 好感阶段）。"""
    pack, state, b = _builder_and_state()
    state.affections["shen_qingqiu"] = 5.0
    assert "冷淡疏离" in b.status_text(state)
    assert "亲近信任" not in b.status_text(state)
    state.affections["shen_qingqiu"] = 60.0
    text = b.status_text(state)
    assert "亲近信任" in text
    assert "冷淡疏离" not in text


def test_present_npc_card_only_when_present():
    """渐进式披露：完整角色卡只在 NPC 出场时加载。"""
    pack, state, b = _builder_and_state()
    state.present_npcs = []
    assert "<在场角色>" not in b.status_text(state)
    state.present_npcs = ["shen_qingqiu"]
    text = b.status_text(state)
    assert "<在场角色>" in text
    assert "性格" in text and "说话风格" in text and "底线" in text


def test_secrets_not_leaked():
    """NPC secrets 不得注入上下文（M1 无揭示机制）。"""
    pack, state, b = _builder_and_state()
    assert "顾长歌" not in b.status_text(state)
    assert "顾长歌" not in b.system_message["content"]


def test_identity_and_goal_in_status():
    """试玩反馈 #1：身份与目标常驻状态栏，给玩家与模型稳定的锚点。"""
    pack, state, b = _builder_and_state()
    text = b.status_text(state)
    assert "你的身份" in text and "游侠" in text
    assert "你的目标" in text and "查明师父旧事" in text

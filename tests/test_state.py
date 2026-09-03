"""W3 验收测试：GameState 构造、中立序列化、快照。"""

from __future__ import annotations

from pathlib import Path

from game_agent.state import ChoiceRecord, GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def test_from_pack_initial_values():
    """初始值必须全部来自世界包定义，引擎不含任何内容。"""
    pack = load_worldpack(PACK_PATH)
    s = GameState.from_pack(pack)
    assert s.pack_name == "江湖旧梦"
    assert s.stats == {"charm": 10.0, "martial": 5.0, "silver": 50.0}
    assert s.affections == {"shen_qingqiu": 5.0}
    assert s.flags == {"met_shen": False, "poetry_join": False, "poetry_top3": False}
    assert s.day == 1
    assert s.scene == "长安城·东市"
    assert s.current_node is None
    assert s.choice_log == [] and s.stat_log == [] and s.triggered_events == []


def test_dict_roundtrip():
    """中立格式往返必须无损（W7 存档文件 I/O 的地基）。"""
    pack = load_worldpack(PACK_PATH)
    s = GameState.from_pack(pack)
    s.day = 9
    s.flags["met_shen"] = True
    s.current_node = "n2_poetry_festival"
    s.choice_log.append(
        ChoiceRecord(day=7, node_id="n2_poetry_festival", choice_id="join", text="欣然登台")
    )
    s2 = GameState.from_dict(s.to_dict())
    assert s2 == s


def test_copy_is_independent():
    """copy() 必须是深拷贝：改副本不影响原状态（回滚依据）。"""
    pack = load_worldpack(PACK_PATH)
    s = GameState.from_pack(pack)
    c = s.copy()
    c.stats["charm"] = 99.0
    c.flags["met_shen"] = True
    c.choice_log.append(ChoiceRecord(day=1, node_id=None, choice_id="x", text="y"))
    assert s.stats["charm"] == 10.0
    assert s.flags["met_shen"] is False
    assert s.choice_log == []

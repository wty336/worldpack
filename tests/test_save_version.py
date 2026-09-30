"""存档版本与回合轴对账（K 系列守卫）。

## 问题

`turn_count` 原本只是"记忆来源追踪"的计数，缺了也无所谓。K 系列把它升级成了
**成本归因的时间轴**：trace 事件的 `game_turn`、usage 条目的 `game_turn` 都用它。
而旧存档里没有这个字段 → `from_dict` 默认 0 → 读档后重新从 1 计数，但
`usage.jsonl` 是**追加**的：同一个文件里两段 `game_turn` 重复且错位，
`trace_report --by-game-turn` 会把两段合成一行，成本静默算错。

## 契约

- 新存档写 `save_version`；旧存档（无该字段）读档时**对账** `turn_count`；
- 对账取"历史尾部玩家回合数"作**下界**——压缩会把多轮折成一条摘要，
  真实值可能更大，故只补不漏，绝不凭空放大；
- 新存档的 `turn_count` 是权威值，读档**不得**改写。
"""

from __future__ import annotations

import json
from pathlib import Path

from game_agent.save import (
    SAVE_VERSION,
    _player_turns_in_tail,
    load_game,
    load_history,
    resolve_turn_count,
    save_game,
)
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "baseline_probe"


def _state() -> GameState:
    return GameState.from_pack(load_worldpack(PACK_PATH))


def _history(player_turns: int) -> list[dict]:
    """构造含 n 个玩家回合的历史（每个回合 = 玩家输入 + 模型叙事）。"""
    out: list[dict] = []
    for i in range(player_turns):
        out.append({"role": "user", "origin": "player", "content": f"玩家第 {i} 轮"})
        out.append({"role": "assistant", "origin": "model", "content": f"叙事第 {i} 轮"})
    return out


def _legacy_dict(player_turns: int = 6, **extra) -> dict:
    """由**真实状态**派生的旧档（缺 `save_version` 与 `turn_count`）。

    从 `to_dict()` 派生而不是手搓字典：存档 schema 有 20+ 必填键，手搓的字典会随
    schema 演进腐烂，而且**测不出真实旧档的形状**（真实旧档就是少这两个键的 to_dict）。
    """
    state = _state()
    state.turn_count = 0
    data = state.to_dict()
    data.pop("turn_count", None)
    data["history"] = _history(player_turns)
    data.update(extra)
    return data


def _write(tmp_path: Path, data: dict, name: str = "legacy.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def test_save_marks_version_and_turn_count(tmp_path):
    state = _state()
    state.turn_count = 7
    p = tmp_path / "s.json"
    save_game(state, p, _history(3))

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["save_version"] == SAVE_VERSION
    assert data["turn_count"] == 7


def test_save_without_history_still_versioned(tmp_path):
    """history 可选，但版本字段必须写（它是格式属性，不依赖历史）。"""
    p = tmp_path / "s.json"
    save_game(_state(), p)
    assert json.loads(p.read_text(encoding="utf-8"))["save_version"] == SAVE_VERSION


# ---------------------------------------------------------------------------
# 读档对账
# ---------------------------------------------------------------------------


def test_new_save_turn_count_is_authoritative(tmp_path):
    """新存档：turn_count 原样取用，不被历史条数改写。"""
    state = _state()
    state.turn_count = 42
    p = tmp_path / "s.json"
    save_game(state, p, _history(2))  # 历史上只有 2 个回合

    assert load_game(p).turn_count == 42
    assert resolve_turn_count(json.loads(p.read_text(encoding="utf-8"))) is None


def test_legacy_save_reconciles_turn_count(tmp_path):
    """旧存档（无 save_version、无 turn_count）→ 用历史尾部玩家回合数对账。

    修复前：读档后 turn_count=0，与同一 usage.jsonl 里的既有回合号重复，
    `--by-game-turn` 把两段合成一行。
    """
    p = _write(tmp_path, _legacy_dict(6))
    state = load_game(p)
    assert state.turn_count == 6, "旧存档必须对账出非零回合号"
    assert state.day == _state().day  # 其余字段照常恢复


def test_legacy_save_never_lowers_recorded_value(tmp_path):
    """旧存档里已有非零 turn_count（1.x 偶发写入）→ 只补不漏。"""
    assert resolve_turn_count(_legacy_dict(3, turn_count=20)) == 20


def test_compressed_history_yields_lower_bound_not_overcount():
    """压缩过的历史只剩 1 个玩家回合，但记录的 turn_count 更大 → 取记录值。

    这条钉住"只补不漏"的方向：摘要把多轮折成一条，若反过来用历史条数覆盖，
    会把回合号**调小**，跨档时间轴就断了。
    """
    data = _legacy_dict(0)
    data["turn_count"] = 30
    data["history"] = [
        {"role": "user", "name": "engine", "content": "【剧情摘要】……"},
        {"role": "user", "content": "最近一轮"},
    ]
    assert resolve_turn_count(data) == 30


def test_engine_meta_messages_do_not_count_as_turns():
    """A-2 口径：带 name 的引擎元消息不算玩家回合（否则对账会虚高）。"""
    history = [
        {"role": "user", "name": "engine", "content": "【时序推进】今天结束了。"},
        {"role": "user", "name": "engine", "content": "[反重复提示] ……"},
        {"role": "user", "content": "真实玩家输入"},
    ]
    assert _player_turns_in_tail({"history": history}) == 1


def test_empty_legacy_save_leaves_zero(tmp_path):
    """无历史的旧存档 → 不编造回合号（保持 0，与改前一致）。"""
    p = _write(tmp_path, _legacy_dict(0))
    assert load_game(p).turn_count == 0
    assert resolve_turn_count(_legacy_dict(0)) is None


def test_history_survives_roundtrip(tmp_path):
    """读档不破坏 history（对账只碰 turn_count）。"""
    state = _state()
    state.turn_count = 3
    hist = _history(3)
    p = tmp_path / "s.json"
    save_game(state, p, hist)

    state2 = load_game(p)
    hist2 = load_history(p)
    assert hist2 == hist
    assert state2.turn_count == 3


def test_real_repo_saves_still_load_and_reconcile():
    """**真实历史存档**回归：仓库里已有的 saves/*.json 必须照常读入。

    这些是实际跑出来的存档（含带 tool_calls 的长历史），比任何手搓夹具都更有说服力
    ——它们就是"旧档"本身。
    """
    saves = Path(__file__).resolve().parent.parent / "saves"
    files = sorted(saves.glob("smoke-*.json"))
    if not files:
        import pytest

        pytest.skip("仓库内无 smoke 存档可作回归样本")

    for f in files:
        state = load_game(f)  # 不得抛异常
        assert state.pack_name
        data = json.loads(f.read_text(encoding="utf-8"))
        turns = _player_turns_in_tail(data)
        if "save_version" in data:
            assert state.turn_count == data["turn_count"], f"{f.name}: 新档不得改写"
        else:
            assert state.turn_count >= turns, f"{f.name}: 旧档对账不得低于历史下界"


def test_web_load_uses_same_reconciled_loader(tmp_path):
    """Web 读档走的是同一个 `load_game` → 对账逻辑不是只有 CLI 受益。"""
    from game_agent import save as save_mod
    from game_agent import web

    assert web.load_game is save_mod.load_game

    # 旧档经 Web 路径读入也必须对账
    p = _write(tmp_path, _legacy_dict(4), "web.json")
    assert web.load_game(p).turn_count == 4

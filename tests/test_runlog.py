"""runtime 平台化 ② 守卫测试：Run checkpoint + Replay（离线，FakeClient）。

契约（docs/plan-runtime-platform.md §2）：
- checkpoint = 每回合全量 state.to_dict() + history，加载回环等价；
- rebuild_game：state/history 原位还原；
- 确定性重放：同 checkpoint + 同玩家动作 + 同（脚本化）模型响应 → 叙事与状态完全复现；
- prompt 补丁：每条 old 恰好出现一次（0/2 次报错）；只改系统提示词（其余消息不动）；
- 引擎主循环零改动（recorder 由驱动调用）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.runlog import RunRecorder, apply_prompt_patch, rebuild_game
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack
from scripts.replay import _apply_action

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _submit(tid: str, narration: str):
    return tool_call(
        tid, "submit_narration",
        {"narration": narration, "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )


def _game(responses):
    pack = load_worldpack(PACK_PATH)
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    return pack, Game(pack, GameState.from_pack(pack), llm)


# ---------------------------------------------------------------------------
# 记录与重建
# ---------------------------------------------------------------------------


def test_recorder_checkpoint_roundtrip(tmp_path):
    rec = RunRecorder.create(tmp_path, run_id="r1")
    pack, game = _game([])
    game.state.day = 9
    rec.checkpoint(0, game.state, game.history, None, None)
    cp = rec.load_checkpoint(0)
    assert cp["state"] == game.state.to_dict()
    assert cp["history"] == game.history
    entries = rec.entries()
    assert len(entries) == 1 and entries[0]["turn"] == 0 and entries[0]["run_id"] == "r1"


def test_rebuild_game_restores_state_and_history(tmp_path):
    rec = RunRecorder.create(tmp_path, run_id="r2")
    pack, game = _game([])
    game.state.day = 9
    game.state.flags["met_shen"] = True
    game.history = [{"role": "user", "content": "你好"}]
    rec.checkpoint(1, game.state, game.history, {"kind": "say", "payload": "你好"}, None)
    cp = rec.load_checkpoint(1)
    llm2 = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    game2 = rebuild_game(pack, cp["state"], cp["history"], llm2)
    assert game2.state == game.state
    assert game2.history == game.history


# ---------------------------------------------------------------------------
# prompt 补丁
# ---------------------------------------------------------------------------


def test_apply_prompt_patch_replaces_once(tmp_path):
    from game_agent.context import ENGINE_RULES

    p = tmp_path / "patch.yaml"
    p.write_text("replace:\n  - {old: 数值纪律, new: 数值纪律（强化）}\n", encoding="utf-8")
    out = apply_prompt_patch(p, ENGINE_RULES)
    assert "数值纪律（强化）" in out
    assert out.count("数值纪律（强化）") == 1
    assert out != ENGINE_RULES


def test_apply_prompt_patch_rejects_missing_or_duplicate(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("replace:\n  - {old: 不存在的文本XYZ, new: 甲}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="恰好 1 次"):
        apply_prompt_patch(p, "原文")
    p.write_text("replace:\n  - {old: 重复, new: 甲}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="恰好 1 次"):
        apply_prompt_patch(p, "重复 重复")


# ---------------------------------------------------------------------------
# 确定性重放
# ---------------------------------------------------------------------------


def test_replay_same_input_reproduces_turn(tmp_path):
    """同 checkpoint + 同动作 + 同模型响应 → 叙事与状态完全复现（重放可复现性）。"""
    rec = RunRecorder.create(tmp_path, run_id="r3")
    pack, game = _game([
        resp(msg(tool_calls=[_submit("s1", "你出手解围，沈清秋向你道谢。")])),  # pick 回合
        resp(msg(tool_calls=[_submit("s2", "她轻声讲起长安旧事。")])),          # say 回合
    ])
    rec.checkpoint(0, game.state, [], None, None)
    view = game.start()
    rec.checkpoint(1, game.state, game.history, {"kind": "start"}, None)
    view = game.pick(0)
    rec.checkpoint(
        2, game.state, game.history,
        {"kind": "pick", "payload": 0},
        {"narration": (view.narration or "")[:120], "stat_changes": 1},
    )
    view = game.say("讲个故事")
    rec.checkpoint(
        3, game.state, game.history,
        {"kind": "say", "payload": "讲个故事"},
        {"narration": (view.narration or "")[:120]},
    )

    # 重放第 3 回合：checkpoint(2) + say（同一脚本化响应）
    cp = rec.load_checkpoint(2)
    llm2 = LLMClient(
        FakeClient([resp(msg(tool_calls=[_submit("s2b", "她轻声讲起长安旧事。")]))]),
        "fake", build_tools(pack.schedule),
    )
    game2 = rebuild_game(pack, cp["state"], cp["history"], llm2)
    entry = next(e for e in rec.entries() if e["turn"] == 3)
    view2, _ = _apply_action(game2, entry)
    assert view2.narration == "她轻声讲起长安旧事。"
    assert game2.state == game.state  # 状态同样复现


def test_prompt_patch_changes_system_message_only(tmp_path):
    import game_agent.context as ctx

    pack, game = _game([resp(msg(tool_calls=[_submit("s1", "你出手解围。")]))])
    rec = RunRecorder.create(tmp_path, run_id="r4")
    rec.checkpoint(0, game.state, [], None, None)
    game.start()
    rec.checkpoint(1, game.state, game.history, {"kind": "start"}, None)
    game.pick(0)
    rec.checkpoint(2, game.state, game.history, {"kind": "pick", "payload": 0}, {"narration": "你出手解围。"})

    patch = tmp_path / "p.yaml"
    patch.write_text("replace:\n  - {old: 数值纪律, new: 数值纪律（实验补丁）}\n", encoding="utf-8")
    original = ctx.ENGINE_RULES
    ctx.ENGINE_RULES = apply_prompt_patch(patch, original)
    try:
        cp = rec.load_checkpoint(1)
        llm2 = LLMClient(
            FakeClient([resp(msg(tool_calls=[_submit("s2", "你出手解围。")]))]),
            "fake", build_tools(pack.schedule),
        )
        game2 = rebuild_game(pack, cp["state"], cp["history"], llm2)
        entry = next(e for e in rec.entries() if e["turn"] == 2)
        _apply_action(game2, entry)
        assert "数值纪律（实验补丁）" in game2.builder.system_message["content"]
        assert "数值纪律（实验补丁）" not in game.builder.system_message["content"]  # 原局不受影响
    finally:
        ctx.ENGINE_RULES = original

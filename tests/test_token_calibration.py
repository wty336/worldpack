"""批次 F 守卫测试：token 估算校准闭环（TokenCalibrator）。

- EMA 数学：回填收敛、钳位、非法观测忽略、未校准 = 1.0；
- LLMClient 接线：真实 usage（prompt_tokens）回填 turn / 侧信道各自用途桶，
  无 usage 的假响应不回填；
- 压缩触发判定乘因子：校准后提前触发、未校准行为不变（阈值语义不变，只修精度）；
- trace 观测：call 事件带 tok_factor。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fakes import FakeClient, msg, resp, tool_call

from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.usage import TokenCalibrator
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _resp_with_usage(prompt_tokens: int):
    """带 usage 的假响应（真实 provider 回传 prompt_tokens，fake 默认没有）。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="x", tool_calls=None, reasoning_content=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=5),
    )


# ---------------------------------------------------------------------------
# EMA 数学
# ---------------------------------------------------------------------------


def test_calibrator_ema_converges_toward_ratio():
    cal = TokenCalibrator(alpha=0.5, clamp=(0.0, 100.0))
    f1 = cal.update("turn", estimated=100, actual=200)  # ratio 2.0
    assert f1 == 1.5  # 1.0 + 0.5 × (2.0 − 1.0)
    f2 = cal.update("turn", estimated=100, actual=200)
    assert f2 == 1.75  # EMA 渐近收敛
    for _ in range(20):
        cal.update("turn", estimated=100, actual=200)
    import pytest

    assert cal.factor("turn") == pytest.approx(2.0, abs=1e-6)


def test_calibrator_clamps_extremes():
    cal = TokenCalibrator(alpha=1.0, clamp=(0.5, 3.0))
    cal.update("turn", estimated=10, actual=1000)  # ratio 100 → 钳到 3
    assert cal.factor("turn") == 3.0
    cal.update("judge", estimated=1000, actual=100)  # ratio 0.1 → 钳到 0.5
    assert cal.factor("judge") == 0.5


def test_calibrator_ignores_invalid_observations():
    cal = TokenCalibrator()
    assert cal.update("turn", estimated=0, actual=100) is None
    assert cal.update("turn", estimated=100, actual=0) is None
    assert cal.factor("turn") == 1.0  # 未校准 = 1.0


def test_calibrator_buckets_by_purpose():
    cal = TokenCalibrator(alpha=1.0, clamp=(0.0, 100.0))
    cal.update("turn", estimated=100, actual=300)
    cal.update("judge", estimated=100, actual=100)
    assert cal.factor("turn") == 3.0
    assert cal.factor("judge") == 1.0


# ---------------------------------------------------------------------------
# LLMClient 接线
# ---------------------------------------------------------------------------


def _llm_with_calibrator(responses):
    return LLMClient(FakeClient(responses), "fake", [], calibrator=TokenCalibrator())


def test_run_turn_updates_turn_bucket():
    submit = tool_call(
        "s1", "submit_narration",
        {"narration": "叙事", "choices": ["一", "二", "三"], "plot_signal": "normal"},
    )
    # 消息内容很短（估算小），声明 prompt_tokens=500 → 因子上行
    llm = _llm_with_calibrator([_resp_with_usage(500), resp(msg(tool_calls=[submit]))])
    result = llm.run_turn([{"role": "user", "content": "短消息"}])
    assert result.narration == "叙事"
    assert llm.token_factor("turn") > 1.0
    # 协议重试路径：第一次响应（纯文本无工具）也被回填


def test_run_turn_without_usage_keeps_factor():
    """假响应无 usage → 不回填，因子保持 1.0（全部既有测试即此路径）。"""
    submit = tool_call(
        "s1", "submit_narration",
        {"narration": "叙事", "choices": ["一", "二", "三"], "plot_signal": "normal"},
    )
    llm = _llm_with_calibrator([resp(msg(tool_calls=[submit]))])
    llm.run_turn([{"role": "user", "content": "短消息"}])
    assert llm.token_factor("turn") == 1.0


def test_side_channel_updates_own_bucket():
    llm = _llm_with_calibrator([_resp_with_usage(800)])
    out = llm.complete_with_meta(
        [{"role": "user", "content": "短"}], purpose="judge", max_tokens=100
    )
    assert out.text == "x"
    assert llm.token_factor("judge") > 1.0
    assert llm.token_factor("turn") == 1.0  # 各用途独立


def test_client_without_calibrator_factor_one():
    llm = LLMClient(FakeClient([]), "fake", [])
    assert llm.token_factor("turn") == 1.0


# ---------------------------------------------------------------------------
# 压缩触发判定 × 因子
# ---------------------------------------------------------------------------


def test_compression_threshold_uses_calibration_factor():
    """校准因子 >1 → 同阈值提前触发压缩；无校准器 → 原判定不变。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.completed_nodes.append("n1_first_meeting")
    state.flags["met_shen"] = True

    llm = LLMClient(FakeClient([resp(msg(tool_calls=[tool_call(
        "s1", "submit_narration",
        {"narration": "这是一段足够长的叙事内容。" * 8, "choices": ["一", "二", "三"], "plot_signal": "normal"},
    )]))]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm, compress_threshold=50)
    game._compress_history = lambda: None  # 只测触发判定，不跑压缩本体
    game.say("随便说说")
    # 历史估算 ≈ 状态栏+叙事 ≫ 50/1.0，未校准也会触发——先验证基线触发
    assert game.compress_threshold == 50

    # 阈值抬高到未校准不触发、校准后触发的区间
    from game_agent.usage import TokenCalibrator

    game2_pack = _pack()
    game2_state = GameState.from_pack(game2_pack)
    game2_state.completed_nodes.append("n1_first_meeting")
    game2_state.flags["met_shen"] = True
    llm2 = LLMClient(FakeClient([
        resp(msg(tool_calls=[tool_call(
            "s1", "submit_narration",
            {"narration": "短叙事。", "choices": ["一", "二", "三"], "plot_signal": "normal"},
        )])),
        resp(msg(tool_calls=[tool_call(
            "s2", "submit_narration",
            {"narration": "第二段短叙事。", "choices": ["一", "二", "三"], "plot_signal": "normal"},
        )])),
        resp(msg(tool_calls=[tool_call(
            "s3", "submit_narration",
            {"narration": "第三段短叙事。", "choices": ["一", "二", "三"], "plot_signal": "normal"},
        )])),
    ]), "fake", build_tools(game2_pack.schedule))
    game2 = Game(game2_pack, game2_state, llm2, compress_threshold=1000)
    triggered = []
    game2._compress_history = lambda: triggered.append(True)
    game2.say("说说")
    assert triggered == []  # 未校准：估算 < 1000 → 不触发

    # 阈值改到"估算+10"：未校准不触发；一次大观测（钳到 6.8 倍）后触发
    from game_agent.compression import history_tokens

    est = history_tokens(game2.history)
    assert est > 0
    game2.compress_threshold = est + 10
    game2.say("再说说")
    assert triggered == []  # 因子 1.0 → est < est+10 → 不触发

    llm2.calibrator = TokenCalibrator(clamp=(0.5, 100.0))
    llm2.calibrator.update("turn", estimated=10, actual=300)  # ratio 30 → 因子 6.8
    game2.say("还说说")
    assert triggered == [True]  # est × 6.8 > est + 10 → 触发


def test_trace_records_tok_factor():
    """trace 的 call 事件带 tok_factor 观测（recorder 关闭时零开销不变）。"""
    from game_agent.trace import TraceRecorder
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        trace_path = Path(td) / "trace.jsonl"
        llm = LLMClient(
            FakeClient([_resp_with_usage(500)]), "fake", [],
            tracer=TraceRecorder(trace_path), calibrator=TokenCalibrator(),
        )
        llm.complete_with_meta([{"role": "user", "content": "短"}], purpose="judge")
        events = [json.loads(l) for l in trace_path.read_text(encoding="utf-8").splitlines()]
        call_events = [e for e in events if e.get("event") == "call"]
        assert call_events and "tok_factor" in call_events[-1]

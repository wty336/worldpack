"""C2/C1 验收测试（离线，P1）：usage 记账 + 模型分层路由。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fakes import tool_call

from game_agent.config import Settings
from game_agent.llm import LLMClient, build_tools
from game_agent.usage import UsageTracker, usage_fields
from game_agent.worldpack import load_worldpack

from pathlib import Path

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


# ---------------------------------------------------------------------------
# C2：UsageTracker
# ---------------------------------------------------------------------------


def test_tracker_appends_jsonl_and_summarizes(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")
    tracker.record("m", "turn", {"prompt_tokens": 10, "completion_tokens": 2,
                                 "cache_hit_tokens": 8, "cache_miss_tokens": 2}, ts="t1")
    tracker.record("m", "judge", {"prompt_tokens": 5, "completion_tokens": 1}, ts="t2")

    lines = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and lines[0]["purpose"] == "turn"
    summary = tracker.summarize()
    assert summary["m@turn"]["prompt"] == 10
    assert summary["m@judge"]["completion"] == 1


def test_cost_report_counts_tokens_and_price(tmp_path):
    tracker = UsageTracker(tmp_path / "unused.jsonl")
    tracker.record(
        "deepseek-v4-flash", "judge",
        {"prompt_tokens": 1_000_000, "completion_tokens": 100,
         "cache_hit_tokens": 900_000, "cache_miss_tokens": 100_000},
        ts="t",
    )
    report = tracker.cost_report()
    # 空闲成本 = 0.9M×0.05 + 0.1M×1.5 + 100×4.5 = 0.045+0.15+0.00045 ≈ 0.19545
    assert "¥0.195" in report
    assert "入 1,000,000" in report and "命中 900,000" in report
    assert "高峰时段" in report


def test_usage_fields_missing_is_none():
    assert usage_fields(SimpleNamespace()) is None
    resp = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1,
                              prompt_cache_hit_tokens=2, prompt_cache_miss_tokens=1)
    )
    assert usage_fields(resp) == {
        "prompt_tokens": 3, "completion_tokens": 1,
        "cache_hit_tokens": 2, "cache_miss_tokens": 1,
    }


# ---------------------------------------------------------------------------
# C2/C1：LLMClient 采集与路由
# ---------------------------------------------------------------------------


class _CallsClient:
    """记录 create 调用的极简客户端。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    @property
    def chat(self):
        return SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("响应耗尽")
        return self.responses.pop(0)


def _resp_with_usage(content: str, model: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=20,
            prompt_cache_hit_tokens=60, prompt_cache_miss_tokens=40,
        ),
    )


def test_complete_records_usage_with_purpose_and_model_routing(tmp_path):
    tracker = UsageTracker(tmp_path / "u.jsonl")
    client = _CallsClient([_resp_with_usage("通过", "judge-x")])
    llm = LLMClient(client, "main", [], models={"judge": "judge-x"}, tracker=tracker)
    out = llm.complete([{"role": "user", "content": "x"}], purpose="judge")
    assert out == "通过"
    assert client.calls[0]["model"] == "judge-x"  # C1 路由
    entry = tracker.entries[-1]
    assert entry["purpose"] == "judge" and entry["model"] == "judge-x"
    assert entry["cache_hit_tokens"] == 60 and entry["prompt_tokens"] == 100


def test_complete_falls_back_to_main_model():
    client = _CallsClient([_resp_with_usage("ok", "main")])
    llm = LLMClient(client, "main", [])
    llm.complete([{"role": "user", "content": "x"}])
    assert client.calls[0]["model"] == "main"


def test_run_turn_stream_captures_usage(tmp_path):
    """流式路径：末 chunk 携带 usage（stream_options include_usage）→ 落盘。"""
    tracker = UsageTracker(tmp_path / "u.jsonl")
    chunks = [
        SimpleNamespace(choices=[], usage=None),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    delta=SimpleNamespace(
                        content=None, reasoning_content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0, id="c1",
                                function=SimpleNamespace(
                                    name="submit_narration",
                                    arguments='{"narration": "流式叙事", "choices": ["甲","乙","丙"],'
                                    ' "plot_signal": "normal"}',
                                ),
                            )
                        ],
                    ),
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45,
                                  prompt_cache_hit_tokens=100, prompt_cache_miss_tokens=23),
        ),
    ]
    client = _CallsClient([iter(chunks)])
    pack = load_worldpack(PACK_PATH)
    llm = LLMClient(client, "main", build_tools(pack.schedule), tracker=tracker)
    result = llm.run_turn(
        [{"role": "user", "content": "玩家发言"}],
        apply_change=lambda a: "ok",
        on_text=lambda t: None,
    )
    assert result.narration == "流式叙事"
    assert client.calls[0]["stream_options"] == {"include_usage": True}
    entry = tracker.entries[-1]
    assert entry["purpose"] == "turn"
    assert entry["prompt_tokens"] == 123 and entry["cache_hit_tokens"] == 100


# ---------------------------------------------------------------------------
# C1：Settings 模型分层
# ---------------------------------------------------------------------------


def test_settings_model_for_falls_back():
    s = Settings(api_key="k", base_url="b", model="main")
    assert s.model_for("judge") == "main"
    assert s.model_for("compress") == "main"
    assert s.model_for("turn") == "main"


def test_settings_model_for_routes():
    s = Settings(api_key="k", base_url="b", model="main", judge_model="j", compress_model="c")
    assert s.model_for("judge") == "j"
    assert s.model_for("compress") == "c"
    assert s.model_for("turn") == "main"


def test_from_settings_wires_models_and_tracker(tmp_path):
    s = Settings(api_key="k", base_url="https://x", model="main", judge_model="j")
    tracker = UsageTracker(tmp_path / "u.jsonl")
    llm = LLMClient.from_settings(s, [], tracker=tracker)
    assert llm.model_for("judge") == "j"
    assert llm.model_for("compress") == "main"  # 空 → 回退主模型
    assert llm.tracker is tracker

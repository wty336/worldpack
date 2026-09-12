"""端点指纹（离线）：报告要能自证"这批数字是哪次部署产出的"。

背景（2026-09-12）：报告里只记 ``model=local-14b``，换量化/换后端/换 max_model_len
之后历史数字无处对账。修复：报告头加 ``endpoint`` 指纹（base_url / model /
model_root / max_model_len / served_by）。
纪律：指纹探测失败**不得中断实验**——只记 ``probe_error``。
"""

from __future__ import annotations

from game_agent import endpoint
from game_agent.config import Settings

VLLM_LISTING = [
    {
        "id": "local-14b",
        "served_by": "vllm",
        "model_root": "Qwen/Qwen2.5-14B-Instruct-AWQ",
        "max_model_len": 32768,
    }
]


def test_fingerprint_captures_deployment(monkeypatch):
    monkeypatch.setattr(endpoint, "_list_models", lambda *a, **k: VLLM_LISTING)

    fp = endpoint.fingerprint("http://127.0.0.1:8000/v1", "local-14b", "k")

    assert fp["base_url"] == "http://127.0.0.1:8000/v1"
    assert fp["model"] == "local-14b"
    assert fp["model_root"] == "Qwen/Qwen2.5-14B-Instruct-AWQ"
    assert fp["max_model_len"] == 32768
    assert fp["served_by"] == "vllm"
    assert "probe_error" not in fp


def test_probe_failure_does_not_raise(monkeypatch):
    def boom(*_a, **_k):
        raise TimeoutError("connect timeout")

    monkeypatch.setattr(endpoint, "_list_models", boom)

    fp = endpoint.fingerprint("http://127.0.0.1:8000/v1", "local-14b")

    assert fp["probe_error"] == "TimeoutError: connect timeout"
    assert fp["model"] == "local-14b"  # 至少留下配置层事实


def test_unknown_model_lists_available(monkeypatch):
    """名字不在列表 ≠ 探测失败：留给读者判断（别名/改名/写错）。"""
    monkeypatch.setattr(endpoint, "_list_models", lambda *a, **k: VLLM_LISTING)

    fp = endpoint.fingerprint("http://127.0.0.1:8000/v1", "local-70b")

    assert fp["not_listed"] is True
    assert fp["available"] == ["local-14b"]
    assert "probe_error" not in fp
    assert "model_root" not in fp  # 不得张冠李戴


def test_cloud_api_without_deployment_fields(monkeypatch):
    """云端 API 不认 root/max_model_len —— 缺字段就不写，不造 None。"""
    monkeypatch.setattr(
        endpoint,
        "_list_models",
        lambda *a, **k: [{"id": "deepseek-v4-flash", "served_by": "deepseek",
                          "model_root": None, "max_model_len": None}],
    )

    fp = endpoint.fingerprint("https://api.deepseek.com", "deepseek-v4-flash")

    assert fp == {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash",
                  "served_by": "deepseek"}


def test_provider_alias_recorded_as_not_listed(monkeypatch):
    """真实案例（2026-09-12）：配 deepseek-v4-flash，端点只列 deepseek-flash / deepseek-v4-pro，
    实测两次回显 model 均为 'deepseek-flash' —— 别名。指纹必须把这件事留痕。"""
    monkeypatch.setattr(
        endpoint,
        "_list_models",
        lambda *a, **k: [
            {"id": "deepseek-flash", "served_by": "deepseek", "model_root": None,
             "max_model_len": None},
            {"id": "deepseek-v4-pro", "served_by": "deepseek", "model_root": None,
             "max_model_len": None},
        ],
    )

    fp = endpoint.fingerprint("https://api.deepseek.com", "deepseek-v4-flash")

    assert fp["not_listed"] is True
    assert fp["available"] == ["deepseek-flash", "deepseek-v4-pro"]


def test_fingerprint_for_uses_routed_model(monkeypatch):
    """judge 走专属路由时，指纹必须记判官那个模型，不是主模型。"""
    monkeypatch.setattr(endpoint, "_list_models", lambda *a, **k: VLLM_LISTING)
    settings = Settings(
        api_key="k",
        base_url="http://127.0.0.1:8000/v1",
        model="deepseek-v4-flash",
        judge_model="local-14b",
    )

    assert endpoint.fingerprint_for(settings, "judge")["model"] == "local-14b"
    assert endpoint.fingerprint_for(settings, "compress")["model"] == "deepseek-v4-flash"

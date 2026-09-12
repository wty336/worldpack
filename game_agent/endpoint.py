"""端点指纹：把「这批数字是哪次部署产出的」钉进报告。

背景（2026-09-12）：报告里只记 ``model=local-14b``，换上更狠的量化、换 vLLM 版本、
换 ``max_model_len`` 之后，历史数字就再也分不清出处。本模块探测
``GET {base_url}/models``，取服务端**自报**的 ``root``（真实权重 repo）、
``max_model_len``、``owned_by``。

纪律：
- 指纹是**元数据**，探测失败绝不抛异常（报告照写，只带 ``probe_error``）；
- 只探一次、短超时（默认 5s）、不重试；
- ``model`` 以**实际生效**的模型为准（judge/compress 可走专属路由，见 ``Settings.model_for``）；
- ``probe_error``（探测失败）与 ``not_listed``（列表里没这个名字，如供应商别名
  `deepseek-v4-flash` → 服务端实为 `deepseek-flash`）是两件事，不得混为一谈。
"""

from __future__ import annotations

from typing import Any

PROBE_TIMEOUT_S = 5.0

# 端点上可能不带这些字段（如云端 OpenAI 兼容 API）——缺就不写，不造 None
_OPTIONAL_FIELDS = ("served_by", "model_root", "max_model_len")


def _list_models(base_url: str, api_key: str, timeout: float) -> list[dict[str, Any]]:
    """探测端点模型列表（测试替身点，勿在别处直接调用）。"""
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key or "EMPTY", timeout=timeout)
    return [
        {
            "id": getattr(m, "id", None),
            "served_by": getattr(m, "owned_by", None),
            "model_root": getattr(m, "root", None),
            "max_model_len": getattr(m, "max_model_len", None),
        }
        for m in client.models.list().data
    ]


def fingerprint(
    base_url: str,
    model: str,
    api_key: str = "",
    *,
    timeout: float = PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """返回可 JSON 序列化的端点指纹；探测失败时含 ``probe_error``。"""
    fp: dict[str, Any] = {"base_url": base_url, "model": model}
    try:
        models = _list_models(base_url, api_key, timeout)
    except Exception as exc:  # noqa: BLE001 —— 元数据探测不得中断实验
        fp["probe_error"] = f"{type(exc).__name__}: {exc}"
        return fp

    for m in models:
        if m.get("id") == model:
            for key in _OPTIONAL_FIELDS:
                if m.get(key) is not None:
                    fp[key] = m[key]
            return fp

    fp["not_listed"] = True
    fp["available"] = [m.get("id") for m in models][:10]
    return fp


def fingerprint_for(settings: Any, purpose: str = "judge") -> dict[str, Any]:
    """按 ``Settings`` 取**实际生效**的模型（judge/compress 走专属路由）再探测。"""
    return fingerprint(
        settings.base_url,
        settings.model_for(purpose),
        settings.api_key,
    )

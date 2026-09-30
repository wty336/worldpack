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


_DWINDOW_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def context_window_from_endpoint(
    settings: Any,
    purpose: str = "turn",
    *,
    timeout: float = PROBE_TIMEOUT_S,
) -> int:
    """端点自报的 ``max_model_len`` → 主回合模型的上下文窗（token）；未知返回 0。

    为什么值得自动取：本模块**已经在**探测 ``max_model_len``（8 个脚本用它做部署指纹），
    而"上下文窗多大"正是 J 系列溢出预检需要的同一个数——让使用者再手工配一份
    ``DEEPSEEK_CONTEXT_WINDOW`` 是重复真源，且换量化/换 ``max_model_len`` 后会漂
    （见 `tests/test_endpoint_fingerprint.py` 的文件头：当初就是为这件事做的指纹）。

    纪律：
    - **探测失败/字段缺失一律返回 0**（fail-soft）：云端 OpenAI 兼容端点不报这个字段
      （`test_cloud_api_without_deployment_fields` 已钉住"缺字段就不写"），
      此时调用方回落到显式配置或"预检关闭"，绝不猜一个窗口出来；
    - **缓存"端点答了什么"，不缓存"探测失败"**：Web 每次请求都会走 `_make_game`，
      探测带超时，不缓存会把网络往返放进每个请求的路径上。故分两种结果：
      ① 端点答了（带 `max_model_len`，或答了但没这个字段）→ 缓存，之后零成本；
      ② 探测抛错（超时/连不上）→ **不**缓存，下一次仍会试（端点稍后可达要能自愈）。
      实测：DeepSeek 云端属于 ①（答得快、但没有该字段），若不缓存会每回合白花约 0.5s。
    """
    base_url = str(getattr(settings, "base_url", "") or "")
    model = str(settings.model_for(purpose) if hasattr(settings, "model_for") else "") or ""
    if not base_url or not model:
        return 0
    key = (base_url, model)
    if key not in _DWINDOW_CACHE:
        fp = fingerprint(base_url, model, getattr(settings, "api_key", ""), timeout=timeout)
        # 只缓存"端点确实答了"的结果（含"答了但没这个字段"→ None）。
        # 探测失败（超时/连不上）不缓存：端点稍后可达时应能自愈。
        if "probe_error" not in fp:
            _DWINDOW_CACHE[key] = fp
    value = _DWINDOW_CACHE.get(key, {}).get("max_model_len")
    try:
        return int(value) if value else 0
    except (TypeError, ValueError):
        return 0

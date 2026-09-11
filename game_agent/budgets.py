"""LLM 调用预算的单一真源（侧信道预算统一）。

规则（M2a 复盘 #3）：任何 LLM 调用预算 ≥ ``MIN_CALL_TOKENS``。

踩坑（retro §5.3 / §8.2 实锤）：思考模式模型的推理链与正文共用同一个
``max_tokens``，预算过小时推理把预算吃光 → ``finish_reason=length`` +
``content`` 为空。空 = 未知，**不是**「通过 / 不重复 / 无洞察 / 无事实」，
而旧代码把空响应当结论静默放行（judge 假阴性、dedup 放行重复事实）。

统一策略：基础预算（≥ ``MIN_CALL_TOKENS``）+ 空响应升级重试一次
（``EMPTY_RETRY_TOKENS``；judge / reflect 实测 2000 足以让推理收敛并产出正文）。
侧信道调用一律走 :func:`complete_with_empty_retry`，不要各自写预算、
也不要各自决定空响应怎么办。
"""

from __future__ import annotations

from typing import Any

MIN_CALL_TOKENS = 500  # 规则：任何 LLM 调用预算 ≥ 500（M2a 复盘 #3）

# 基础预算（全部 ≥ MIN_CALL_TOKENS）
TURN_MAX_TOKENS = 2048  # 主回合：含思考链余量（重回合需要；截断会导致无工具调用）
COMPRESS_MAX_TOKENS = 2000  # 增量摘要合并（≤800 字目标，留足思考与重写空间）
JUDGE_MAX_TOKENS = 500  # 判定：通过 / 问题类型：描述
EXTRACT_MAX_TOKENS = 500  # 事实提炼：行式「重要性|事实」，≤5 条
DEDUP_MAX_TOKENS = 500  # 二值判定：重复 / 不重复（正文仅 2 字，预算留给思考链）
REFLECT_MAX_TOKENS = 500  # 洞察合成：≤2 行「洞察|来源编号」

# 空响应升级预算：必须严格大于所有基础预算，否则「升级」不成立（见 tests）
EMPTY_RETRY_TOKENS = 2000


def complete_with_empty_retry(
    llm: Any,
    messages: list[dict],
    *,
    purpose: str,
    max_tokens: int,
    temperature: float | None = None,
) -> str:
    """无工具补全；空响应时用升级预算重试一次。

    「空」按 ``strip()`` 判定（纯空白/换行也算空）。重试后仍空则原样返回，
    交给调用方按各自语义处理——但调用方不得再把空当作肯定结论。
    """
    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "purpose": purpose}
    if temperature is not None:
        kwargs["temperature"] = temperature
    text = llm.complete(messages, **kwargs)
    if text and text.strip():
        return text
    kwargs["max_tokens"] = max(EMPTY_RETRY_TOKENS, max_tokens)
    return llm.complete(messages, **kwargs) or ""

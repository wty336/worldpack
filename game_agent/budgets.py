"""LLM 调用预算的单一真源（侧信道预算统一）。

规则（M2a 复盘 #3）：任何 LLM 调用预算 ≥ ``MIN_CALL_TOKENS``。

踩坑（retro §5.3 / §8.2 实锤）：思考模式模型的推理链与正文共用同一个
``max_tokens``，预算过小时推理把预算吃光 → ``finish_reason=length`` +
``content`` 为空。空 = 未知，**不是**「通过 / 不重复 / 无洞察 / 无事实」，
而旧代码把空响应当结论静默放行（judge 假阴性、dedup 放行重复事实）。

统一策略：基础预算（≥ ``MIN_CALL_TOKENS``）+ **空响应或截断**时按
``retry_tokens_for()`` 升级重试一次。侧信道调用一律走
:func:`complete_checked` / :func:`complete_with_empty_retry`，
不要各自写预算、也不要各自决定空响应/截断怎么办。
"""

from __future__ import annotations

from typing import Any

MIN_CALL_TOKENS = 500  # 规则：任何 LLM 调用预算 ≥ 500（M2a 复盘 #3）

# 基础预算（全部 ≥ MIN_CALL_TOKENS）
TURN_MAX_TOKENS = 2048  # 主回合：含思考链余量（重回合需要；截断会导致无工具调用）
COMPRESS_MAX_TOKENS = 4000  # 增量摘要合并：实测 flash 自然结束落在 933~1803 token，
                            # 而 53% 的调用顶在 1999~2001（2026-09-11 取证）→ 2000 不够，
                            # 取自然上限的约 2 倍；截断摘要会替换历史前缀（= 静默丢内容）
JUDGE_MAX_TOKENS = 500  # 判定：通过 / 问题类型：描述
EXTRACT_MAX_TOKENS = 500  # 事实提炼：行式「重要性|事实」，≤5 条
DEDUP_MAX_TOKENS = 500  # 二值判定：重复 / 不重复（正文仅 2 字，预算留给思考链）
REFLECT_MAX_TOKENS = 500  # 洞察合成：≤2 行「洞察|来源编号」

# 空响应升级下限：小任务（基础预算 ≤1000）够用；大任务按 2× 走 retry_tokens_for()
EMPTY_RETRY_TOKENS = 2000

# 截断标记：正文被 max_tokens 砍断（推理链吃光预算），与空响应同属"不可信输出"
TRUNCATED_FINISH_REASON = "length"


def retry_tokens_for(max_tokens: int) -> int:
    """升级预算 = ``max(EMPTY_RETRY_TOKENS, 2 × 基础预算)``。

    判定类小任务（500）→ 2000；大任务（compress 4000）→ 8000。
    规则单点定义（测试直接断言本函数），避免各调用点写死升级值。
    """
    return max(EMPTY_RETRY_TOKENS, max_tokens * 2)


def complete_checked(
    llm: Any,
    messages: list[dict],
    *,
    purpose: str,
    max_tokens: int,
    temperature: float | None = None,
) -> tuple[str, str | None]:
    """无工具补全；**空响应或截断**时用升级预算重试一次。

    - 空：``strip()`` 后为空（纯空白/换行也算），推理链吃光了全部预算；
    - 截断：``finish_reason == "length"``——正文非空但被砍断，判定不可信
      （retro §8.2 的 reflect 半句洞察即此类）。

    返回 ``(文本, 最终一次调用的 finish_reason)``：调用方可据此识别
    「重试后仍被截断」（compress 场景：宁可放弃压缩，也不采纳被截断的摘要）。
    若重试仍空，则返回第一次的文本与结束原因：调用方按各自语义处理，
    但**不得再把空当作肯定结论**。
    """
    text, finish_reason = _complete(
        llm, messages, max_tokens=max_tokens, purpose=purpose, temperature=temperature
    )
    if text.strip() and finish_reason != TRUNCATED_FINISH_REASON:
        return text, finish_reason
    retry_text, retry_finish = _complete(
        llm,
        messages,
        max_tokens=retry_tokens_for(max_tokens),
        purpose=purpose,
        temperature=temperature,
    )
    if retry_text.strip():
        return retry_text, retry_finish
    return text, finish_reason


def complete_with_empty_retry(
    llm: Any,
    messages: list[dict],
    *,
    purpose: str,
    max_tokens: int,
    temperature: float | None = None,
) -> str:
    """``complete_checked`` 的文本版（不需要 finish_reason 的调用点用这个）。"""
    return complete_checked(
        llm, messages, purpose=purpose, max_tokens=max_tokens, temperature=temperature
    )[0]


def _complete(
    llm: Any,
    messages: list[dict],
    *,
    max_tokens: int,
    purpose: str,
    temperature: float | None,
) -> tuple[str, str | None]:
    """调用补全，返回 (文本, finish_reason)。

    优先走 ``complete_with_meta``（LLMClient 提供，带 finish_reason）；
    没有该方法的轻量替身退化为 ``complete``，finish_reason 视作未知（None）。
    """
    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "purpose": purpose}
    if temperature is not None:
        kwargs["temperature"] = temperature
    meta = getattr(llm, "complete_with_meta", None)
    if meta is None:
        return (llm.complete(messages, **kwargs) or ""), None
    result = meta(messages, **kwargs)
    return result.text, result.finish_reason

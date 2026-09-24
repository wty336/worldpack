"""agent-first 第 4 件：规划层 plan-and-execute 的侧信道（设计见 docs/design-planning.md）。

职责：节点进入且作者未手写 steps 时，把主线目标拆成 2~4 个顺序子步骤。
- 输入只含 goal / briefing / 场景——**不含 flags**（引擎真值纪律）；
- 失败/空/格式非法 → 返回 []（调用方静默降级 = 无计划 = 现状行为）；
- 步骤是"引导"不是"真值"：完成判定仍只认 completion 条件（代码复核）。
"""

from __future__ import annotations

import re

PLAN_MAX_TOKENS = 500  # 子步骤列表（≤4 行，每行 ≤40 字）——满足 budgets 最小预算纪律

PLAN_SYSTEM = (
    "你是主线剧情规划器。把给定的主线目标拆成 2~4 个**按执行顺序**排列的子步骤，"
    "每个子步骤是该目标达成路上的一次具体推进动作（打听消息/取得资格/完成任务等），"
    "彼此不重叠、顺序不可颠倒。只依据给定材料，不得编造材料外的设定。"
    "每行一条，格式：`序号. 子步骤`（≤40 字）；只输出步骤列表本身，不要解释。"
)


def parse_steps(output: str) -> list[str]:
    """解析规划输出为子步骤列表。容忍编号/项目符号/噪声行；1~4 条才有效，否则 []。

    - 超 4 条 → 截断到 4（模型话多不是错，前 4 条通常已够用）；
    - 空行/「无」哨兵/超 40 字行跳过；
    - 0 条 → []（调用方视为无计划）。
    """
    steps: list[str] = []
    for line in (output or "").splitlines():
        line = re.sub(r"^\s*[\d一二三四五六七八九十]+[.、)）:：]\s*", "", line).strip()
        line = line.lstrip("-*·•").strip()
        line = line.rstrip("。.！!，,；;：: ")
        if not line or line in ("无", "没有", "无法拆分"):
            continue
        if len(line) > 40:
            continue
        steps.append(line)
        if len(steps) >= 4:
            break
    if not steps:
        return []
    return steps

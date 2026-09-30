"""剧情摘要压缩（M2b）：近窗 + 增量摘要（design.md §4.5 的 v1）。

设计要点：
- 只在接近 token 阈值时批量压缩，不逐轮压（KV Cache 权衡，章 2）；
- **增量式**：每次压缩只总结"上次摘要之后的新增部分"并与旧摘要合并——
  避免把整个历史一次性塞进摘要调用（输入成本可控、摘要随剧情演进）；
- 切点落在**回合起点**（user 消息），保证 tool_calls/tool_result 配对不变量不被切断；
- 压缩只碰历史，不碰静态前缀与状态栏事实区块（M2a 复盘 §7.1 的硬约束）。
"""

from __future__ import annotations

import json

SUMMARY_MAX_TARGET = 800  # 摘要目标长度（字符，提示词指导）

COMPRESS_SYSTEM = (
    "你是剧情摘要器。把给定材料（旧摘要 + 新增对话历史）合并更新为一份结构化剧情摘要，"
    "供后续叙事使用。必须保留：\n"
    "① 人物关系与好感变化；\n"
    "② 关键选择、承诺与约定（保留原文细节：数字、名字、暗号、日期）；\n"
    "③ 已发生的重要事件与结果；\n"
    "④ 未完成的目标与线索；\n"
    "⑤ 失败尝试与原因。\n"
    "丢弃日常寒暄与重复内容。输出 Markdown 结构，总长控制在 {target} 字以内。"
)

SUMMARY_MARK = "【剧情摘要】"


def est_tokens(text: str) -> int:
    """粗估 token 数：中文 1 字 ≈ 1 token，取字符数作保守上界。"""
    return len(text)


def history_tokens(history: list[dict]) -> int:
    total = 0
    for m in history:
        total += est_tokens(str(m.get("content") or ""))
        calls = m.get("tool_calls") or []
        if calls:
            total += est_tokens(json.dumps(calls, ensure_ascii=False))
    return total


def find_turn_cut(history: list[dict], keep_turns: int) -> int:
    """返回压缩切点：切点之前进摘要，之后保留近窗。

    回合起点 = **无 name 标记的 user 消息**（A-2 口径：引擎元消息统一带
    name="engine"，玩家动作消息不带）。批次审查修复：原"不以【开头"启发式
    会被批次 A 新增的 [反重复提示]/[引擎熔断]（方括号开头）误判为玩家回合。
    返回 0 表示无需压缩（回合数不足）。
    """
    user_idx = [
        i
        for i, m in enumerate(history)
        if m.get("role") == "user" and not m.get("name")
    ]
    if len(user_idx) <= keep_turns:
        return 0
    return user_idx[-keep_turns]


def locate_summary(history: list[dict]) -> int:
    """返回最近一条摘要消息的索引，无则 -1。"""
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("role") == "user" and str(m.get("content") or "").startswith(SUMMARY_MARK):
            return i
    return -1


def summary_text(history: list[dict]) -> str:
    """当前压缩摘要的正文（无摘要返回空串）。设计加固 A1：摘要入事实图。"""
    idx = locate_summary(history)
    if idx < 0:
        return ""
    return str(history[idx].get("content") or "").replace(SUMMARY_MARK, "").strip()


def history_text(history: list[dict]) -> str:
    """把消息历史渲染为摘要器可读的纯文本（只保留内容，忽略协议细节）。"""
    lines = []
    for m in history:
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        role = m.get("role")
        if role == "user":
            lines.append(f"玩家：{content}")
        elif role == "assistant":
            lines.append(f"叙事：{content}")
    return "\n".join(lines)


def rebuild_history(
    history: list[dict], summary_idx: int, new_summary: str, cut: int
) -> list[dict]:
    """用新摘要重建历史：**摘要原位替换** + 丢弃被摘要覆盖的部分 + 保留近窗。

    为什么必须原位替换（而不是插在近窗前）——KV Cache 的字节前缀约束：

    旧实现返回 `[旧前缀] + [新摘要] + [近窗]`（摘要插在 prefix 之后）。而近窗消息
    在两次请求之间**逐字未变**，本应是缓存里最值钱的一段；插一条新消息进去，
    缓存匹配到插入点就断了，**从摘要起往后全部失效**。
    实测症状还包括摘要自身漂移：摘要最初在 index 0，第二次压缩起 index 变成 1、3……
    且条数按 1→2→4 累积（旧摘要从未被删除，只是被推到前缀里）。

    正确形态：
    - `history[:summary_idx]` 逐字保留（前缀继续命中缓存）；
    - 已有的摘要条目**被替换**（不是被保留）；
    - `history[cut:]` 逐字保留（近窗不动，缓存可复用）；
    - 其余被摘要覆盖的消息丢弃 —— 这才是压缩的目的。
    """
    prefix = history[:summary_idx] if summary_idx > 0 else []
    summary_msg = {
        "role": "user",
        "name": "engine",  # A-2：剧情摘要是引擎元消息，排除出检索上下文
        "origin": "engine",  # 血缘标记：本条由引擎注入，非模型/玩家产出
        "content": f"{SUMMARY_MARK}\n{new_summary}",
    }
    # 关键：切点之后的区间里可能**残留着旧摘要**（摘要若在切点之前，它会被前缀或
    # 被丢弃覆盖；但一旦 history 里已有摘要且 cut 落在它之后，不排除就会累积成
    # 多条摘要 —— 实测按 1→2→4 增长）。故按内容标记剔除所有既有摘要消息。
    tail = [
        m for m in history[cut:] if not str(m.get("content") or "").startswith(SUMMARY_MARK)
    ]
    return [*prefix, summary_msg, *tail]


def ensure_pairing(history: list[dict]) -> bool:
    """校验 tool_calls / tool_result 配对不变量（压缩后必须成立）。"""
    i = 0
    while i < len(history):
        m = history[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            j = i + 1
            while j < len(history) and history[j].get("role") == "tool":
                ids.discard(history[j].get("tool_call_id"))
                j += 1
            if ids:
                return False
        i += 1
    return True

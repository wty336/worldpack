"""Lore 键匹配原语（A-3）：正则键、大小写、命中评分与四值逻辑。

独立成模块的原因：`worldpack`（加载期校验正则）与 `context`（运行时匹配）都需要
同一套判定，而 `context` 已 import `worldpack`——把它放在任一侧都会造成循环导入。
这里只放**不依赖任何项目模型的纯函数**，两侧共用即天然一致。

A-3（对照 SillyTavern WorldInfoBuffer.matchKeys）：键写成 `/pattern/` 即按正则匹配，
且**正则优先于一切字面量选项**（SillyTavern `world-info.js:337-342`）。
大小写由 `case_sensitive` 逐条控制（SillyTavern `:268-271`，条目级不设则继承全局）。

本模块**不实现 `matchWholeWords`**：SillyTavern 用 `(?:^|\\W)(key)(?:$|\\W)` 兜标点边界，
但 `\\W` 对中文按 Unicode 判定——每个汉字都是 `\\w`，词边界规则在中文语境下不成立。
中文字面量本就无边界分歧，故不引入这个只对英文有效、却会让中文作者困惑的开关。
"""

from __future__ import annotations

import re

# A-3：次级关键词四值逻辑（语义与 SillyTavern `world_info_logic` 对齐）
LORE_LOGIC_MODES = ("AND_ANY", "AND_ALL", "NOT_ANY", "NOT_ALL")


class LoreKeyError(ValueError):
    """键本身非法（例如 `/…/` 里的正则编译不过）。"""


def is_regex_key(key: str) -> bool:
    """`/…/` 成对即正则；单个斜杠是普通字面量（避免误伤含斜杠的词）。"""
    return len(key) >= 2 and key.startswith("/") and key.endswith("/")


def compile_regex_key(key: str, *, case_sensitive: bool = True) -> re.Pattern[str]:
    """把 `/…/` 编译为正则；非法或空模式抛 `LoreKeyError`。"""
    if not is_regex_key(key):
        raise LoreKeyError(f"不是正则键: {key!r}")
    pattern = key[1:-1]
    if not pattern:
        raise LoreKeyError("正则键的模式不能为空: //")
    try:
        return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise LoreKeyError(f"正则键非法 {key!r}: {exc}") from exc


def validate_key(key: str, *, kind: str, owner: str, case_sensitive: bool = True) -> None:
    """加载期校验一个键：非空，且 `/…/` 形式必须能编译。"""
    if not key.strip():
        raise LoreKeyError(f"{kind} 中的键不能为空（{owner}）")
    if is_regex_key(key):
        try:
            compile_regex_key(key, case_sensitive=case_sensitive)
        except LoreKeyError:
            raise
    elif key.strip() == "//":
        raise LoreKeyError(f"{kind} 中的键不能是空正则 `//`（{owner}）")


def key_matches(key: str, context: str, *, case_sensitive: bool = True) -> bool:
    """单个键是否命中上下文。`/…/` 走正则，其余走字面量子串。"""
    if not key:
        return False
    if is_regex_key(key):
        try:
            return compile_regex_key(key, case_sensitive=case_sensitive).search(context) is not None
        except LoreKeyError:
            return False  # 非法正则由加载期拦截；运行时保守不命中
    if case_sensitive:
        return key in context
    return key.lower() in context.lower()


def match_score(
    keys: list[str],
    secondary_keys: list[str],
    logic: str,
    context: str,
    *,
    case_sensitive: bool = True,
) -> int:
    """A-2/A-3：命中评分；0 = 不命中。

    评分 = 主键命中数 + 次键命中数，用于排序（评分高者先注入）。
    四值语义：
    - `AND_ANY`：主键命中 且 次键至少一条命中；
    - `AND_ALL`：主键命中 且 次键全部命中；
    - `NOT_ANY`：主键命中 且 次键一条都没命中（命中 A 但没提到 B 才注入）；
    - `NOT_ALL`：主键命中 且 次键至少一条未命中。
    次键为空时不参与判定，退化为纯主键命中（此时 `logic` 无意义）。
    """
    primary = sum(1 for k in keys if key_matches(k, context, case_sensitive=case_sensitive))
    if primary == 0:
        return 0
    if not secondary_keys:
        return primary
    secondary = sum(
        1 for k in secondary_keys if key_matches(k, context, case_sensitive=case_sensitive)
    )
    total = len(secondary_keys)
    if logic == "AND_ANY":
        return primary + secondary if secondary > 0 else 0
    if logic == "AND_ALL":
        return primary + secondary if secondary == total else 0
    if logic == "NOT_ANY":
        return primary if secondary == 0 else 0
    if logic == "NOT_ALL":
        return primary + (total - secondary) if secondary < total else 0
    return 0  # 非法 logic 由加载期拦截，此处保守不注入

"""条件表达式求值器（W3）：when DSL，供节点触发/事件/结局/节点完成共用。

语法（design.md §5.4）：
  {all: [条件, ...]} / {any: [条件, ...]} / {not: 条件}
  {day: {gte: 5, lte: 10}}              # 游戏内日期比较
  {flags: {名字: true|false}}           # 剧情旗标相等
  {stat: {属性名: {gte: 30}}}           # 玩家属性比较
  {affection: {NPCid: {gte: 30}}}       # 好感比较
  {} 或 {all: []}                       # 恒真

原则：非法结构/未声明引用一律抛错，绝不让拼写错误静默通过（design.md §10 约束 C3）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import GameState

_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
_KEYS = frozenset({"all", "any", "not", "day", "flags", "stat", "affection"})


class ConditionError(Exception):
    """条件表达式结构非法（加载期校验 / 求值期兜底）。"""


# ---------------------------------------------------------------------------
# 加载期结构校验（由 worldpack 交叉校验调用）
# ---------------------------------------------------------------------------


def validate_condition(cond: Any, where: str = "condition") -> None:
    if not isinstance(cond, dict):
        raise ConditionError(f"{where}: 条件必须是映射，当前为 {cond!r}")
    for key, value in cond.items():
        if key in ("all", "any"):
            if not isinstance(value, list):
                raise ConditionError(f"{where}: '{key}' 的值必须是列表")
            for i, sub in enumerate(value):
                validate_condition(sub, f"{where}.{key}[{i}]")
        elif key == "not":
            validate_condition(value, f"{where}.not")
        elif key == "day":
            _validate_comparisons(value, f"{where}.day")
        elif key == "flags":
            if not isinstance(value, dict) or not all(
                isinstance(v, bool) for v in value.values()
            ):
                raise ConditionError(f"{where}.flags: 必须是 {{flag: true/false}} 映射")
        elif key in ("stat", "affection"):
            if not isinstance(value, dict):
                raise ConditionError(f"{where}.{key}: 必须是 {{名称: {{操作: 数值}}}} 映射")
            for name, ops in value.items():
                _validate_comparisons(ops, f"{where}.{key}.{name}")
        else:
            raise ConditionError(
                f"{where}: 未知条件键 '{key}'（可用: {', '.join(sorted(_KEYS))}）"
            )


def _validate_comparisons(ops: Any, where: str) -> None:
    if not isinstance(ops, dict) or not ops:
        raise ConditionError(f"{where}: 必须是 {{操作: 数值}} 映射，如 {{gte: 5}}")
    for op, val in ops.items():
        if op not in _OPS:
            raise ConditionError(
                f"{where}: 未知操作 '{op}'（可用: {', '.join(sorted(_OPS))}）"
            )
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ConditionError(f"{where}.{op}: 值必须是数字，当前为 {val!r}")


# ---------------------------------------------------------------------------
# 求值
# ---------------------------------------------------------------------------


def _cmp(a: float, op: str, b: float) -> bool:
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if op == "gt":
        return a > b
    if op == "gte":
        return a >= b
    if op == "lt":
        return a < b
    if op == "lte":
        return a <= b
    raise ConditionError(f"未知操作 '{op}'")  # 理论上被 validate 拦截，兜底


def _compare_all(actual: float, ops: dict[str, Any]) -> bool:
    return all(_cmp(actual, op, val) for op, val in ops.items())


def evaluate(cond: Any, state: "GameState") -> bool:
    if not isinstance(cond, dict):
        raise ConditionError(f"条件必须是映射，当前为 {cond!r}")
    if not cond:  # 空条件 = 恒真
        return True
    for key, value in cond.items():
        if key == "all":
            if not all(evaluate(sub, state) for sub in value):
                return False
        elif key == "any":
            if not any(evaluate(sub, state) for sub in value):
                return False
        elif key == "not":
            if evaluate(value, state):
                return False
        elif key == "day":
            if not _compare_all(float(state.day), value):
                return False
        elif key == "flags":
            for name, want in value.items():
                if name not in state.flags:
                    raise ConditionError(f"条件引用了未声明的 flag '{name}'")
                if state.flags[name] != want:
                    return False
        elif key == "stat":
            for name, ops in value.items():
                actual = state.stats.get(name)
                if actual is None:
                    raise ConditionError(f"条件引用了未声明的属性 '{name}'")
                if not _compare_all(actual, ops):
                    return False
        elif key == "affection":
            for npc_id, ops in value.items():
                actual = state.affections.get(npc_id)
                if actual is None:
                    raise ConditionError(f"条件引用了未声明的好感对象 '{npc_id}'")
                if not _compare_all(actual, ops):
                    return False
        else:
            raise ConditionError(f"未知条件键 '{key}'")
    return True

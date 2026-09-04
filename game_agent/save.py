"""存档读档（W7）：GameState + 对话历史 中立格式落盘（design.md §11）。

M1.5 起存档包含对话历史（history）：读档后 NPC 完整记得之前的对话，剧情连续。
M2b 完成压缩后，将升级为「近窗历史 + 剧情摘要」的更优方案（存档体积与上下文成本可控）。
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import GameState


def save_game(state: GameState, path: str | Path, history: list[dict] | None = None) -> None:
    """存档。history 为会话消息列表（纯 JSON 可序列化），可选但强烈建议传入。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = state.to_dict()
    if history is not None:
        data["history"] = history
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_game(path: str | Path) -> GameState:
    """读档：只恢复 GameState（history 字段由 load_history 单独读取）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return GameState.from_dict(data)


def load_history(path: str | Path) -> list[dict]:
    """读取存档中的对话历史；旧版存档（无 history 字段）返回空列表。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data.get("history", []))

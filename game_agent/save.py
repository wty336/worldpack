"""存档读档（W7）：GameState 中立格式落盘（design.md §11）。"""

from __future__ import annotations

import json
from pathlib import Path

from .state import GameState


def save_game(state: GameState, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_game(path: str | Path) -> GameState:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return GameState.from_dict(data)

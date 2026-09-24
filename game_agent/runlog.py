"""runtime 平台化 ②：Run checkpoint + Replay（docs/plan-runtime-platform.md §2）。

职责：把"一局游戏"变成可回放、可对比的实验对象（runlog = 回合级状态线，
与 trace 的调用级事件流分工——replay 报告用 run_id/turn 关联两者）。

- 引擎主循环**零改动**：RunRecorder 由驱动脚本在每次玩家动作后调用；
- 默认关闭：无驱动调用即无开销（实验工具定位，不是生产存档——生产仍走 save/autosave）；
- checkpoint = 每回合全量 state.to_dict() + history（长局 10MB 级/局，接受，
  见 plan §2.2 成本口径）；
- 中立格式：state 序列化复用 GameState.to_dict（与存档同源，跨模型可读）。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

RUNS_DIR = Path("runs")  # 实验产物目录（runlog + checkpoints）


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


class RunRecorder:
    """一局实验的回合级记录器：runlog.jsonl + checkpoints/<turn>.json。"""

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.runlog_path = self.run_dir / "runlog.jsonl"
        self.checkpoint_dir = self.run_dir / "checkpoints"
        self.run_id = self.run_dir.name

    @classmethod
    def create(cls, runs_dir: str | Path = RUNS_DIR, run_id: str | None = None) -> "RunRecorder":
        run_id = run_id or new_run_id()
        rec = cls(Path(runs_dir) / run_id)
        rec.run_dir.mkdir(parents=True, exist_ok=False)
        rec.checkpoint_dir.mkdir()
        return rec

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def checkpoint(
        self,
        turn: int,
        state,
        history: list[dict],
        action: dict | None,
        outcome: dict | None,
    ) -> None:
        """记录一个回合：checkpoint = 回合结束后的全量状态 + 历史；runlog 一行 =
        动作 + 结果 + checkpoint 引用。``action`` = 玩家动作
        {kind: say/act/pick/start, payload}；``outcome`` = 回合结果摘要。
        """
        cp_path = self.checkpoint_dir / f"{turn:06d}.json"
        cp_path.write_text(
            json.dumps({"state": state.to_dict(), "history": history}, ensure_ascii=False),
            encoding="utf-8",
        )
        entry = {
            "run_id": self.run_id,
            "turn": turn,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": action,
            "outcome": outcome,
            "checkpoint": str(cp_path.name),
        }
        with open(self.runlog_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def entries(self) -> list[dict]:
        if not self.runlog_path.exists():
            return []
        return [
            json.loads(line) for line in self.runlog_path.read_text(encoding="utf-8").splitlines()
        ]

    def load_checkpoint(self, turn: int) -> dict:
        """加载第 turn 回合结束后的 checkpoint：{state_dict, history}。"""
        cp = self.checkpoint_dir / f"{turn:06d}.json"
        return json.loads(cp.read_text(encoding="utf-8"))


def apply_prompt_patch(patch_path: str | Path, engine_rules: str) -> str:
    """把 yaml 补丁应用到引擎规则文本（{replace: [{old, new}, ...]}）。

    替换前逐条校验 old 恰好出现一次（防静默改错位置）；无命中 → 报错。
    """
    import yaml

    data = yaml.safe_load(Path(patch_path).read_text(encoding="utf-8")) or {}
    text = engine_rules
    for i, item in enumerate(data.get("replace", []), start=1):
        old, new = str(item["old"]), str(item["new"])
        if text.count(old) != 1:
            raise ValueError(
                f"补丁第 {i} 条的 old 在规则文本中出现 {text.count(old)} 次（要求恰好 1 次）: {old[:40]}"
            )
        text = text.replace(old, new)
    return text


def rebuild_game(pack, state_dict: dict, history: list[dict], llm):
    """从 checkpoint 重建 Game（state/history 原位还原；turn_count 等随 state 走）。"""
    from game_agent.game import Game
    from game_agent.state import GameState

    state = GameState.from_dict(state_dict)
    game = Game(pack, state, llm)
    game.history = list(history)
    return game


def reset_runs_dir(runs_dir: str | Path = RUNS_DIR) -> None:
    """清空实验产物目录（record_run --fresh 用）。"""
    p = Path(runs_dir)
    if p.exists():
        shutil.rmtree(p)

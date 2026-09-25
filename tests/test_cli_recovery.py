"""玩家反馈修复的守卫测试：CLI 规则性错误（GameError）恢复路径（离线）。

契约：
- 关键抉择期误入行动阶段（end_day / act 抛 GameError）→ 恢复**固定选项视图**
  （把玩家带回模态选择），不走崩溃存档；
- `_handle_command` 的 /end 分支同口径：`out["view"]` 带回模态选择视图；
- 无待决选择时 → 保守视图（仅自由输入入口）。
"""

from __future__ import annotations

from types import SimpleNamespace

import game_agent.cli as cli
from game_agent.cli import _action_phase, _handle_command, _recover_view
from game_agent.game import GameError
from game_agent.storyline import FREE_INPUT_OPTION


class _LockedGame:
    """假游戏：关键抉择待决 + 行动/结束今天一律抛 GameError。"""

    def __init__(self, pending: bool = True, actions: bool = True):
        self.state = SimpleNamespace(day=3, action_points_left=2)
        self._pending = pending
        self._actions = actions
        self.story = self._Story(self)

    class _Story:
        def __init__(self, game):
            self._game = game

        def pending_choice(self, state):
            if not self._game._pending:
                return None
            return SimpleNamespace(
                id="c1", prompt="如何抉择？",
                options=[SimpleNamespace(text="选项一"), SimpleNamespace(text="选项二")],
            )

    def end_day(self):
        raise GameError("此刻是关键抉择，只能从固定选项中选择")

    def act(self, action_id):
        raise GameError("此刻是关键抉择，只能从固定选项中选择")

    def actions_available(self):
        if not self._actions:
            return []
        return [SimpleNamespace(id="a1", label="修炼")]


def test_recover_view_pending_choice():
    view = _recover_view(_LockedGame(pending=True))
    assert view.choice_prompt is not None
    assert view.choices == ["选项一", "选项二"]


def test_recover_view_no_pending_choice():
    view = _recover_view(_LockedGame(pending=False))
    assert view.choice_prompt is None
    assert view.choices == [FREE_INPUT_OPTION]


def test_handle_command_end_recovers_to_choice_view(capsys):
    game = _LockedGame()
    out: dict = {}
    marker = _handle_command(game, "/end", out)
    assert marker == "view"
    assert out["view"].choice_prompt is not None  # 恢复模态选择视图
    assert "关键抉择" in capsys.readouterr().out  # 友好提示而非崩溃存档


def test_action_phase_act_recovers(monkeypatch):
    monkeypatch.setattr(cli, "_ask_number", lambda n: 1)
    view = _action_phase(_LockedGame(actions=True))
    assert view.choice_prompt is not None


def test_action_phase_end_day_recovers():
    view = _action_phase(_LockedGame(actions=False))
    assert view.choice_prompt is not None

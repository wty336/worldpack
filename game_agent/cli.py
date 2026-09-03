"""CLI 前端（M1）：世界包校验 + 可玩的 REPL 游戏循环。

命令：
  python -m game_agent check-worldpack [path]   校验并加载一个世界包（离线）
  python -m game_agent play [path]              开始游戏（需 .env 配置 DEEPSEEK_API_KEY）
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_settings
from .game import Game
from .llm import LLMClient, build_tools, make_client
from .save import load_game, save_game
from .state import GameState
from .worldpack import WorldPackError, load_worldpack

DEFAULT_WORLDPACK = "world-packs/ancient_jianghu"
DEFAULT_SAVE = "saves/save.json"


def _cmd_check_worldpack(args: argparse.Namespace) -> int:
    root = Path(args.path)
    try:
        pack = load_worldpack(root)
    except WorldPackError as e:
        print(f"[✗] 世界包加载失败: {e}")
        return 1

    s = pack.schedule
    print(f"[✓] 世界包 '{pack.world.name}' 加载成功")
    print(f"  背景: {pack.world.era}")
    print(
        "  属性: "
        + ", ".join(f"{k}({v.label}, 初始 {v.initial})" for k, v in s.stats.items())
    )
    print(
        "  好感对象: "
        + ", ".join(f"{k}({v.label}, 初始 {v.initial})" for k, v in s.affections.items())
    )
    print(f"  日程行动: " + ", ".join(a.label for a in s.actions))
    print(f"  主线节点: " + ", ".join(n.title for n in pack.mainline.nodes))
    print(f"  事件: {len(pack.events.events)} 个 · 结局: {len(pack.endings.endings)} 个")
    print(f"  NPC: " + ", ".join(n.name for n in pack.npcs.values()))
    print(f"  flag 声明: " + ", ".join(sorted(s.flags)) or "（无）")
    return 0


# ---------------------------------------------------------------------------
# play：REPL 游戏循环
# ---------------------------------------------------------------------------


def _cmd_play(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY：请复制 .env.example 为 .env 并填入 key")
        return 1
    try:
        pack = load_worldpack(args.path or DEFAULT_WORLDPACK)
    except WorldPackError as e:
        print(f"[✗] 世界包加载失败: {e}")
        return 1
    state = GameState.from_pack(pack)
    llm = LLMClient(make_client(settings), settings.model, build_tools(pack.schedule))
    game = Game(pack, state, llm)
    return _repl(game)


def _repl(game: Game) -> int:
    print(f"===== 《{game.pack.world.name}》 =====")
    print(
        "命令：/help 帮助 · /status 状态 · /actions 今日行动 · /save [/load] 存档读档 "
        "· /end 结束今天 · /quit 退出"
    )
    view = game.start()
    while True:
        if view.choice_prompt is not None:
            print(f"\n【关键抉择】{view.choice_prompt.prompt}")
            for i, text in enumerate(view.choices, 1):
                print(f"  {i}. {text}")
            n = _ask_number(len(view.choices))
            view = game.pick(n - 1)
            continue

        if view.ending is not None:
            print(f"\n『{view.ending.title}』")
            print(view.ending.text)
            print("（游戏结束。输入 /load 读档重来，或 /quit 退出）")
            while True:
                raw = input("> ").strip()
                if raw.startswith("/load"):
                    path = raw.partition(" ")[2].strip() or DEFAULT_SAVE
                    try:
                        game.state = load_game(path)
                    except (FileNotFoundError, ValueError) as e:
                        print(f"[✗] 读档失败: {e}")
                        continue
                    game.history = []
                    game.ending = None
                    print(f"已读档 ← {path}")
                    view = _action_phase(game)
                    break
                if raw.startswith("/quit"):
                    return 0
                print("结局后仅支持 /load 或 /quit")
            continue

        if view.narration:
            print("\n" + view.narration)
        print("\n你可以：")
        for i, c in enumerate(view.choices, 1):
            print(f"  {i}. {c}")
        print(
            f"（输入 1~{len(view.choices)} 选一项；选 {len(view.choices)} 或直接输入文字 = 自由行动；"
            "命令以 / 开头）"
        )
        while True:
            raw = input("> ").strip()
            if not raw:
                continue
            if raw.startswith("/"):
                marker = _handle_command(game, raw)
                if marker == "action":
                    view = _action_phase(game)
                    break
                if marker == "quit":
                    return 0
                continue
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx < len(view.choices):
                    view = game.say(view.choices[idx - 1])
                    break
                if idx == len(view.choices):  # 自由输入入口
                    text = input("说些什么 > ").strip()
                    if not text:
                        continue
                    view = game.say(text)
                    break
                print(f"请输入 1~{len(view.choices)} 的数字")
                continue
            view = game.say(raw)
            break


def _action_phase(game: Game):
    actions = game.actions_available()
    if not actions:
        game.end_day()
        print(f"—— 第 {game.state.day} 天 ——")
        actions = game.actions_available()
    print(f"\n—— 第 {game.state.day} 天 —— 今日行动（行动点 {game.state.action_points_left}）：")
    for i, a in enumerate(actions, 1):
        print(f"  {i}. {a.label}")
    n = _ask_number(len(actions))
    return game.act(actions[n - 1].id)


def _handle_command(game: Game, raw: str) -> str | None:
    """处理 / 命令。返回 'action'（进入行动阶段）、'quit' 或 None（继续对话循环）。"""
    cmd, _, arg = raw.partition(" ")
    cmd = cmd.strip()
    if cmd == "/help":
        print(
            "命令：/help · /status 状态 · /actions 今日行动 · /save [路径] · "
            "/load [路径] · /end 结束今天 · /quit 退出\n"
            "日常输入：数字 = 选择选项；直接打字 = 自由行动"
        )
    elif cmd == "/status":
        print("\n" + game.status_text())
    elif cmd == "/actions":
        actions = game.actions_available()
        print(f"今日行动（行动点 {game.state.action_points_left}）：")
        for i, a in enumerate(actions, 1):
            print(f"  {i}. {a.label}")
    elif cmd == "/save":
        path = arg.strip() or DEFAULT_SAVE
        save_game(game.state, path)
        print(f"已存档 → {path}")
    elif cmd == "/load":
        path = arg.strip() or DEFAULT_SAVE
        try:
            game.state = load_game(path)
        except (FileNotFoundError, ValueError) as e:
            print(f"[✗] 读档失败: {e}")
            return None
        game.history = []
        game.ending = None
        print(f"已读档 ← {path}")
        return "action"
    elif cmd == "/end":
        game.end_day()
        print(f"—— 第 {game.state.day} 天 ——")
        return "action"
    elif cmd == "/quit":
        return "quit"
    else:
        print(f"未知命令 {cmd}，输入 /help 查看")
    return None


def _ask_number(max_n: int) -> int:
    while True:
        raw = input(f"请输入 1~{max_n}: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= max_n:
            return int(raw)
        print(f"请输入 1~{max_n} 的数字")


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="game-agent", description="文字对话养成游戏引擎")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check-worldpack", help="校验并加载一个世界包")
    p_check.add_argument("path", nargs="?", default=DEFAULT_WORLDPACK)

    p_play = sub.add_parser("play", help="开始游戏（需 API Key）")
    p_play.add_argument("path", nargs="?", default=DEFAULT_WORLDPACK)

    args = parser.parse_args(argv)
    if args.command == "check-worldpack":
        return _cmd_check_worldpack(args)
    if args.command == "play":
        return _cmd_play(args)
    parser.print_help()
    return 0

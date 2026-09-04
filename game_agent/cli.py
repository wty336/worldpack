"""CLI 前端（M1）：世界包校验 + 可玩的 REPL 游戏循环。

命令：
  python -m game_agent check-worldpack [path]   校验并加载一个世界包（离线）
  python -m game_agent play [path]              开始游戏（需 .env 配置 DEEPSEEK_API_KEY）
"""

from __future__ import annotations

import argparse
import traceback
from pathlib import Path

from .config import load_settings
from .game import Game
from .llm import LLMClient, LLMTurnError, build_tools, make_client
from .save import load_game, load_history, save_game
from .state import GameState
from .worldpack import WorldPackError, load_worldpack

DEFAULT_WORLDPACK = "world-packs/ancient_jianghu"
DEFAULT_SAVE = "saves/save.json"
AUTOSAVE = "saves/autosave.json"  # W-C：节点完成自动存档


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
    # W-C：节点完成自动存档（引擎侧钩子）
    game = Game(pack, state, llm, autosave_path=AUTOSAVE)
    # 流式显示：内容增量实时输出（修复"等很久才有反应"的体验）
    game.on_text = _make_stream_display(game)
    return _repl(game)


def _make_stream_display(game: Game):
    state = {"begun": False}

    def on_text(piece: str) -> None:
        if not state["begun"]:
            state["begun"] = True
            print()  # 叙事开始，另起一行
        print(piece, end="", flush=True)
        game.last_streamed += piece

    return on_text


def _show_narration(game: Game, view) -> None:
    """显示本轮叙事：流式已显示的内容不再重复打印（去重）。"""
    streamed = game.last_streamed.strip()
    game.last_streamed = ""
    if view.narration and not (streamed and streamed in view.narration):
        print("\n" + view.narration)
    elif streamed:
        print()  # 流式已显示完毕，补一个换行


def _crash_save(game: Game, reason: str) -> None:
    """崩溃兜底：保存进度 + 错误日志，优雅退出而非裸 traceback。"""
    print(f"\n[!] 游戏中断：{reason}")
    try:
        save_game(game.state, "saves/crash.json", game.history)
        print(
            "已保存进度 → saves/crash.json（数值、剧情状态与对话历史均已保留；"
            "重新运行后 /load 可无缝继续）"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        Path("saves").mkdir(exist_ok=True)
        with open("saves/error.log", "a", encoding="utf-8") as f:
            f.write(reason + "\n" + traceback.format_exc() + "\n" + "-" * 60 + "\n")
    except Exception:  # noqa: BLE001
        pass


def _repl(game: Game) -> int:
    print(f"===== 《{game.pack.world.name}》 =====")
    if game.pack.world.opening:
        print("\n" + game.pack.world.opening)  # 开场背景介绍（修复"上来就是选项"）
    print(
        "\n命令：/help 帮助 · /status 状态 · /actions 今日行动 · /save [/load] 存档读档 "
        "· /new 重新开始 · /end 结束今天 · /quit 退出"
    )
    try:
        view = game.start()
        while True:
            if view.choice_prompt is not None:
                if view.briefing:  # 关键抉择前的剧情背景
                    print(f"\n【剧情】{view.briefing}")
                print(f"\n【关键抉择】{view.choice_prompt.prompt}")
                for i, text in enumerate(view.choices, 1):
                    print(f"  {i}. {text}")
                n = _ask_number(len(view.choices))
                print("（生成中…）")
                view = game.pick(n - 1)
                continue

            if view.ending is not None:
                print(f"\n『{view.ending.title}』")
                print(view.ending.text)
                print("（游戏结束。输入 /load 读档重来、/new 重新开始，或 /quit 退出）")
                while True:
                    raw = input("> ").strip()
                    if raw.startswith("/load"):
                        path = raw.partition(" ")[2].strip() or DEFAULT_SAVE
                        try:
                            game.state = load_game(path)
                            game.history = load_history(path)  # 恢复对话历史，NPC 不失忆
                        except (FileNotFoundError, ValueError) as e:
                            print(f"[✗] 读档失败: {e}")
                            continue
                        game.ending = None
                        print(f"已读档 ← {path}（含对话历史）")
                        view = _action_phase(game)
                        break
                    if raw.startswith("/new"):
                        game.state = GameState.from_pack(game.pack)
                        game.history = []
                        game.ending = None
                        game.last_choices = []
                        print("重新开始。")
                        view = game.start()
                        break
                    if raw.startswith("/quit"):
                        return 0
                    print("结局后仅支持 /load、/new 或 /quit")
                continue

            _show_narration(game, view)
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
                    if marker == "new":
                        view = game.start()  # 已重置状态，重新开场
                        break
                    if marker == "quit":
                        return 0
                    continue
                if raw.isdigit():
                    idx = int(raw)
                    if 1 <= idx < len(view.choices):
                        print("（生成中…）")
                        view = game.say(view.choices[idx - 1])
                        break
                    if idx == len(view.choices):  # 自由输入入口
                        text = input("说些什么 > ").strip()
                        if not text:
                            continue
                        print("（生成中…）")
                        view = game.say(text)
                        break
                    print(f"请输入 1~{len(view.choices)} 的数字")
                    continue
                print("（生成中…）")
                view = game.say(raw)
                break
    except EOFError:
        print("\n（再见）")
        return 0
    except KeyboardInterrupt:
        print("\n（再见）")
        return 0
    except LLMTurnError as e:
        _crash_save(game, f"生成失败（协议熔断）: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        _crash_save(game, f"{type(e).__name__}: {e}")
        return 1


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
    print("（生成中…）")
    return game.act(actions[n - 1].id)


def _handle_command(game: Game, raw: str) -> str | None:
    """处理 / 命令。返回 'action'（进入行动阶段）、'quit' 或 None（继续对话循环）。"""
    cmd, _, arg = raw.partition(" ")
    cmd = cmd.strip()
    if cmd == "/help":
        print(
            "命令：/help · /status 状态 · /actions 今日行动 · /save [路径] · "
            "/load [路径] · /new 重新开始 · /end 结束今天 · /quit 退出\n"
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
        save_game(game.state, path, game.history)
        print(f"已存档 → {path}（数值、剧情状态与对话历史）")
    elif cmd == "/load":
        path = arg.strip() or DEFAULT_SAVE
        try:
            game.state = load_game(path)
            game.history = load_history(path)  # 恢复对话历史，NPC 不失忆
        except (FileNotFoundError, ValueError) as e:
            print(f"[✗] 读档失败: {e}")
            return None
        game.ending = None
        print(f"已读档 ← {path}（含对话历史）")
        return "action"
    elif cmd == "/new":
        game.state = GameState.from_pack(game.pack)
        game.history = []
        game.ending = None
        game.last_choices = []
        print("重新开始。")
        return "new"
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

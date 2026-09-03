"""CLI 前端（M1 起逐步实现 REPL）。

当前命令：
  python -m game_agent check-worldpack [path]   校验并加载一个世界包（离线）
  python -m game_agent play                      开始游戏（W4–W7 逐步实现）
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .worldpack import WorldPackError, load_worldpack

DEFAULT_WORLDPACK = "world-packs/ancient_jianghu"


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


def _cmd_play(args: argparse.Namespace) -> int:
    print("「play」尚未实现——游戏主循环在 W4–W7 工作包中逐步构建。")
    print("当前可用命令：python -m game_agent check-worldpack")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="game-agent", description="文字对话养成游戏引擎"
    )
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check-worldpack", help="校验并加载一个世界包")
    p_check.add_argument("path", nargs="?", default=DEFAULT_WORLDPACK)

    sub.add_parser("play", help="开始游戏（尚未实现）")

    args = parser.parse_args(argv)
    if args.command == "check-worldpack":
        return _cmd_check_worldpack(args)
    if args.command == "play":
        return _cmd_play(args)
    parser.print_help()
    return 0

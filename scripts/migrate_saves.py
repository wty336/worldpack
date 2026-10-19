"""把 N4 之前**平铺在 `saves/` 根上**的存档搬进 `saves/<pack_id>/`。

用法::

    python scripts/migrate_saves.py            # 只报告（默认，不动任何文件）
    python scripts/migrate_saves.py --apply    # 真的搬

**为什么需要它，而不是自动迁移**：`saves/` 不只是运行时产物，它还是仓库里**有意维护的
证据库**（90 余份带语义名字的冒烟/回放记录）。启动时静默搬动别人的文件是不可接受的——
那会让"我昨天那份 smoke-ancient_jianghu.json 去哪了"变成一个需要考古的问题。
所以：**先报告、再动手**，两条命令分开。

**判据只有一条：存档里有没有剧本身份戳**（G2 的 `pack: {id, digest}`）。

- 有戳 → 能确定归属，搬进 `saves/<id>/`（与 N4 之后新写的档同一个位置）；
- 没戳（G2 之前的旧档）→ **无从判断它属于哪张卡**，原地不动并如实报告。
  它仍然读得出来（`_safe_save_path` 保留了"根上的旧档原地可读"的回退），
  只是继续躺在根上。

**目标已存在时不覆盖**：撞名说明两份档要抢同一个位置，这要人来决定
（可能是同一个文件被手工复制过，也可能真的是两局）。覆盖是不可逆的，报告是可逆的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from game_agent.save import saved_pack_meta

DEFAULT_SAVE_ROOT = Path("saves")


def plan_migration(save_root: Path) -> tuple[list[tuple[Path, Path]], list[tuple[Path, str]]]:
    """算出要搬什么、以及哪些搬不了。

    返回 `(moves, skipped)`：`moves` 是 `[(源, 目标)]`，`skipped` 是 `[(源, 原因)]`。
    **纯计算，不动文件**——所以"看一眼会怎么搬"与"真的搬"用的是同一段逻辑，
    不存在"计划里说搬 A、执行时搬了 B"这种只有执行才知道的分歧。
    """
    moves: list[tuple[Path, Path]] = []
    skipped: list[tuple[Path, str]] = []
    if not save_root.is_dir():
        return moves, skipped
    for src in sorted(save_root.glob("*.json")):
        meta = saved_pack_meta(src)
        if not meta or not meta.get("id"):
            skipped.append((src, "没有剧本身份戳（G2 之前的旧档），无从判断归属"))
            continue
        dst = save_root / meta["id"] / src.name
        if dst.exists():
            skipped.append((src, f"目标已存在：{dst}（不覆盖，请人工确认哪份是要留的）"))
            continue
        moves.append((src, dst))
    return moves, skipped


def apply_migration(moves: list[tuple[Path, Path]]) -> None:
    """执行搬迁（父目录按需创建）。"""
    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把平铺的存档搬进 saves/<pack_id>/（N4）")
    ap.add_argument("--root", default=str(DEFAULT_SAVE_ROOT), help="存档根目录（默认 saves/）")
    ap.add_argument("--apply", action="store_true", help="真的搬（默认只报告）")
    args = ap.parse_args(argv)

    save_root = Path(args.root)
    moves, skipped = plan_migration(save_root)

    if not moves and not skipped:
        print(f"{save_root}/ 下没有平铺的存档，无需迁移。")
        return 0

    print(f"存档根目录：{save_root.resolve()}")
    print(f"\n可迁移 {len(moves)} 份：")
    for src, dst in moves:
        print(f"  {src.name:44s} → {dst.relative_to(save_root).as_posix()}")
    if skipped:
        print(f"\n跳过 {len(skipped)} 份：")
        for src, why in skipped:
            print(f"  {src.name:44s} {why}")

    if not args.apply:
        print("\n这是**预演**，没有动任何文件。确认无误后加 --apply。")
        return 0
    apply_migration(moves)
    print(f"\n已迁移 {len(moves)} 份。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

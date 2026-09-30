"""世界包目录层（剧本市场的**数据面**）。

上游：`docs/plan-tavern-shaped-product.md` §2.1（E-3）。

**为什么需要这一层**：`world-packs/` 早就是一个目录了，`load_worldpack` 也能对任意
目录全量加载并在加载期做完交叉校验，返回富元数据（世界名/时代/开场/NPC/节点/结局）。
唯一挡住"玩家选一张卡"的是 `web.py` 里一个**进程级环境变量** `GAME_WORLDPACK`
——一个进程 = 一个包，换包要重启。本模块把那堆现成的元数据变成**可枚举、可校验、
可传给前端**的目录。

三条纪律（都是踩过的坑，不是设计洁癖）：

1. **单包失败不拖垮整个目录**。目录里有一个坏包（作者改到一半、YAML 缩进错了），
   目录仍然要能列出来——坏包以 `error` 字段呈现，让玩家看见"这张卡坏了"，
   而不是整个"选卡"界面白屏。
2. **`id` 就是目录名，且必须与存档身份戳同源**。`worldpack.pack_meta()` 用的也是
   `pack.root.name`——两边必须是同一个字符串，否则"按卡隔离存档"和"按卡归因成本"
   会对不上。
3. **`id` 绝不参与路径拼接**。客户端传来的 `pack_id` 只能通过 `resolve_pack()`
   在**已列出的目录项**里查表得到，永远不做 `root / pack_id` 这种拼接
   （`_safe_save_path` 的同一条教训：客户端可控的字符串直传文件 API 是路径穿越洞）。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .worldpack import WorldPackError, load_worldpack, pack_digest

DEFAULT_PACK_ROOT = "world-packs"

# 目录名白名单：与 `_safe_save_path` 同一姿态（宁可严，不可漏）。
# 允许中文与外文包名（作者会用 `武侠_旧梦` 这种），但**禁止任何路径成分**。
_ID_PATTERN = re.compile(r"^[\w\u4e00-\u9fff\-]{1,64}$")


@dataclass(frozen=True)
class PackEntry:
    """一张"卡"的元数据（前端卡片直接渲染这个）。"""

    id: str  # 目录名；**与 pack_meta()['id'] 同源**
    name: str = ""
    era: str = ""
    opening: str = ""
    npcs: int = 0
    nodes: int = 0
    endings: int = 0
    lore: int = 0
    locations: int = 0
    digest: str = ""  # 玩法内容指纹（与存档身份戳同源）
    path: str = ""
    error: str = field(default="")  # 非空 = 该包加载失败，原因在此

    @property
    def playable(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "era": self.era,
            "opening": self.opening,
            "npcs": self.npcs,
            "nodes": self.nodes,
            "endings": self.endings,
            "lore": self.lore,
            "locations": self.locations,
            "digest": self.digest,
            "path": self.path,
            "playable": self.playable,
            "error": self.error,
        }


def _is_pack_dir(path: Path) -> bool:
    """有 `world.yaml` 才算包目录。

    这样 `world-packs/_drafts/`（批次 3 的草稿区）与任何误建的空目录都不会
    变成"一张坏卡"——它们只是不是卡。
    """
    return path.is_dir() and (path / "world.yaml").is_file()


def _entry_from_dir(path: Path) -> PackEntry:
    """读一个包目录 → 目录项。**任何加载失败都降级为 `error`，不抛出。**"""
    base = PackEntry(id=path.name, path=str(path))
    try:
        pack = load_worldpack(path)
    except WorldPackError as e:  # 校验/缺失：作者可修，前端提示原文
        return PackEntry(id=path.name, path=str(path), error=f"{e}")
    except Exception as e:  # noqa: BLE001 — 目录层不得因单包异常而整体不可用
        return PackEntry(
            id=path.name, path=str(path), error=f"内部错误: {type(e).__name__}: {e}"
        )
    return PackEntry(
        id=path.name,
        name=pack.world.name,
        era=pack.world.era,
        opening=pack.world.opening or "",
        npcs=len(pack.npcs),
        nodes=len(pack.mainline.nodes),
        endings=len(pack.endings.endings),
        lore=len(pack.world.lore),
        locations=len(pack.world.locations),
        digest=pack_digest(path),
        path=str(path),
    )


_CACHE: dict[str, tuple[tuple, list[PackEntry]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX_ROOTS = 16


def _signature(root: Path) -> tuple:
    """目录的**廉价**变更指纹：只 `stat`，不解析 YAML。

    为什么不能只看包目录的 mtime：**修改一个已存在的文件不会改父目录的 mtime**，
    于是"作者改了 `npcs/x.yaml`"永远不会让缓存失效——而那正是最需要重建缓存的场景。
    逐个文件 stat（82 个文件，微秒级）比一次 `load_worldpack`（~35 ms）便宜三个数量级。

    指纹包含包内**全部**文件（含 `judge_corpus`）：保守一点只会多重建几次缓存，
    漏掉一个文件则会让玩家选到一张"元数据与内容不符"的卡。
    """
    parts: list[tuple] = []
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not _is_pack_dir(d):
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file():
                st = f.stat()
                parts.append((f.relative_to(root).as_posix(), st.st_size, st.st_mtime_ns))
    return tuple(parts)


def list_packs(
    root: str | Path = DEFAULT_PACK_ROOT,
    *,
    playable_only: bool = False,
    use_cache: bool = True,
) -> list[PackEntry]:
    """枚举目录下的世界包（按 id 排序）。

    坏包默认**也返回**（`playable=False` + `error` 原文）——"看不见的坏包"比
    "看得见的坏卡"更难排查。调用方要过滤时用 `playable_only=True`。

    **为什么要缓存**：一次全量列举 = 8 次 `load_worldpack`（含 717 行 `_cross_check`
    交叉校验 + 内容摘要），实测 **~288 ms**。而它会被"打开选卡屏一次 + 每次开局校验
    一次"调用——不缓存就是每次开局白等 0.3 秒。缓存只省掉**列举**这一次；
    真正决定加载哪一局的那个 `load_worldpack` 在 `_make_game` 里照常发生，
    所以"缓存过期导致玩到旧内容"不可能发生。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    key = str(base.resolve())
    sig: tuple | None = None
    if use_cache:
        sig = _signature(base)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None and hit[0] == sig:
            entries = hit[1]
            return [e for e in entries if e.playable] if playable_only else list(entries)

    entries = [
        _entry_from_dir(d) for d in sorted(base.iterdir(), key=lambda p: p.name)
        if _is_pack_dir(d)
    ]
    if use_cache and sig is not None:
        with _CACHE_LOCK:
            if len(_CACHE) > _CACHE_MAX_ROOTS:  # 测试会造大量临时目录，防无界增长
                _CACHE.clear()
            _CACHE[key] = (sig, entries)
    return [e for e in entries if e.playable] if playable_only else entries


def resolve_pack(pack_id: str | None, root: str | Path = DEFAULT_PACK_ROOT) -> PackEntry | None:
    """把客户端传来的 `pack_id` 解析成目录项；不认识就返回 None。

    **这是唯一的入口**，且实现是"在已列出的目录项里查表"——不做路径拼接，
    因此 `../`、绝对路径、盘符天然不可能生效（它们不在任何目录项里）。
    额外再做一次白名单正则，是为了在**目录本身**被塞进怪名字时也拒绝。
    """
    if not pack_id:
        return None
    if not _ID_PATTERN.fullmatch(pack_id):
        return None
    for entry in list_packs(root):
        if entry.id == pack_id:
            return entry
    return None


def default_pack_id(root: str | Path = DEFAULT_PACK_ROOT) -> str | None:
    """兜底选包：优先 `ancient_jianghu`（示例/冒烟基准），否则目录里第一个可玩的。

    为什么要有兜底而不是硬编码 DEFAULT_PACK：目录是内容，不该有"某个包必须在库"的
    假设——包被移走时应当退到"还有什么能玩"，而不是启动即 500。
    """
    entries = list_packs(root)
    playable = [e for e in entries if e.playable]
    for e in playable:
        if e.id == "ancient_jianghu":
            return e.id
    return playable[0].id if playable else None

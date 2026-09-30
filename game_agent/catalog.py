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
import shutil
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from .worldpack import WorldPackError, load_worldpack, pack_digest, validate_name


class CatalogError(Exception):
    """目录层的操作失败（发布/删除被拒）。与 `WorldPackError` 分工：
    那个是"包内容不合法"，这个是"目录层不允许这么做"。
    """


DEFAULT_PACK_ROOT = "world-packs"

# 目录名白名单：与 `_safe_save_path` 同一姿态（宁可严，不可漏）。
# 允许中文与外文包名（作者会用 `武侠_旧梦` 这种），但**禁止任何路径成分**。
_ID_PATTERN = re.compile(r"^[\w\u4e00-\u9fff\-]{1,64}$")

DRAFTS_DIRNAME = "_drafts"
"""草稿区目录名（`world-packs/_drafts/<name>/`）。

上游：`docs/plan-tavern-shaped-product.md` §3.2 ③——"生成的包先进草稿区，
过 `check_worldpack` 才允许发布"。这是 `plan-creator-player.md` 的**唯一写口**：
Web 界面不直写文件系统，所有变更经"生成 → 校验 → 发布"这条链。

**为什么必须是独立目录，而不是一个 `published: false` 字段**：
- 目录层扫描天然排除它——`_is_pack_dir` 要求目录里有 `world.yaml`，而 `_drafts`
  本身没有。于是**草稿不可能被误当成可玩的卡**，不需要每个读取点都记得过滤
  （"记得过滤"这种事迟早会漏）；
- 发布是一次 `rename`（同文件系统内原子），而不是"改一个字段 + 祈祷没人漏读"。

下划线前缀还有一层实用考虑：它在目录列表里排最前，作者一眼看得见"这是工作区，不是内容"。
"""


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
    draft: bool = False  # True = 还在草稿区：未发布、不在可选卡列表里
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
            "draft": self.draft,
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


def can_create(name: str, root: str | Path = DEFAULT_PACK_ROOT) -> str | None:
    """能否建一个新草稿；不能则返回原因。

    **这是 API 侧的护栏，CLI 没有它也不该有**：`import_story.py --name X` 是人手敲的，
    覆盖自己的包是明确意图；而一个能被脚本调用的接口不该有"静默毁掉已发布内容"的默认行为。

    草稿区（§3.2 ③）落地后这条放松了一半：**同名草稿可以反复覆盖**（那正是草稿的用途），
    但**已发布的同名包仍然拒绝**——否则发布时会撞名，且作者会误以为在改那个已发布的包。
    """
    if (bad := validate_name(name)) is not None:
        return bad
    published = Path(root) / name
    if published.exists():
        return (
            f"已存在同名的**已发布**世界包：{published}。"
            f"草稿不会写到这里（避免覆盖线上内容）；请换个名字，"
            f"或先用别的名字生成草稿再比较。"
        )
    return None


# ---------------------------------------------------------------------------
# 草稿区 ↔ 已发布区（§3.2 ③ 的"唯一写口"）
# ---------------------------------------------------------------------------


def drafts_root(root: str | Path = DEFAULT_PACK_ROOT) -> Path:
    return Path(root) / DRAFTS_DIRNAME


def draft_dir(name: str, root: str | Path = DEFAULT_PACK_ROOT) -> Path:
    """草稿目录。**调用方必须先 `validate_name`**（这里只做拼接）。"""
    return drafts_root(root) / name


def list_drafts(root: str | Path = DEFAULT_PACK_ROOT) -> list[PackEntry]:
    """草稿区里的包（与 `list_packs` 同形状，`draft=True`）。

    **与 `list_packs` 的一个刻意差别**：这里列出 `_drafts/` 下的**每一个子目录**，
    不要求它有 `world.yaml`。理由：
    - 已发布区是"内容"，缺 `world.yaml` 的东西**不是内容**，不该出现在卡列表里；
    - 草稿区是"工作区"，作者**明确**把它放在那儿。一个缺文件的草稿如果直接消失，
      作者看到的是"我生成的草稿去哪了"——而这正是最需要看到错误原文的时刻。
      列出它、附上 `error`，比藏起来有用。

    （生成失败在提取阶段时根本不会建目录，所以正常路径下的草稿都是有 `world.yaml` 的；
    这条主要覆盖"作者手工动过草稿"与"生成中途被杀"。）
    """
    base = drafts_root(root)
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.iterdir(), key=lambda p: p.name):
        if d.is_dir() and not d.name.startswith("."):
            out.append(replace(_entry_from_dir(d), draft=True))
    return out


def resolve_draft(name: str | None, root: str | Path = DEFAULT_PACK_ROOT) -> PackEntry | None:
    """把名字解析成草稿项；不存在/非法返回 None。与 `resolve_pack` 同样是**查表**。"""
    if not name or not _ID_PATTERN.fullmatch(name):
        return None
    for entry in list_drafts(root):
        if entry.id == name:
            return entry
    return None


def can_publish(name: str, root: str | Path = DEFAULT_PACK_ROOT) -> str | None:
    """能否把该草稿发布出去；不能则返回原因。**闸门 = `check_worldpack` 必须过。**

    上游 §3.2 ③："过 `check_worldpack` 才允许发布"。这条不是形式主义——
    `check_worldpack` 会拒绝"该节点将永远无法完成""结局数值不可达""引用了未声明的旗标"
    这类**作者看不出来但玩家一定会撞上**的问题。发布是把草稿变成"别人也能玩"的承诺，
    没过闸门的东西不该获得这个承诺。
    """
    if (bad := validate_name(name)) is not None:
        return bad
    entry = resolve_draft(name, root)
    if entry is None:
        return f"草稿不存在：{drafts_root(root) / name}"
    if (Path(root) / name).exists():
        return (
            f"已存在同名的已发布包：{Path(root) / name}。"
            f"发布不会覆盖已发布内容——请先改名，或另行处理那个包。"
        )
    if not entry.playable:
        return (
            f"草稿未通过 check-worldpack，不能发布：\n{entry.error}\n"
            f"（web 创作工作台会把这段报错原文给模型去修；也可以在草稿目录里手工改）"
        )
    return None


def publish(name: str, root: str | Path = DEFAULT_PACK_ROOT) -> PackEntry:
    """把草稿发布到已发布区（`rename`，同一文件系统内原子）。失败抛 `CatalogError`。"""
    if (bad := can_publish(name, root)) is not None:
        raise CatalogError(bad)
    src = draft_dir(name, root)
    dst = Path(root) / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)  # 原子：失败时草稿原地不动，不会出现"半个已发布包"
    except OSError as e:
        raise CatalogError(f"发布失败（草稿仍在原处）：{e}") from e
    _invalidate(root)
    entry = _entry_from_dir(dst)
    _CACHE.pop(str(Path(root).resolve()), None)
    return entry


def delete_draft(name: str, root: str | Path = DEFAULT_PACK_ROOT) -> None:
    """删除草稿（清理用）。拒绝删已发布包——那不在本函数的职责里。"""
    if validate_name(name) is not None or resolve_draft(name, root) is None:
        raise CatalogError(f"草稿不存在：{name!r}")
    shutil.rmtree(draft_dir(name, root))
    _invalidate(root)


def can_fork(name: str, root: str | Path = DEFAULT_PACK_ROOT) -> str | None:
    """能否把已发布的 `name` 复制成一份草稿；不能则返回原因。

    **为什么需要这条**：创作者 Agent（N6）面向**工作版**改内容，而工作版就是草稿区
    （§4.2 "工作者面向工作版，原始包只读"）。库里的 8 张卡都已经发布，
    没有这条路径，Agent 就只能在"刚生成、还没发布"的草稿上工作——
    等于对着空包改，能力 C 的实际用处归零。
    """
    if (bad := validate_name(name)) is not None:
        return bad
    if not (Path(root) / name).is_dir():
        return f"没有这个已发布的世界包：{Path(root) / name}"
    if draft_dir(name, root).exists():
        return (
            f"草稿区已存在同名草稿：{draft_dir(name, root)}。"
            f"草稿不覆盖——先用它，或先删掉它再复制。"
        )
    return None


def fork_to_draft(name: str, root: str | Path = DEFAULT_PACK_ROOT) -> PackEntry:
    """把已发布包复制成草稿（"拿现成的卡来改"）。失败抛 `CatalogError`。

    **是复制不是移动**：已发布内容必须原地不动——它可能正被某个玩家玩着，
    而且"复制一份来改"才符合草稿区的语义（原版只读）。发布时若同名已存在，
    `can_publish` 会拒绝，所以这条不会静默覆盖线上内容。
    """
    if (bad := can_fork(name, root)) is not None:
        raise CatalogError(bad)
    src = Path(root) / name
    dst = draft_dir(name, root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dst)
    except OSError as e:
        shutil.rmtree(dst, ignore_errors=True)  # 半个副本比没有更糟
        raise CatalogError(f"复制成草稿失败（已回滚）：{e}") from e
    _invalidate(root)
    return replace(_entry_from_dir(dst), draft=True)


def _invalidate(root: str | Path) -> None:
    """目录变更后主动清缓存（不等指纹比对）。"""
    with _CACHE_LOCK:
        _CACHE.pop(str(Path(root).resolve()), None)

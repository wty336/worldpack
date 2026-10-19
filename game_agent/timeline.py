"""N7 / G-2：玩家面的存档点 / 回退 / 分支（把 `runlog` 的回合级 checkpoint 产品化）。

上游：`docs/plan-dsh-tavern-parity.md` 期 2 / 差距 **G-2（P0）**、`docs/roadmap.md` **N7**。

## 为什么不在 `runlog.RunRecorder` 上直接改

`runlog` 的定位是**事后实验设施**（写在 gitignore 的 `runs/` 下，由驱动脚本调用），
而且它按 **turn** 命名 checkpoint：`checkpoints/{turn:06d}.json`。这在"回放一局"里没问题，
但在**玩家面**里直接撞死：

> 回退到第 5 回合 → 继续玩 → 又走到第 6 回合 → **`000006.json` 被覆盖**，
> 旧的那一版第 6 回合永久消失，而玩家以为自己"开了个分支"。

所以本模块按 **revision（只增不减）** 编号，而不是按 turn —— 这是 G-2 的核心数据结构，
不是实现细节。`runlog` 一行不改，两者分工：它是实验线，这是玩家线。

## 四条语义（直接采用 dsh-tavern 踩过坑的结论，不重新发明）

1. **revision 只增不减**：回退**也**产生一个新版本（`rev = max+1`），
   而不是把指针挪回去。于是"发生了什么"永远是可审计的追加序列，
   旧版本不会被覆盖，也不会出现"同一 rev 指向两份不同内容"。
2. **回退即开分支**：回退到 rev N 会分配一个新 `branch`（`b2`、`b3`…），
   `parent` 记下它从哪来。**旧分支的条目与文件原地不动**——保留可查，
   但当前线已经是新分支，旧分支**不再影响当前剧情**（没有任何读取路径会碰它）。
3. **派生失败不阻塞**：checkpoint 的写入发生在回合已经提交**之后**，
   所以写盘失败**不得**让这一回合失败（调用方负责吞掉并如实提示）。
   与 ADR 0006 同一条纪律：前台已提交的东西不因为后台派生失败而撤销。
4. **每条 checkpoint 带 rng 状态**：检定掷骰、`chance` 日程事件、`{base, spread}` 收益
   曲线都吃 rng（`game.rng`），快照不覆盖它。不记 rng 的话，回退后的世界会
   从"另一个随机流"继续长——玩家的观感是"回退了但世界变了"。
   `runlog.rebuild_game` 已经能把 JSON 回环的元组结构还原，这里复用同一份序列化口径。

## 存储

```
saves/timeline-<sid>/
  timeline.jsonl        # 追加式索引：每行一个 revision
  rev-000001.json       # 一个 revision 的全量快照（state + history + rng_state）
```

**回退产生的新 revision 不复制快照**，而是复用被回退到的那个 rev 的文件
（索引里记 `checkpoint: rev-000005.json`）。理由：状态**逐字节相同**，
复制只是在磁盘上放两份一样的东西；而"这一版的状态来自哪个快照"在索引里是显式的。
继续玩下去写出的新 revision 才是新文件——所以只有回退那一行是共享的。

## 保留策略：**全都留着**

长局 10MB 级（`plan-runtime-platform.md` §2.2 的成本口径已接受）。本模块**不删**任何
revision——G-2 的整个价值就是"回得去"，静默丢弃历史会直接摧毁它。
`size_bytes()` 把占用暴露给前端，让人自己看得见。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

TIMELINE_DIRNAME = "timeline"
INDEX_NAME = "timeline.jsonl"
MAX_LABEL = 80

MAX_SNAPSHOTS = 40
"""最多保留多少个**快照文件**（N10-3）。

**为什么要有上限**：每个 revision 存一份全量 `state.to_dict() + history`，长局实测
约 30KB/版 → 300 回合就是 ~9MB/局，而且**永不回收**。异步化（N8）之后回合推进更快，
这条只会更明显。

**为什么按"文件"而不是按"条目"计数**：回退派生的 revision **复用**目标那一版的
快照文件（`rewind_to` 的 `reuse`）——按条目计数的话，回退十次就会误删十个仍被
引用的文件。

**为什么这个数字**：40 版 ≈ 40 个回合的"任意回去"能力，远超实际手感（玩家真正常用的
是最近几版）；再往前的版本被回收时**明确告诉玩家**（`pruned` 标记 + 拒绝理由），
而不是让他点到一片空白。

与 dsh-tavern 的同与不同：它 `checkpoint 上限 40`、读不到就抛
`CHECKPOINT_HISTORY_REQUIRED` 而**不猜**——这两条都照做；不同之处是它只有 checkpoint
没有"分支"概念，而这里旧分支的索引条目**保留**（可审计），只是快照可能已回收。
"""


class TimelineError(Exception):
    """时间线操作失败（未知 revision / 索引损坏 / 快照已回收）。"""


def pruned_error(rev: int) -> TimelineError:
    """快照已回收时的**可照做**的错误（对应 dsh-tavern 的 CHECKPOINT_HISTORY_REQUIRED）。

    **绝不猜**：不复用别的版本、不拿 head 顶替、不静默回退到最近可用版本——
    那会让玩家以为"回到了第 5 回合"，实际落在第 45 回合的状态上，而且是静默的。
    """
    return TimelineError(
        f"CHECKPOINT_HISTORY_REQUIRED：rev {rev} 的快照已被回收"
        f"（只保留最近 {MAX_SNAPSHOTS} 版）。请回到列表里仍可点的版本，"
        f"或从存档列表读档——**不会**用别的版本顶替（那会让状态与你的预期不符）。"
    )


def timeline_root(sid: str, base: str | Path = "saves") -> Path:
    """一局的时间线目录。

    **`sid` 不参与路径拼接的校验责任在调用方**（`web.py` 的 sid 是自己生成的 hex），
    这里再做一次白名单，与 `catalog` / `_safe_save_path` 同一姿态：
    客户端可控的字符串直传文件 API 是路径穿越洞。
    """
    if not sid or not all(c.isalnum() or c in "-_" for c in sid):
        raise TimelineError(f"非法会话标识：{sid!r}")
    return Path(base) / f"{TIMELINE_DIRNAME}-{sid}"


def _rev_name(rev: int) -> str:
    return f"rev-{rev:06d}.json"


@dataclass(frozen=True)
class Entry:
    """一个 revision 的索引条目。"""

    rev: int
    branch: str
    parent: int | None
    turn: int
    day: int
    ts: str
    label: str
    note: str = ""
    checkpoint: str = ""
    #: N10-3：快照文件已被回收（超过 `MAX_SNAPSHOTS`）。条目仍然保留——**可审计**
    #: 比"少一段历史"重要；前端据它把入口置灰并说明原因，而不是让玩家点了才失败。
    pruned: bool = False

    @property
    def is_rewind(self) -> bool:
        return self.parent is not None

    def to_dict(self) -> dict:
        return {
            "rev": self.rev,
            "branch": self.branch,
            "parent": self.parent,
            "turn": self.turn,
            "day": self.day,
            "ts": self.ts,
            "label": self.label,
            "note": self.note,
            "is_rewind": self.is_rewind,
            "pruned": self.pruned,
        }


class Timeline:
    """一局的时间线。所有写操作只落在自己的目录里。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.index_path = self.root / INDEX_NAME

    # ---- 基础 ----

    def entries(self) -> list[Entry]:
        if not self.index_path.is_file():
            return []
        out: list[Entry] = []
        for i, line in enumerate(self.index_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as e:
                # 索引损坏：**不静默跳过**（跳过等于悄悄少一段历史，玩家看不出来）
                raise TimelineError(f"时间线索引第 {i} 行损坏：{e}") from e
            cp = str(d.get("checkpoint", ""))
            out.append(Entry(
                rev=int(d["rev"]), branch=str(d["branch"]), parent=d.get("parent"),
                turn=int(d.get("turn", 0)), day=int(d.get("day", 0)),
                ts=str(d.get("ts", "")), label=str(d.get("label", "")),
                note=str(d.get("note", "")), checkpoint=cp,
                # N10-3：**现算**而不是存进索引。存进去会变成第二份真值：
                # 有人手工删了快照文件、或将来回收策略变了，标记就会撒谎。
                pruned=bool(cp) and not (self.root / cp).is_file(),
            ))
        return out

    def head(self) -> Entry | None:
        entries = self.entries()
        return entries[-1] if entries else None

    def branches(self) -> list[str]:
        seen: list[str] = []
        for e in self.entries():
            if e.branch not in seen:
                seen.append(e.branch)
        return seen

    def current_branch(self) -> str:
        h = self.head()
        return h.branch if h else ""

    def payload(self, rev: int) -> dict:
        """读某个 revision 的全量快照。

        N10-3：快照已回收（或文件被删）→ 抛**可照做**的错误，**不猜**。
        """
        e = self._entry(rev)
        p = self.root / e.checkpoint
        if not p.is_file():
            raise pruned_error(rev)
        return json.loads(p.read_text(encoding="utf-8"))

    def live_snapshots(self) -> list[tuple[str, int]]:
        """现存快照文件 → `[(文件名, 引用它的最大 rev)]`（N10-3 的回收依据）。

        按**文件**聚合而不是按条目：回退派生的 revision 复用目标那一版的快照，
        一个文件可能被多条条目引用，"最近一次被用到"才是它该不该留下的判据。
        """
        newest: dict[str, int] = {}
        for e in self.entries():
            if not e.checkpoint:
                continue
            if not (self.root / e.checkpoint).is_file():
                continue  # 已经回收掉的，不用再管
            newest[e.checkpoint] = max(newest.get(e.checkpoint, 0), e.rev)
        return sorted(newest.items(), key=lambda kv: kv[1])

    def prune(self, keep: int | None = None) -> list[str]:
        """回收最旧的快照，只留 `keep` 个文件；返回被删的文件名（N10-3）。

        `keep=None` 时**运行时**读模块常量 `MAX_SNAPSHOTS`（而不是把它烧进默认参数）：
        默认参数在函数定义时求值，那样测试就没法把上限调小来跑长局形态，
        而且改常量会让"代码里写的上限"和"实际生效的上限"悄悄不一致。

        **只删"最新引用它的 rev 也足够旧"的文件**，所以 head 所在的快照永远留着
        （它是最大 rev）——"这一局随时可以继续"这件事不能被回收破坏。

        索引条目**一个都不删**：玩家看到的仍然是完整的"这一局发生过什么"，
        只是更早的版本标成 `pruned`（可审计 > "悄悄少了一段历史"）。
        """
        if keep is None:
            keep = MAX_SNAPSHOTS
        snaps = self.live_snapshots()
        if len(snaps) <= keep:
            return []
        # head 的快照是绝对不能动的
        h = self.head()
        protected = {h.checkpoint} if h is not None else set()
        victims: list[str] = []
        for name, _rev in snaps:
            if len(snaps) - len(victims) <= keep:
                break
            if name in protected:
                continue
            victims.append(name)
        for name in victims:
            (self.root / name).unlink(missing_ok=True)
        return victims

    def size_bytes(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.glob("*.json*") if p.is_file())

    def _entry(self, rev: int) -> Entry:
        for e in self.entries():
            if e.rev == rev:
                return e
        raise TimelineError(
            f"没有 rev {rev}（现有：{', '.join(str(e.rev) for e in self.entries()) or '无'}）"
        )

    def _next_rev(self) -> int:
        h = self.head()
        return (h.rev + 1) if h else 1

    # ---- 写 ----

    def append(self, *, state, history: list[dict], rng, label: str,
               note: str = "") -> Entry:
        """记录一个 revision（回合**已提交**之后调用）。

        `rng` 传 `game.rng`；`label` 是给玩家看的一句话（谁做了什么），
        `note` 非空表示这一版是"回退派生"出来的。
        """
        rev = self._next_rev()
        branch = self.current_branch() or "b1"
        return self._write(rev=rev, branch=branch, parent=None, state=state,
                           history=history, rng=rng, label=label, note=note)

    def rewind_to(self, rev: int, *, label: str = "") -> Entry:
        """回退到 rev：**产生一个新 revision + 新分支**，并返回它。

        调用方拿到这个 Entry 后应当用 `payload(rev)` 重建 Game
        （`runlog.rebuild_game` 或 `web._rebuild_from_timeline`）。
        **不修改任何已有条目**——旧分支原地保留。
        """
        target = self._entry(rev)
        # N10-3：要回的那一版快照已被回收 → **明确拒绝**（不拿别的版本顶替）
        if not (self.root / target.checkpoint).is_file():
            raise pruned_error(rev)
        h = self.head()
        if h is not None and rev == h.rev and h.parent is None and h.branch == self.current_branch():
            # 回退到"就是当前这一版"是空操作：不产生垃圾 revision
            return h
        new_rev = self._next_rev()
        new_branch = f"b{len(self.branches()) + 1}"
        return self._write(
            rev=new_rev, branch=new_branch, parent=rev,
            state=None, history=None, rng=None,
            label=label or f"回到 rev {rev}",
            note=f"从 rev {rev}（{target.label}）回退，开分支 {new_branch}",
            reuse=target.checkpoint,
        )

    def _write(self, *, rev: int, branch: str, parent: int | None, state, history,
               rng, label: str, note: str = "", reuse: str = "") -> Entry:
        self.root.mkdir(parents=True, exist_ok=True)
        if reuse:
            src = self.root / reuse
            if not src.is_file():
                raise TimelineError(f"要复用的快照不存在：{reuse}")
            payload = json.loads(src.read_text(encoding="utf-8"))
            cp_name = reuse
        else:
            if state is None:
                raise TimelineError("新建 revision 需要 state（回退派生请走 rewind_to）")
            payload = {"state": state.to_dict(), "history": history}
            if rng is not None:
                # 与 `runlog.RunRecorder.checkpoint` 同一口径：JSON 回环把元组读成
                # 列表，重建时由 `runlog.rebuild_game` 还原成元组。
                payload["rng_state"] = rng.getstate()
            cp_name = _rev_name(rev)
            (self.root / cp_name).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        st = payload.get("state") or {}
        entry = Entry(
            rev=rev, branch=branch, parent=parent,
            turn=int(st.get("turn_count", 0)), day=int(st.get("day", 0)),
            ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
            label=(label or "")[:MAX_LABEL], note=note, checkpoint=cp_name,
        )
        with open(self.index_path, "a", encoding="utf-8") as f:
            # `pruned` 是**读时现算**的派生量，不落索引（落进去就成了会撒谎的第二份真值）
            row = {k: v for k, v in entry.to_dict().items() if k != "pruned"}
            f.write(json.dumps({**row, "checkpoint": cp_name},
                               ensure_ascii=False) + "\n")
        # N10-3：写完就回收（而不是"启动时清理"——那会让长局在跑的过程里一路涨，
        # 崩一次就留下一个最大的垃圾堆）。
        self.prune()
        return entry

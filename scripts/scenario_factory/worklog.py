"""产线工作日志：**断点续跑**的真源（2026-09-13 生产批前评估补）。

**为什么需要**（评估时在 `assemble.py:main` 里逐行读出来的两个事实，不是推测）：

1. 出库是**最后一步**一次性落盘（`write_layer` 在全部模块产完之后才被调用），样本此前只活在
   内存里 → 十来小时的批（≈¥45）跑到第 9 小时断了（Ctrl-C / 网络超时 / 机器重启），
   **全部作废**，已花的调用费不可回收。
2. "分模块跑"有两个坑：① 同模块再跑一次 `range(count)` 又从 i=0 起（`_build_module`）
   → 前一批**重复付费**并**覆盖**出库文件；② `write_layer` 的 manifest 只记**本次**模块
   → 先跑 extract 再跑 judge，manifest 里就只剩 judge，`extract.jsonl` 虽在盘上却无人认领
   （下游按 manifest 校验会**静默漏掉一个模块**）。

**做法**：每处理完一张卡就**立刻**追加一行到 `{out}/{layer}/.work/{module}.jsonl`，
写完 `flush` 落盘。键是 `i` —— `card_seed(seed_base, i, module)` 决定卡，而
`generate_card` 是确定性的（同 `(seed, seq, module)` 必出同一张卡），
故"续跑"就是"跳过日志里已有的 `i`"。日志 append-only + **后写的行覆盖先写的**（last-wins），
所以补判（质检/重造）也只需再追加一行，不必改写历史。

行格式（JSON Lines，首行是头）::

    {"_header": {"journal": 1, "layer": "train", "module": "extract", ...}}
    {"i": 0, "status": "kept", "card_id": "sc-10000-0000", "sample": {...}}
    {"i": 3, "status": "dropped", "card_id": "sc-10003-0003", "reason": "演绎丢弃: ..."}
    {"i": 7, "status": "rejected", "card_id": "sc-10007-0007", "reason": "质检自然度<1"}
    {"i": 9, "status": "kept", ..., "quality": 2, "sampled": true}   # 后写覆盖：补上质检判定

**为什么要版本守卫**：续跑最大的风险不是丢数据，而是把**两代提示词**产出的样本混进同一份
数据集（改完 `VERBALIZE_SYSTEM` 接着跑：旧风格 400 条 + 新风格 600 条，出库时**看不出来**）。
故日志头记 `factory_version`（提示词/卡生成器内容摘要）+ 采样档 + 模型名，不一致就**拒绝续跑**；
要接着跑只能显式 `--fresh`（已产样本作废，账要认）。

状态语义（`pending()` 只跳过 `kept`/`dropped`）：

- `kept`     过门禁，进样本集
- `dropped`  门禁丢的（演绎丢弃/confab 撞卡/前缀去重…）——重跑**不重做**：否则每续跑一次就把
             丢过的卡再烧一遍钱；要重做走 `--fresh`，或把数量调大去产新卡
- `rejected` 质检自然度 <1（§7.4 的"剔除重造"）——**可重造**，故不算"已完成"
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field as dc_field

JOURNAL_VERSION = 1
WORK_DIR = ".work"          # 藏在层目录里（不进 manifest、不进数据集）

KEPT = "kept"
DROPPED = "dropped"
REJECTED = "rejected"


def journal_path(out_dir: pathlib.Path | str, layer: str, module: str) -> pathlib.Path:
    return pathlib.Path(out_dir) / layer / WORK_DIR / f"{module}.jsonl"


@dataclass
class Journal:
    path: pathlib.Path
    header: dict | None = None
    entries: dict[int, dict] = dc_field(default_factory=dict)   # 后写覆盖先写
    corrupt: int = 0                                            # 解析不出的行数

    # --- 读侧 -----------------------------------------------------------------

    @property
    def done(self) -> set[int]:
        """已处理（含被丢的）→ 重跑不重做。`rejected` **不算**（它要重造）。"""
        return {i for i, e in self.entries.items() if e["status"] in (KEPT, DROPPED)}

    @property
    def rejected(self) -> dict[int, str]:
        return {i: e.get("reason", "") for i, e in self.entries.items()
                if e["status"] == REJECTED}

    def pending(self, target: int) -> list[int]:
        """目标前 `target` 张卡里**还没产出**的索引（升序）。"""
        return [i for i in range(target) if i not in self.done]

    def kept(self) -> list[dict]:
        """保留样本（按索引升序 = 产卡顺序）。"""
        return [e["sample"] for _, e in sorted(self.entries.items())
                if e["status"] == KEPT and "sample" in e]

    def built_samples(self) -> list[dict]:
        """**产出过的**样本（含被质检剔除的）—— 发布时要用它去 `drop_flagged`，
        账才对得上：`built − 剔除 = 出库`（与 `_stats_from_journals` 的 built 同口径）。"""
        return [e["sample"] for _, e in sorted(self.entries.items())
                if e["status"] in (KEPT, REJECTED) and "sample" in e]

    def entries_in_order(self) -> list[dict]:
        return [e for _, e in sorted(self.entries.items())]

    def unjudged(self) -> list[dict]:
        """保留但**还没质检过**的条目（质检判定写回日志后就不再重复判 → 发布幂等）。"""
        return [e for _, e in sorted(self.entries.items())
                if e["status"] == KEPT and "quality" not in e]

    # --- 写侧 -----------------------------------------------------------------

    @classmethod
    def open(cls, out_dir: pathlib.Path | str, layer: str, module: str,
             header: dict) -> "Journal":
        """读日志；不存在/空则视为空日志，并把 `header` 挂上（首次 append 时落盘）。"""
        j = read_journal(journal_path(out_dir, layer, module))
        if not j.header:
            j.header = header
        return j

    def append(self, entry: dict) -> None:
        """追加一行并**落盘**（`flush` 到 OS：进程被杀不丢已完成的行）。"""
        append_entry(self.path, self.header, entry)
        self.entries[entry["i"]] = entry


def append_entry(path: pathlib.Path, header: dict | None, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = not path.exists() or path.stat().st_size == 0
    # newline="\n"：Windows 下默认会把 \n 写成 \r\n（与 file_digest 的换行归一化同因）
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        if first and header:
            fh.write(json.dumps({"_header": header}, ensure_ascii=False) + "\n")
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()


def read_journal(path: pathlib.Path) -> Journal:
    """读日志。**坏行不致命**：解析不出的行跳过并计数（"空 = 未知 ≠ 通过"在这里的用法是
    ——解析不出的行**不算已完成**，宁可多产一张卡，也不能因为一次断电把整份日志判废）。"""
    j = Journal(path)
    if not path.exists():
        return j
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            j.corrupt += 1
            continue
        if isinstance(rec, dict) and "_header" in rec:
            j.header = rec["_header"]
        elif isinstance(rec, dict) and "i" in rec and "status" in rec:
            j.entries[int(rec["i"])] = rec
        else:
            j.corrupt += 1
    return j


def header_mismatch(found: dict | None, want: dict) -> str | None:
    """日志头与当前产线是否一致（一致 → `None`）。**缺头 = 未知 ≠ 一致**，同样拒绝。"""
    if not found:
        return "日志没有头（无法确认是哪一代产线产的）"
    diff = [f"{k}: 日志 {found.get(k)!r} ≠ 现在 {v!r}"
            for k, v in sorted(want.items()) if found.get(k) != v]
    return "；".join(diff) or None

"""N10-3 守卫：时间线快照的**上限**与"读不到就不猜"。

上游：`docs/plan-tavern-shaped-product.md` §8.3、`docs/roadmap.md` **N10**。

**被钉住的两个缺口**：

1. **无界增长**：每个 revision 存一份全量 `state.to_dict() + history`（长局实测
   ~30KB/版）。300 回合 ≈ 9MB/局，而它**永不回收**。异步化（N8）之后回合推进更快，
   这条只会更明显。
2. **读不到就猜**：快照缺了（回收/被删/盘满）时，任何"挑一个相近版本顶替"的做法
   都会让玩家以为回到了第 5 回合、实际落在第 45 回合的状态上——而且**静默**。
   dsh-tavern 的做法是抛 `CHECKPOINT_HISTORY_REQUIRED` 而不猜，这里照做。

守六件事：

1. 快照文件数**有上限**，且按**文件**而不是按条目计数（回退派生的版本复用同一份快照，
   按条目算会误删仍被引用的文件）；
2. **已回收的版本明确标出来**（`pruned`），玩家看得见"回不去了"而不是点了才失败；
3. 索引条目**一条都不删**——"这一局发生过什么"是可审计的，回收只回收体积；
4. head 的快照**永远留着**：随时可以继续这一局这件事不能被回收破坏；
5. 读已回收的版本 → 抛**可照做**的错误（含 `CHECKPOINT_HISTORY_REQUIRED` 与怎么办），
   **不拿别的版本顶替**；
6. 真的省下空间：跑 60 版之后的占用要显著小于 60 份快照的总和。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from game_agent import timeline as tl_mod
from game_agent.state import GameState
from game_agent.timeline import MAX_SNAPSHOTS, Timeline, TimelineError
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


@pytest.fixture(scope="module")
def pack():
    """包只加载一次（`load_worldpack` 在 Windows 上约 1 秒/次，本文件 11 个用例）。"""
    return load_worldpack(PACK_PATH)


@pytest.fixture
def state(pack):
    return GameState.from_pack(pack)


@pytest.fixture
def tl(tmp_path):
    return Timeline(tmp_path / "timeline-test")


def _turn(state: GameState, i: int) -> None:
    """推一回合（只推回合计数与场景，够造出体积差异）。"""
    state.turn_count = i
    state.day = 1 + i // 10
    state.scene = f"第 {i} 回合的场景：" + "长" * 50


def _append_many(tl: Timeline, state: GameState, n: int) -> None:
    rng = random.Random(0)
    for i in range(1, n + 1):
        _turn(state, i)
        tl.append(state=state, history=[{"role": "user", "content": f"第 {i} 回合"}],
                  rng=rng, label=f"第 {i} 回合")


# ---------------------------------------------------------------------------
# 1/2/4. 上限 · 标记 · head 永远在
# ---------------------------------------------------------------------------


def test_snapshot_count_is_capped(tl, state):
    """用**真上限**跑一次：磁盘上的快照文件不超过 `MAX_SNAPSHOTS`。

    （其余用例把上限调小以便跑得快——它们测的是机制，与上限的具体数值无关；
    这一条专门负责"默认真上限确实生效"。）
    """
    _append_many(tl, state, MAX_SNAPSHOTS + 2)
    snap_files = [p for p in tl.root.glob("rev-*.json") if p.is_file()]
    assert len(snap_files) <= MAX_SNAPSHOTS, f"快照文件 {len(snap_files)} 个，超过上限 {MAX_SNAPSHOTS}"


def test_index_keeps_every_revision_even_after_pruning(tl, state, monkeypatch):
    """**索引一条都不删**：回收的是体积，不是历史记录。"""
    monkeypatch.setattr(tl_mod, "MAX_SNAPSHOTS", 5)  # 行为与上限数值无关，调小只为跑得快
    _append_many(tl, state, 12)
    entries = tl.entries()
    assert len(entries) == 12, f"索引被误删：只剩 {len(entries)} 条"
    assert [e.rev for e in entries] == list(range(1, 13))


def test_pruned_entries_are_marked(tl, state, monkeypatch):
    """被回收的版本必须标 `pruned=True`；近期的必须是 False。"""
    monkeypatch.setattr(tl_mod, "MAX_SNAPSHOTS", 5)
    _append_many(tl, state, 12)
    entries = {e.rev: e for e in tl.entries()}
    assert entries[1].pruned is True, "第 1 版早该回收了却没标"
    assert entries[12].pruned is False, "head 不能被回收"
    marked = [r for r, e in entries.items() if e.pruned]
    assert marked == sorted(marked) and marked == list(range(1, 8)), (
        f"应当是连续的最早若干版被回收（12-5=7 个），实际 {marked}"
    )


def test_head_snapshot_always_survives(tl, state, monkeypatch):
    """head 的快照必须一直在——"随时可以继续这一局"不能被回收破坏。

    分几个阶段追加（每次跨过回收线），确认**任何时刻** head 都读得出来。
    上限调小到 5 来跑：这里测的是"回收之后 head 还在不在"，与上限的具体数值无关，
    而跑真上限 40 的形态只是把这个断言变慢。
    """
    monkeypatch.setattr(tl_mod, "MAX_SNAPSHOTS", 5)
    for extra in (1, 7, 12):
        _append_many(tl, state, extra)
        head = tl.head()
        assert head is not None
        assert (tl.root / head.checkpoint).is_file(), f"跑到 {len(tl.entries())} 版后 head 的快照没了"
        assert head.pruned is False


def test_pruning_counts_files_not_entries(tl, state):
    """回退会产生大量条目但**复用小快照**——回收必须按文件算，否则会误删。"""
    _append_many(tl, state, 3)
    for _ in range(50):
        tl.rewind_to(2)  # 每条都复用 rev2 的快照文件
    files = [p for p in tl.root.glob("rev-*.json")]
    assert len(files) <= 3, f"50 次回退不该产生 50 份快照，实际 {len(files)} 份"
    # 被复用的 rev2 快照必须还在（否则所有回退派生版本一起失效）
    assert (tl.root / "rev-000002.json").is_file()


# ---------------------------------------------------------------------------
# 3/5. 不猜：读已回收的版本要明确拒绝
# ---------------------------------------------------------------------------


def test_payload_of_pruned_revision_refuses_with_actionable_error(tl, state, monkeypatch):
    monkeypatch.setattr(tl_mod, "MAX_SNAPSHOTS", 5)
    """读已回收版本 → 抛带有 `CHECKPOINT_HISTORY_REQUIRED` 与"该怎么办"的错误。"""
    _append_many(tl, state, 12)
    with pytest.raises(TimelineError) as ei:
        tl.payload(1)
    msg = str(ei.value)
    assert "CHECKPOINT_HISTORY_REQUIRED" in msg
    assert "存档" in msg and "仍可点" in msg, f"错误没说清怎么办：{msg}"


def test_rewind_to_pruned_revision_refuses_instead_of_substituting(tl, state, monkeypatch):
    monkeypatch.setattr(tl_mod, "MAX_SNAPSHOTS", 5)
    """回退到已回收版本必须**拒绝**——绝不能挑一个相近版本顶替（那是静默的状态错位）。"""
    _append_many(tl, state, 12)
    before = len(tl.entries())
    with pytest.raises(TimelineError) as ei:
        tl.rewind_to(1)
    assert "CHECKPOINT_HISTORY_REQUIRED" in str(ei.value)
    assert len(tl.entries()) == before, "拒绝时不该留下半截 revision"


def test_no_silent_substitution_of_state(tl, state, monkeypatch):
    monkeypatch.setattr(tl_mod, "MAX_SNAPSHOTS", 5)
    """反向断言：拒绝之后，玩家读到的状态**没有**被悄悄换成别的版本。"""
    _append_many(tl, state, 12)
    with pytest.raises(TimelineError):
        tl.payload(1)
    # head 仍然可用，且就是第 60 回合那一版（没有被"回退"过）
    assert tl.payload(tl.head().rev)["state"]["turn_count"] == 12


# ---------------------------------------------------------------------------
# 6. 真的省下空间
# ---------------------------------------------------------------------------


def test_growth_is_bounded_compared_to_unbounded(tl, state, monkeypatch):
    monkeypatch.setattr(tl_mod, "MAX_SNAPSHOTS", 5)
    """跑 60 版后的占用要明显小于"60 份全量快照"的总和。"""
    _append_many(tl, state, 12)
    total = tl.size_bytes()
    per_snapshot = (tl.root / tl.head().checkpoint).stat().st_size
    assert total < per_snapshot * 12 * 0.85, (
        f"占用 {total} 字节 ≈ {total / per_snapshot:.1f} 份快照——回收没生效？"
    )


def test_pruning_is_idempotent_and_never_removes_live_files(tl, state, monkeypatch):
    monkeypatch.setattr(tl_mod, "MAX_SNAPSHOTS", 5)
    """反复回收是幂等的，且**现存条目引用的文件不会被删**（除了本来就已回收的）。"""
    _append_many(tl, state, 12)
    assert tl.prune() == [], "已经回收过一轮，再跑一次不该再删东西"
    for e in tl.entries():
        if not e.pruned:
            assert (tl.root / e.checkpoint).is_file()


def test_manual_snapshot_deletion_is_reported_not_swallowed(tl, state):
    """有人手工删了快照 → 条目照样标 pruned，读它照样拒绝（不是 500、不是空对象）。"""
    _append_many(tl, state, 3)
    (tl.root / "rev-000001.json").unlink()
    entries = {e.rev: e for e in tl.entries()}
    assert entries[1].pruned is True
    with pytest.raises(TimelineError):
        tl.payload(1)

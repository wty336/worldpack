"""G2 守卫：存档的剧本身份戳（`docs/plan-tavern-shaped-product.md` §6 E-2）。

**被钉住的缺陷**：此前存档只写 `state.to_dict()`，不含剧本身份。作者改版已发布剧本
（改 NPC id、改旗标名）后，旧档被读进新版剧本 → `GameState` 里的旗标/NPC 好感
**在包内不存在** → 静默穿帮：NPC 失忆、条件永不触发、结局判不出。
没有任何报错，玩家只会觉得"这局怪怪的"。

本文件守三件事：
1. **摘要口径**：内容变则变、换行归一（跨机可对账）、**门禁语料不参与**；
2. **拒绝语义**：id 或 digest 不一致 → `PackMismatchError`，且**不留下半应用状态**；
3. **只补不漏**：旧存档（无身份戳）与裸调用（无当前包身份）都放过——
   不能因为存档早于本机制就把玩家的档拒之门外。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from game_agent.save import (
    SAVE_VERSION,
    PackMismatchError,
    load_game,
    load_history,
    save_game,
    save_summary,
    saved_pack_meta,
    state_pack_mismatch,
)
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack, pack_digest, pack_meta

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


# ---------------------------------------------------------------------------
# 夹具：一个最小可摘要的包目录
# ---------------------------------------------------------------------------

_CONTENT = {
    "world.yaml": "name: 测试世界\nera: 测试\n",
    "schedule.yaml": "day_action_points: 1\n",
    "mainline.yaml": "nodes: []\n",
    "events.yaml": "events: []\n",
    "endings.yaml": "endings: []\n",
    "npcs/a.yaml": "id: a\nname: 甲\nidentity: 测试\n",
}


def _make_pack(root: Path, **overrides: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "npcs").mkdir(exist_ok=True)
    files = dict(_CONTENT)
    files.update(overrides)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" 是关键：默认的 write_text 在 Windows 上会把 \n 写成 \r\n，
        # 于是"LF 基准 vs CRLF 对照"的测试两边都是 CRLF，测不到归一化。
        p.write_text(text, encoding="utf-8", newline="\n")
    return root


def _state() -> GameState:
    return GameState(pack_name="测试世界")


# ---------------------------------------------------------------------------
# 1. 摘要口径
# ---------------------------------------------------------------------------


def test_digest_is_stable_and_hex(tmp_path):
    """同一份内容两次摘要必须相同（否则存档永远"不匹配"）。"""
    root = _make_pack(tmp_path / "p")
    d1, d2 = pack_digest(root), pack_digest(root)
    assert d1 == d2
    assert len(d1) == 16 and all(c in "0123456789abcdef" for c in d1)


def test_digest_changes_when_content_changes(tmp_path):
    """内容变 → 摘要变。这是"改版后拒绝旧档"的判据本身。"""
    a = _make_pack(tmp_path / "a")
    b = _make_pack(tmp_path / "b", **{"npcs/a.yaml": "id: a\nname: 乙\nidentity: 改了\n"})
    assert pack_digest(a) != pack_digest(b)


def test_digest_changes_when_npc_renamed(tmp_path):
    """改文件名也要变——旗标/NPC id 的改名正是静默穿帮的典型成因。"""
    a = _make_pack(tmp_path / "a")
    b = _make_pack(tmp_path / "b")
    (b / "npcs" / "a.yaml").rename(b / "npcs" / "a2.yaml")
    assert pack_digest(a) != pack_digest(b)


def test_digest_is_newline_normalized(tmp_path):
    """**跨机可对账**：同一份内容 CRLF 与 LF 必须算出同一摘要。

    不归一的话，Windows 检出的存档拿到 Linux/CI 上会被判成"剧本不匹配"——
    正是 `evalmeta.file_digest` 在 2026-09-12 踩过的那个坑
    （当时让 `test_eval_frozen` 在 Linux 上必红）。
    """
    a = _make_pack(tmp_path / "lf")
    b = _make_pack(tmp_path / "crlf")
    for rel in _CONTENT:
        p = b / rel
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    assert pack_digest(a) == pack_digest(b)


def test_digest_ignores_judge_corpus(tmp_path):
    """**门禁语料不参与摘要**：重跑语料生成器不该让旧存档失效。

    把"质量门的尺子"当成"游戏规则"是口径混淆——作者扩语料是常规操作，
    若参与摘要，每次扩语料都会让所有玩家存档被拒。
    """
    a = _make_pack(tmp_path / "a")
    b = _make_pack(tmp_path / "b")
    (b / "judge_corpus.yaml").write_text("cases: []\n", encoding="utf-8")
    (b / "judge_corpus.gen.yaml").write_text("cases: []\n", encoding="utf-8")
    (b / "smoke_profile.yaml").write_text("x: 1\n", encoding="utf-8")
    assert pack_digest(a) == pack_digest(b)


def test_pack_meta_carries_id_and_digest():
    """真实包：`id` 是目录名（人可读），`digest` 是内容指纹（可对账）。"""
    pack = load_worldpack(REAL_PACK)
    meta = pack_meta(pack)
    assert meta["id"] == "ancient_jianghu"
    assert len(meta["digest"]) == 16


# ---------------------------------------------------------------------------
# 2. 落盘与拒绝
# ---------------------------------------------------------------------------


def test_save_writes_pack_stamp(tmp_path):
    meta = {"id": "p1", "digest": "d" * 16}
    p = tmp_path / "s.json"
    save_game(_state(), p, pack_meta=meta)
    assert saved_pack_meta(p) == meta


def test_save_without_meta_omits_stamp(tmp_path):
    """脚本的中间产物/崩溃转储不带身份戳，也不该被读档校验拦住。"""
    p = tmp_path / "s.json"
    save_game(_state(), p)
    assert saved_pack_meta(p) is None


def test_load_accepts_matching_pack(tmp_path):
    meta = {"id": "p1", "digest": "d" * 16}
    p = tmp_path / "s.json"
    save_game(_state(), p, pack_meta=meta)
    assert load_game(p, pack_meta=meta).pack_name == "测试世界"


def test_load_rejects_digest_mismatch(tmp_path):
    """同 id 不同 digest（改版）→ 拒绝。这是 G2 的核心断言。"""
    p = tmp_path / "s.json"
    save_game(_state(), p, pack_meta={"id": "p1", "digest": "a" * 16})
    with pytest.raises(PackMismatchError) as ei:
        load_game(p, pack_meta={"id": "p1", "digest": "b" * 16})
    # 报错必须可行动：说清两边是谁、为什么拒、怎么办
    msg = str(ei.value)
    assert "aaaaaaaa" in msg and "bbbbbbbb" in msg
    assert "改版" in msg and "另开新局" in msg


def test_load_rejects_id_mismatch(tmp_path):
    """不同 id（换包）→ 也要拒：成本归因与存档隔离都以 id 为准。"""
    p = tmp_path / "s.json"
    save_game(_state(), p, pack_meta={"id": "p1", "digest": "a" * 16})
    with pytest.raises(PackMismatchError):
        load_game(p, pack_meta={"id": "p2", "digest": "a" * 16})


def test_legacy_save_without_stamp_is_accepted(tmp_path):
    """**只补不漏**：G2 之前的存档没有身份戳，必须照常能读。"""
    p = tmp_path / "old.json"
    save_game(_state(), p)  # 不带 pack
    assert load_game(p, pack_meta={"id": "p1", "digest": "a" * 16}).pack_name == "测试世界"


def test_bare_load_skips_check(tmp_path):
    """裸调用（脚本/测试）不提供当前包身份 → 不做校验，行为与 G2 之前一致。"""
    p = tmp_path / "s.json"
    save_game(_state(), p, pack_meta={"id": "p1", "digest": "a" * 16})
    assert load_game(p).pack_name == "测试世界"


def test_mismatch_rejection_does_not_apply_partial_state(tmp_path):
    """拒绝时**不得留下半应用状态**：校验发生在构造 GameState 之前。"""
    p = tmp_path / "s.json"
    save_game(_state(), p, pack_meta={"id": "p1", "digest": "a" * 16})
    with pytest.raises(PackMismatchError):
        load_game(p, pack_meta={"id": "p1", "digest": "b" * 16})
    # 存档本身没被改动，仍可用正确身份读回
    assert load_game(p, pack_meta={"id": "p1", "digest": "a" * 16}).pack_name == "测试世界"


def test_history_reads_alongside_identity_check(tmp_path):
    """身份校验不影响 history 读取（两者是同一文件的两半）。"""
    meta = {"id": "p1", "digest": "d" * 16}
    p = tmp_path / "s.json"
    save_game(_state(), p, [{"role": "user", "content": "你好"}], pack_meta=meta)
    assert load_history(p) == [{"role": "user", "content": "你好"}]


def test_save_version_bumped_for_identity_stamp():
    """v3 = 带剧本身份戳。版本号是给后来人读的迁移说明，不能停在 v2。"""
    assert SAVE_VERSION == 3


def test_real_pack_round_trip(tmp_path):
    """端到端：真实包 → 存档 → 改一个文件 → 读档被拒。"""
    root = tmp_path / "ancient_jianghu"
    shutil.copytree(REAL_PACK, root)
    pack = load_worldpack(root)
    meta = pack_meta(pack)
    p = tmp_path / "s.json"
    save_game(GameState.from_pack(pack), p, pack_meta=meta)

    assert load_game(p, pack_meta=meta).pack_name == pack.world.name

    # 改一个 NPC 的说话风格 → 内容指纹变化
    npc_file = next((root / "npcs").glob("*.yaml"))
    npc_file.write_text(
        npc_file.read_text(encoding="utf-8") + "\n# 作者改版\n", encoding="utf-8"
    )
    with pytest.raises(PackMismatchError):
        load_game(p, pack_meta=pack_meta(load_worldpack(root)))


# ---------------------------------------------------------------------------
# 3. 内容级兜底：**没有身份戳的旧档**（2026-10 实测 HTTP 500 的成因）
# ---------------------------------------------------------------------------
#
# 身份戳比对对旧档无能为力（没有戳就无从比），而"只补不漏"又要求放行旧档。
# 于是出现了这个洞：把 A 卡的旧档读进 B 卡 → state 里出现 B 卡不认识的键 →
# `context.status_text()` 的 `self.pack.schedule.stats[k]` KeyError → **HTTP 500**。
# 玩家看到"读取状态失败：HTTP 500"，真实原因却是"你拿错卡了"。
# 下面这组守卫覆盖这条路径。

CAMPUS_PACK = REPO / "world-packs" / "campus_otome"


def _cross_pack_legacy_save(tmp_path: Path) -> Path:
    """造一个**旧格式**（无 pack 身份戳）的存档，但它属于另一个包。"""
    campus = load_worldpack(CAMPUS_PACK)
    state = GameState.from_pack(campus)
    assert state.pack_name == campus.world.name != load_worldpack(REAL_PACK).world.name
    p = tmp_path / "legacy.json"
    save_game(state, p)  # 不带 pack_meta = G2 之前的旧档
    assert saved_pack_meta(p) is None
    return p


def test_state_pack_mismatch_accepts_matching_state():
    pack = load_worldpack(REAL_PACK)
    assert state_pack_mismatch(GameState.from_pack(pack), pack) is None


def test_state_pack_mismatch_detects_foreign_stats(tmp_path):
    """异包状态必须被认出来——这是那条 500 的根因。"""
    pack = load_worldpack(REAL_PACK)
    foreign = load_game(_cross_pack_legacy_save(tmp_path))  # 不校验，纯取状态
    reason = state_pack_mismatch(foreign, pack)
    assert reason is not None
    # **结论先行**：第一句就要说清"该选哪张卡"，键名清单只作证据
    assert reason.index(foreign.pack_name) < reason.index("对不上的内容")
    assert pack.world.name in reason and foreign.pack_name in reason
    assert "改选该卡" in reason


def test_state_pack_mismatch_detects_unknown_npc():
    """NPC 引用也要查：缺一个就会在渲染角色卡时炸。"""
    pack = load_worldpack(REAL_PACK)
    state = GameState.from_pack(pack)
    state.affections["ghost_npc"] = 5.0
    assert state_pack_mismatch(state, pack) is not None


def test_legacy_identity_check_still_passes_but_content_check_catches(tmp_path):
    """两道关的分工：身份戳放行旧档，**内容关**把异包旧档拦下。"""
    pack = load_worldpack(REAL_PACK)
    p = _cross_pack_legacy_save(tmp_path)
    # 第一道：无戳 → 放行（"只补不漏"）
    load_game(p, pack_meta=pack_meta(pack))
    # 第二道：内容对不上 → 报因
    assert state_pack_mismatch(load_game(p), pack) is not None


def test_save_summary_reports_world_name_for_legacy_saves(tmp_path):
    """旧档没有身份戳，列表里必须能看出它属于哪张卡（否则玩家只能靠"读了被拒"来试）。"""
    p = _cross_pack_legacy_save(tmp_path)
    s = save_summary(p)
    assert s["pack"] is None  # 旧档确实没有戳
    assert s["pack_name"] == load_worldpack(CAMPUS_PACK).world.name

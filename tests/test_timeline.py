"""N7 / G-2 守卫：存档点 · 回退 · 分支（把 runlog 的回合级 checkpoint 产品化）。

上游：`docs/plan-dsh-tavern-parity.md` 期 2 / 差距 **G-2（P0）**、`docs/roadmap.md` **N7**。

**被钉住的缺口**：`runlog` 早就有回合级全量 checkpoint（`state.to_dict()` + history + rng），
但它是**事后实验设施**——写在 gitignore 的 `runs/` 下、由驱动脚本调用，
而且按 **turn** 命名。玩家面一个入口都没有：玩错了只能读档，回不到上一回合。

本文件守五类性质（前四条直接来自对标文档里"dsh-tavern 踩过坑的结论"）：

1. **revision 只增不减**——回退**也**产生新版本，而不是把指针挪回去；
2. **回退即开分支，旧分支原地不动**——保留可查，且没有读取路径会碰它；
3. **state / history / rng 三者一起还原**——少还原任何一样，回退后的世界都会"变了样"；
4. **派生失败不阻塞**——存档点写盘失败不得让已经提交的回合失败（ADR 0006 同款纪律）；
5. **按 rev 而不是按 turn 命名**——这是 `runlog` 不能直接复用的**唯一**原因：
   按 turn 存的话，回退到第 5 回合再玩到第 6 回合会把旧的 `000006.json` **覆盖掉**，
   而玩家以为自己开了个分支。
"""

from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fakes import FakeClient, msg, resp, tool_call

import game_agent.web as web
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.timeline import Timeline, TimelineError, timeline_root
from game_agent.worldpack import load_worldpack

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


def _submit(narration: str, choices: list[str], cid: str = "s1"):
    return tool_call(cid, "submit_narration",
                     {"narration": narration, "choices": choices, "plot_signal": "normal"})


@pytest.fixture
def pack():
    return load_worldpack(REAL_PACK)


@pytest.fixture
def tl(tmp_path) -> Timeline:
    return Timeline(tmp_path / "timeline-probe")


def _state(pack, **kw) -> GameState:
    st = GameState.from_pack(pack)
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# ---------------------------------------------------------------------------
# 1. revision 只增不减
# ---------------------------------------------------------------------------


def test_revisions_never_go_backwards(tl, pack):
    """**核心语义**：回退产生的是**新版本**（rev = max+1），不是把指针挪回去。

    为什么这条是核心而不是实现细节：`rev` 若可以回退，就会出现"同一个 rev 先后
    指向两份不同内容"，于是"上周那一版到底长什么样"再也答不出来——
    审计性直接没了。dsh-tavern 的 `CONTEXT.md` 把这条写死了，直接采用。
    """
    st = _state(pack)
    e1 = tl.append(state=st, history=[{"role": "assistant", "content": "开场"}],
                   rng=random.Random(1), label="开局")
    st.turn_count = 2
    e2 = tl.append(state=st, history=[], rng=random.Random(1), label="你说：你好")
    assert (e1.rev, e2.rev) == (1, 2)

    w = tl.rewind_to(e1.rev)
    assert w.rev == 3, "回退必须产生新版本（rev 只增不减）"
    assert w.parent == e1.rev, "要记下它从哪来"
    assert [e.rev for e in tl.entries()] == [1, 2, 3], "追加序列，不覆盖"


def test_rewind_to_current_head_is_a_noop(tl, pack):
    """回退到"就是当前这一版"不产生垃圾版本（否则列表会被空操作灌满）。"""
    st = _state(pack)
    e1 = tl.append(state=st, history=[], rng=random.Random(1), label="开局")
    again = tl.rewind_to(e1.rev)
    assert again.rev == e1.rev
    assert len(tl.entries()) == 1


def test_unknown_rev_is_rejected_with_a_readable_message(tl, pack):
    tl.append(state=_state(pack), history=[], rng=random.Random(1), label="开局")
    with pytest.raises(TimelineError) as e:
        tl.rewind_to(99)
    assert "没有 rev 99" in str(e.value)
    assert "现有" in str(e.value), "拒绝时要说清现有哪些版本"


# ---------------------------------------------------------------------------
# 2. 回退即开分支；旧分支原地不动
# ---------------------------------------------------------------------------


def test_rewind_opens_a_new_branch_and_leaves_the_old_one_untouched(tl, pack):
    """旧分支**一个字节都不动**——这是"分支"而不是"覆盖"的判据。"""
    st = _state(pack)
    e1 = tl.append(state=st, history=[], rng=random.Random(1), label="开局")
    st.turn_count = 2
    e2 = tl.append(state=st, history=[], rng=random.Random(1), label="第二回合")
    before = {e.rev: (e.branch, e.checkpoint, e.parent) for e in tl.entries()}
    old_files = {p.name: p.read_bytes() for p in tl.root.glob("rev-*.json")}

    w = tl.rewind_to(e1.rev)
    assert w.branch == "b2" and tl.current_branch() == "b2"
    assert tl.branches() == ["b1", "b2"]

    after = {e.rev: (e.branch, e.checkpoint, e.parent) for e in tl.entries() if e.rev <= 2}
    assert after == before, "回退改动了旧条目——那不是开分支，是覆盖"
    assert {p.name: p.read_bytes() for p in tl.root.glob("rev-*.json")} == old_files

    # 继续玩 → 写在新分支上，旧分支仍然只有它原来那两版
    st.turn_count = 3
    e4 = tl.append(state=st, history=[], rng=random.Random(1), label="新分支继续")
    assert e4.branch == "b2"
    assert [e.branch for e in tl.entries()] == ["b1", "b1", "b2", "b2"]


def test_rewind_reuses_the_target_snapshot_instead_of_copying(tl, pack):
    """回退派生**复用**目标快照而不是复制：状态逐字节相同，复制只是放两份一样的东西。"""
    st = _state(pack)
    e1 = tl.append(state=st, history=[], rng=random.Random(1), label="开局")
    w = tl.rewind_to(e1.rev)
    assert w.checkpoint == e1.checkpoint
    files = sorted(p.name for p in tl.root.glob("rev-*.json"))
    assert files == ["rev-000001.json"], f"不该多出快照文件：{files}"


# ---------------------------------------------------------------------------
# 3. state / history / rng 三者一起还原
# ---------------------------------------------------------------------------


def test_payload_roundtrips_state_history_and_rng(tl, pack):
    """快照必须带 rng：不记的话回退后的世界会从另一个随机流继续长。"""
    rng = random.Random(20261001)
    rng.random()  # 让状态不是初始值（否则"还原成功"可能只是巧合）
    st = _state(pack, turn_count=4, day=3)
    st.stats["martial"] = 33.0
    st.flags["met_lin"] = True
    tl.append(state=st, history=[{"role": "assistant", "content": "第四回合"}],
              rng=rng, label="第四回合")

    payload = tl.payload(1)
    assert payload["state"]["turn_count"] == 4
    assert payload["state"]["stats"]["martial"] == 33.0
    assert payload["history"][0]["content"] == "第四回合"
    assert "rng_state" in payload, "没有 rng 状态 → 回退后随机流会变"

    # rng 状态的 JSON 回环：必须是可还原的（元组 → 列表 → 元组，见 runlog.rebuild_game）
    version, keys, gauss = payload["rng_state"]
    restored = random.Random()
    restored.setstate((version, tuple(keys), tuple(gauss) if gauss is not None else None))
    assert restored.random() == rng.random(), "还原后的随机流与原来不一致"


def test_rebuilt_game_matches_the_target_revision(pack):
    """回退后重建的 `Game`：state / history / rng 与目标版本一致。"""
    from game_agent.runlog import rebuild_game

    rng = random.Random(7)
    st = _state(pack, turn_count=5, day=3)
    st.stats["martial"] = 21.0
    history = [{"role": "assistant", "content": "第五回合正文"}]

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tl = Timeline(Path(d) / "tl")
        tl.append(state=st, history=history, rng=rng, label="第五回合")
        payload = tl.payload(1)
        llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
        game = rebuild_game(pack, payload["state"], payload["history"], llm,
                            payload.get("rng_state"))
    assert game.state.turn_count == 5 and game.state.day == 3
    assert game.state.stats["martial"] == 21.0
    assert game.history == history
    assert game.rng.random() == rng.random(), "重建后的 rng 必须接上原来的流"


# ---------------------------------------------------------------------------
# 4. HTTP 面：端点契约
# ---------------------------------------------------------------------------


@pytest.fixture
def live(tmp_path, monkeypatch):
    """一个跑在临时世界包 + 临时 saves 上的 Web 客户端（离线假 LLM，不花钱）。"""
    root = tmp_path / "world-packs"
    root.mkdir(parents=True)
    shutil.copytree(REAL_PACK, root / "probe")
    monkeypatch.setattr(web, "_pack_root", lambda: root)
    monkeypatch.setattr(web, "SAVE_ROOT", tmp_path / "saves")
    monkeypatch.setattr(web, "SESSIONS", {})
    monkeypatch.setattr(web, "load_settings", lambda: _FakeSettings())

    # 每轮给一条合法协议输出（够走几个回合）
    def _llm_factory():
        return LLMClient(
            FakeClient([resp(msg(tool_calls=[_submit("正文。", ["甲", "乙", "丙"], f"s{i}")]))
                        for i in range(40)]),
            "fake", build_tools(load_worldpack(root / "probe").schedule),
        )

    real_make = web._make_game

    def fake_make(sid, pack_id=None, *, draft=None, mainline_enabled=True):
        pack = load_worldpack(root / "probe")
        game = Game(pack, GameState.from_pack(pack), _llm_factory(),
                    autosave_path=str(tmp_path / "saves" / f"autosave-{sid}.json"),
                    mainline_enabled=mainline_enabled, **web._game_options())
        return game, web.UsageTracker(str(tmp_path / "saves" / f"usage-{sid}.jsonl"), session=sid)

    monkeypatch.setattr(web, "_make_game", fake_make)
    monkeypatch.setattr(web, "_game_options", lambda: {})  # 假 Game 不吃这些选项
    del real_make
    client = TestClient(web.app)
    sid = client.post("/api/new", json={"pack_id": "probe"}).json()["sid"]
    # 该包**开局即关键抉择**（`n1_first_meeting` 的 `how_to_help`）→ 先选一次，
    # 否则后续 `say`/`act` 会被引擎以"此刻是关键抉择"拒绝（这也正是 N7 顺手修掉的
    # 那个卡死 bug 的同一条规则）。选完这一版也就成了时间线的 rev 1。
    r = client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
    assert "event: done" in r.text, r.text[:200]
    return client, sid, tmp_path / "saves"


class _FakeSettings:
    has_api_key = True


def _turn(client, sid, text="我上前一步。"):
    r = client.post(f"/api/{sid}/turn", json={"kind": "say", "text": text})
    assert r.status_code == 200, r.text
    return r


def test_timeline_endpoint_lists_every_revision(live):
    """每回合自动记一版；列表新的在前，且标出当前版本与分支。"""
    client, sid, saves = live
    _turn(client, sid, "第一句")
    _turn(client, sid, "第二句")

    d = client.get(f"/api/{sid}/timeline").json()
    assert d["ok"] is True
    assert [e["rev"] for e in d["entries"]] == [d["current"], *(e["rev"] for e in d["entries"][1:])]
    assert d["entries"][0]["rev"] == d["current"], "新的应当排在最前"
    assert d["current_branch"] == "b1"
    assert d["size_bytes"] > 0
    labels = [e["label"] for e in d["entries"]]
    assert any("第一句" in x for x in labels), labels
    del saves


def test_history_endpoint_returns_prose_for_rebuilding(live):
    """正文此前只活在前端内存里——刷新一次故事栏就空了。这个端点补上那一半。"""
    client, sid, _ = live
    _turn(client, sid, "我上前一步。")
    d = client.get(f"/api/{sid}/history").json()
    texts = [e["text"] for e in d["entries"]]
    assert any("正文。" in t for t in texts), texts
    assert any("我上前一步" in t for t in texts), "玩家自己的话也要在（否则重建后上下文断裂）"
    # 引擎元消息**不得**回放到故事栏
    assert not any("引擎熔断" in t or "[时序推进]" in t for t in texts)


def test_rewind_endpoint_rebuilds_and_opens_a_branch(live):
    """端到端：玩两回合 → 回到第一版 → 后端开了新分支且返回重建后的正文。"""
    client, sid, saves = live
    _turn(client, sid, "第一句")
    _turn(client, sid, "第二句")
    before = client.get(f"/api/{sid}/timeline").json()
    first = min(e["rev"] for e in before["entries"])

    r = client.post(f"/api/{sid}/rewind", json={"rev": first})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rev"] > before["current"], "回退必须产生更大的 rev"
    assert d["branch"] != before["current_branch"], "回退必须开新分支"
    assert d["parent"] == first
    assert isinstance(d["history"], list)

    after = client.get(f"/api/{sid}/timeline").json()
    assert after["current"] == d["rev"]
    assert after["current_branch"] == d["branch"]
    assert after["current_branch"] in after["branches"]
    # 旧分支的条目仍在（保留可查）
    assert before["branches"][0] in after["branches"]
    del saves


def test_rewind_rejects_unknown_rev_with_400(live):
    client, sid, _ = live
    _turn(client, sid)
    r = client.post(f"/api/{sid}/rewind", json={"rev": 999})
    assert r.status_code == 400
    assert "没有 rev 999" in r.json()["detail"]


def test_state_really_goes_back(live):
    """**端到端的行为断言**：回退之后 `turn_count` 真的退回去了，而新版本更大。"""
    client, sid, _ = live
    _turn(client, sid, "第一句")
    t1 = client.get(f"/api/{sid}/meta").json()["turn"]
    _turn(client, sid, "第二句")
    t2 = client.get(f"/api/{sid}/meta").json()["turn"]
    assert t2 > t1

    tl = client.get(f"/api/{sid}/timeline").json()["entries"]
    first = min(e["rev"] for e in tl)
    r = client.post(f"/api/{sid}/rewind", json={"rev": first}).json()
    assert r["turn"] == client.get(f"/api/{sid}/meta").json()["turn"], "返回值与真值要一致"
    assert r["turn"] <= t1, f"回退后回合数应当退回去（现在 {r['turn']}，第二句后是 {t2}）"


# ---------------------------------------------------------------------------
# 5. 派生失败不阻塞（ADR 0006 同款纪律）
# ---------------------------------------------------------------------------


def test_checkpoint_failure_does_not_fail_the_turn(live, monkeypatch):
    """存档点写盘失败 → **回合照常成功**，只给一句提示。

    这条挡的是"派生设施把前台拖下水"：正文已经落盘、状态已经提交，
    此时让整轮 500 等于告诉玩家"你这一回合白玩了"——而它其实玩成了。
    """
    client, sid, _ = live

    def boom(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(web.timeline.Timeline, "append", boom)
    r = client.post(f"/api/{sid}/turn", json={"kind": "say", "text": "第一句"})
    assert r.status_code == 200, "回合不该因为存档点失败而失败"
    assert "event: done" in r.text, "done 帧必须照发（正文与状态都已提交）"
    assert "没能记入时间线" in r.text, "但要如实提示（不静默）"
    # 状态确实提交了
    assert client.get(f"/api/{sid}/meta").json()["turn"] >= 1


def test_timeline_isolated_per_session(live):
    """时间线按会话隔离（与 `usage-<sid>.jsonl` 同一条纪律：不共享、不串号）。"""
    client, sid, saves = live
    _turn(client, sid)
    other = client.post("/api/new", json={"pack_id": "probe"}).json()["sid"]
    assert other != sid
    assert client.get(f"/api/{other}/timeline").json()["entries"] == []
    assert client.get(f"/api/{sid}/timeline").json()["entries"]
    del saves


def test_timeline_root_rejects_path_traversal():
    """sid 是客户端可控字符串的邻居——**绝不参与路径拼接**（与 `_safe_save_path` 同姿态）。"""
    for evil in ("../x", "a/b", "a\\b", "", ".", ".."):
        with pytest.raises(TimelineError):
            timeline_root(evil)


def test_corrupt_index_is_not_silently_skipped(tmp_path):
    """索引某行坏掉时**必须报错**，不能悄悄跳过——跳过等于少一段历史而玩家看不出来。"""
    tl = Timeline(tmp_path / "tl")
    tl.root.mkdir(parents=True)
    tl.index_path.write_text(
        json.dumps({"rev": 1, "branch": "b1", "parent": None, "turn": 1, "day": 1,
                    "ts": "", "label": "x", "checkpoint": "rev-000001.json"},
                   ensure_ascii=False) + "\n{ 这不是 json\n",
        encoding="utf-8",
    )
    with pytest.raises(TimelineError) as e:
        tl.entries()
    assert "第 2 行损坏" in str(e.value)

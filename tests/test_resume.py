"""N13 守卫：**跨重启续玩**（从磁盘时间线重新打开一局）。

上游：`docs/roadmap.md` **N7 的已知边界**（"服务器重启后不能续玩时间线"）→ N13。

**被钉住的缺口**：会话活在进程内存里（`SESSIONS`），服务一停就没了。玩家关掉终端、
第二天回来，那一局就"消失"了——存档还在，但读档是另一条体验（回到存档点，不是接着
刚才那一回合）。而磁盘上其实什么都在：`saves/timeline-<sid>/` 有每一版的
state + history + rng，缺的只是"这局属于哪张卡、什么模式"这一层身份。

守七件事：

1. **不需要会话也能列出来**：进程刚起、`SESSIONS` 空着时，盘上的局要看得见；
2. **从 head 继续** = 接着那一回合（同一目录、rev 继续往上走）；
3. **从旧版继续** = 回退语义（开新分支、旧线一字节不动）；
4. **模式不丢**：自由探索的局重启之后还是自由探索（不许悄悄变回主线）；
5. **包漂移要拒绝**：卡改过之后旧局不通用（与读档 G2 同一口径），且错误能照做；
6. **旧快照有兜底但不猜**：没有身份戳的旧时间线按世界名**唯一匹配**才认；
7. **幂等**：这一局已经在内存里跑着时，再点"继续"不许把进行中的会话顶掉。
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from fakes import FakeClient, msg, resp, tool_call

import game_agent.web as web
from game_agent import timeline as tl_mod
from game_agent.llm import LLMClient
from game_agent.worldpack import load_worldpack

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


def _submit(narration: str, choices: list[str], cid: str = "s1"):
    return tool_call(cid, "submit_narration",
                     {"narration": narration, "choices": choices, "plot_signal": "normal"})


@pytest.fixture
def live(tmp_path, monkeypatch):
    """一个真实 `_make_game` 的 Web 客户端（离线假 LLM）+ 可清空的会话表。"""
    root = tmp_path / "world-packs"
    root.mkdir()
    shutil.copytree(REAL_PACK, root / "probe")
    saves = tmp_path / "saves"
    monkeypatch.setattr(web, "_pack_root", lambda: root)
    monkeypatch.setattr(web, "SAVE_ROOT", saves)
    monkeypatch.setattr(web, "SESSIONS", {})
    monkeypatch.setattr(web, "load_settings", lambda: SimpleNamespace(has_api_key=True))
    monkeypatch.setattr(web, "_game_options", lambda: {})

    def fake_from_settings(settings, tools, tracker=None):
        replies = [
            resp(msg(tool_calls=[_submit(f"第{i}段正文。", ["甲", "乙", "丙"], f"s{i}")]))
            for i in range(30)
        ]
        return LLMClient(FakeClient(replies), "fake", tools)

    monkeypatch.setattr(web.LLMClient, "from_settings", staticmethod(fake_from_settings))
    return SimpleNamespace(client=TestClient(web.app), saves=saves, root=root)


def _open(live, mode: str = "story") -> str:
    d = live.client.post("/api/new", json={"pack_id": "probe", "mode": mode}).json()
    sid = d["sid"]
    if d["view"].get("choice_prompt"):
        r = live.client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
        assert "event: done" in r.text, r.text[:200]
    return sid


def _say(live, sid: str, text: str = "我上前一步。") -> None:
    r = live.client.post(f"/api/{sid}/turn", json={"kind": "say", "text": text})
    assert r.status_code == 200 and "event: done" in r.text, r.text[-300:]


def _restart(live) -> None:
    """模拟服务重启：进程内会话全没了，但磁盘上的一切原样保留。"""
    web.SESSIONS.clear()


# ---------------------------------------------------------------------------
# 1. 盘上列得出来（不需要会话）
# ---------------------------------------------------------------------------


def test_timelines_are_listed_from_disk_without_a_session(live):
    sid = _open(live)
    _say(live, sid)
    head_rev = web._timeline(sid).head().rev
    _restart(live)

    d = live.client.get("/api/timelines").json()
    rows = {r["sid"]: r for r in d["timelines"]}
    assert sid in rows, d
    row = rows[sid]
    assert row["pack_id"] == "probe"
    assert row["turn"] >= 1 and row["day"] >= 1
    assert row["rev"] == head_rev
    assert row["resumable"] is True, row
    assert row["live"] is False
    assert d["ok"] is True


def test_snapshot_carries_pack_and_mode(live):
    """新快照必须带 `pack` / `mainline`——否则重启之后答不出"哪张卡、什么模式"。"""
    sid = _open(live, mode="free")
    payload = web._timeline(sid).payload(web._timeline(sid).head().rev) if web._timeline(sid).head() else None
    if payload is None:  # 还没有回合 → 手工记一版（开局本身不建点）
        _say(live, sid)
        payload = web._timeline(sid).payload(web._timeline(sid).head().rev)
    assert payload["pack"]["id"] == "probe"
    assert payload["pack"]["digest"], "身份戳必须含 digest（包漂移靠它判）"
    assert payload["mainline"] is False, "自由探索模式没落盘"


# ---------------------------------------------------------------------------
# 2/3. 从 head 继续 / 从旧版继续
# ---------------------------------------------------------------------------


def test_resume_at_head_continues_the_same_timeline(live):
    sid = _open(live)
    _say(live, sid)
    rev_before = web._timeline(sid).head().rev
    turns_before = web.SESSIONS[sid].game.state.turn_count
    _restart(live)

    d = live.client.post(f"/api/{sid}/resume").json()
    assert d["ok"] is True and d["sid"] == sid
    assert d["pack_id"] == "probe"
    assert d["mode"] == "story"
    assert d["rev"] == rev_before, "从 head 继续不该凭空多出版本"
    assert d["turn"] == turns_before
    assert web.SESSIONS[sid].game.state.turn_count == turns_before
    # 继续玩：同一目录里 rev 往上走
    _say(live, sid, "我继续。")
    assert web._timeline(sid).head().rev > rev_before
    assert len(web._timeline(sid).entries()) >= 2


def test_resume_restores_history_and_last_choices(live):
    """续玩第一屏要有**正文历史**与**上一回合的候选项**（不是"只能自由输入"）。"""
    sid = _open(live)
    _say(live, sid, "我递上一枚玉佩。")
    _restart(live)

    d = live.client.post(f"/api/{sid}/resume").json()
    texts = [e["text"] for e in d["history"]]
    assert any("第" in t and "段正文" in t for t in texts), texts
    assert "我递上一枚玉佩。" in texts
    assert d["view"]["choices"][:3] == ["甲", "乙", "丙"], d["view"]["choices"]
    # **视图不带正文**：正文由 history 重建，再给一遍会让玩家看到同一段出现两次
    # （真机冒烟抓到过：两个"最后一段正文"挨着）。这条断言就是那次修复的墓碑。
    assert d["view"]["narration"] is None, d["view"]["narration"]
    # 历史里 engine 注入的元消息不回放（与刷新/回退同一口径）
    assert not any("agent_status" in t for t in texts)


def test_resume_from_an_older_rev_is_a_rewind(live):
    sid = _open(live)
    tl = web._timeline(sid)
    first_rev = tl.head().rev
    _say(live, sid)
    later_rev = tl.head().rev
    assert later_rev > first_rev
    entries_before = len(tl.entries())
    _restart(live)

    d = live.client.post(f"/api/{sid}/resume", json={"rev": first_rev}).json()
    assert d["ok"] is True
    assert d["rev"] > later_rev, "从旧版继续 = 新版本（rev 只增不减）"
    assert d["branch"] != "b1", "回退要开新分支"
    after = web._timeline(sid)
    assert len(after.entries()) == entries_before + 1, "只新增一条，旧条目一字节不动"
    # 旧线还在（旧 rev 仍可读）
    assert (after.root / after._entry(later_rev).checkpoint).is_file()


def test_free_mode_survives_a_restart(live):
    """自由探索的局重启之后还是自由探索——不能悄悄变回主线。"""
    sid = _open(live, mode="free")
    _say(live, sid)
    _restart(live)
    d = live.client.post(f"/api/{sid}/resume").json()
    assert d["mode"] == "free"
    assert web.SESSIONS[sid].game.mainline_enabled is False


# ---------------------------------------------------------------------------
# 4/5. 拒绝的三种情形（都要能照做）
# ---------------------------------------------------------------------------


def test_resume_refuses_when_the_pack_changed(live):
    """卡被改过 → 旧局不通用（与读档 G2 同一口径），拒绝并说清怎么办。"""
    sid = _open(live)
    _say(live, sid)
    _restart(live)
    # 改一处玩法内容（digest 变）
    sched = live.root / "probe" / "schedule.yaml"
    data = yaml.safe_load(sched.read_text(encoding="utf-8"))
    data["actions"][0]["label"] = data["actions"][0]["label"] + "（改过）"
    sched.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    r = live.client.post(f"/api/{sid}/resume")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "不是同一份内容" in detail and "存档列表" in detail, detail


def test_legacy_snapshot_matches_by_world_name_and_says_so(live):
    """旧快照没有身份戳 → 按世界名**唯一匹配**才认，并在响应里说明走了兜底。"""
    sid = _open(live)
    _say(live, sid)
    tl = web._timeline(sid)
    head = tl.head()
    path = tl.root / head.checkpoint
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("pack", None)       # 退回 N13 之前的形状
    payload.pop("mainline", None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _restart(live)

    rows = {r["sid"]: r for r in live.client.get("/api/timelines").json()["timelines"]}
    assert rows[sid]["legacy"] is True
    assert rows[sid]["resumable"] is True, rows[sid]

    d = live.client.post(f"/api/{sid}/resume").json()
    assert d["pack_id"] == "probe"
    assert "旧格式" in (d["note"] or ""), d["note"]


def test_unknown_sid_is_a_clear_404(live):
    r = live.client.post("/api/nosuchsession/resume")
    assert r.status_code == 404
    assert "没有这一局的时间线" in r.json()["detail"]


def test_sid_cannot_escape_the_saves_root(live):
    """sid 是客户端可控字符串：绝不许拼出 `saves/` 之外的路径。"""
    for bad in ("../../etc", "..%2F..%2Fevil", "a b"):
        r = live.client.get(f"/api/timelines")  # 列表不接 sid，但续玩要
        assert r.status_code == 200
        r2 = live.client.post(f"/api/{bad}/resume")
        assert r2.status_code in (400, 404), (bad, r2.status_code)


# ---------------------------------------------------------------------------
# 6. 幂等
# ---------------------------------------------------------------------------


def test_resume_is_idempotent_when_the_session_is_live(live):
    """已经在内存里跑着 → 直接回现状，**不重建**（重建会顶掉进行中的会话）。"""
    sid = _open(live)
    _say(live, sid)
    session_before = web.SESSIONS[sid]
    turns_before = session_before.game.state.turn_count

    d = live.client.post(f"/api/{sid}/resume").json()
    assert d["ok"] is True and d["turn"] == turns_before
    assert "已经在内存里" in (d["note"] or ""), d["note"]
    assert web.SESSIONS[sid] is session_before, "会话对象被替换了——进行中的回合会丢"


def test_resume_attaches_the_derived_worker(live, monkeypatch):
    """续玩出来的会话也要挂 N8 调度器——否则"重启一次就悄悄失去一个能力"。"""
    monkeypatch.setattr(web, "DERIVED_ASYNC", True)
    sid = _open(live)
    _say(live, sid)
    _restart(live)
    web.SESSIONS.clear()
    live.client.post(f"/api/{sid}/resume")
    session = web.SESSIONS[sid]
    assert session.derived is not None, "续玩的会话没挂后台 worker"
    assert session.game.scheduler is not None, "game 的调度器没接上"
    if session.derived is not None:
        session.derived.stop()


# ---------------------------------------------------------------------------
# N14：结局落盘 + 按卡列全部局
# ---------------------------------------------------------------------------


def _finish_with_ending(live, sid: str):
    """把这一局"打到结局"并记一版（走真实的 `_checkpoint_turn`）。

    **注入必须发生在回合之后**：`_narrate` 结尾会 `self.ending = last.ending`，
    所以回合前塞进去的 ending 会被这一回合覆盖掉（第一版夹具就是这么错的，
    症状是"结局没进快照"——差一点被当成实现的 bug 去改实现）。
    """
    from game_agent.worldpack import EndingSpec

    session = web.SESSIONS[sid]
    _say(live, sid)
    session.game.ending = EndingSpec(
        id="end_left", title="归隐", kind="auto", text="你收拾行装，从此不问世事。"
    )
    note = web._checkpoint_turn(sid, session, web.TurnRequest(kind="say", text="（记一版）"))
    assert note == "", note  # 空 = 真的写进去了（非空是"没能记入时间线"）
    return session


def test_ending_is_persisted_in_the_snapshot(live):
    """`Game.ending` 只在内存里——不落盘的话续玩看不到"这一局已经结束"。"""
    sid = _open(live)
    _finish_with_ending(live, sid)
    tl = web._timeline(sid)
    payload = tl.payload(tl.head().rev)
    assert payload.get("ending"), "结局没进快照"
    assert payload["ending"]["title"] == "归隐"
    assert payload["ending"]["text"] == "你收拾行装，从此不问世事。"


def test_resume_restores_the_ending(live):
    """续玩一个已结束的局：视图里要带回结局（否则玩家以为还在局中）。"""
    sid = _open(live)
    _finish_with_ending(live, sid)
    _restart(live)

    rows = {r["sid"]: r for r in live.client.get("/api/timelines").json()["timelines"]}
    assert rows[sid]["ended"] is True, rows[sid]
    assert rows[sid]["ending_title"] == "归隐"

    d = live.client.post(f"/api/{sid}/resume").json()
    assert d["view"]["ending"] is not None, d["view"]
    assert d["view"]["ending"]["title"] == "归隐"
    assert "不问世事" in d["view"]["ending"]["text"]
    assert web.SESSIONS[sid].game.ending is not None


def test_ending_survives_a_rewind_to_before_it(live):
    """回退到**结局之前**的版本 → 结局不该还在（快照是逐版记的）。

    这条防的是"把 ending 当全局状态"：它属于**那一版**，不是这一局。
    """
    sid = _open(live)
    _say(live, sid)
    tl = web._timeline(sid)
    before_ending = tl.head().rev
    _finish_with_ending(live, sid)
    _restart(live)

    d = live.client.post(f"/api/{sid}/resume", json={"rev": before_ending}).json()
    assert d["view"]["ending"] is None, d["view"]["ending"]
    assert web.SESSIONS[sid].game.ending is None


def test_timelines_report_per_pack_counts(live):
    """`pack_counts` 由服务端算（前端不重复一遍口径）。"""
    a = _open(live)
    _say(live, a)
    b = _open(live)
    _say(live, b)
    _restart(live)

    d = live.client.get("/api/timelines").json()
    assert d["pack_counts"].get("probe") == 2, d["pack_counts"]
    assert len(d["timelines"]) == 2
    # 每行都要能定位到卡（前端按卡筛靠它）
    assert all(r["pack_id"] == "probe" for r in d["timelines"])

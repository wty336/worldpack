"""N8 守卫：前后台分离（「一主多子」）+ revision 绑定。

上游：`docs/plan-dsh-tavern-parity.md` 期 3 / 差距 **G-3**、`docs/roadmap.md` **N8**。

**被钉住的缺口**：一个叙事回合内部**串行**跑完「正文 → 判劣重写 → 结算 → 计划 →
事实抽取 → 缺席证据检查 → Judge → 反思」。后五步都是**派生、可后补**的 LLM 调用，
却全卡在 `done` 之前——玩家得等它们跑完才看到正文与选项。

**这个文件单独存在的理由**：`tests/conftest.py` 默认把 `web.DERIVED_ASYNC` 关掉
（异步会让"回合返回后立刻断言 state"的既有守卫变偶发）。所以异步路径需要
**自己的**守卫，而不是指望既有测试顺带覆盖。

守五件事：

1. **默认不改变行为**：没注入 scheduler 时，派生步骤**内联执行**（与改动前一致）；
2. **开关真的生效**：注入 scheduler 后，回合返回时派生步骤**还没跑**，之后才跑
   ——这就是"一主多子"的全部收益，必须能被观察到，而不是"改了但看不出区别"；
3. **revision 绑定**：入队后发生回退（分支变了）→ 任务**丢弃**，不污染新剧情；
4. **失败可见**：派生任务抛异常 → 计数与最近错误可查，而**回合本身不受影响**；
5. **串行化代价如实**：派生工作持会话锁，所以"快速连发"的下一回合会等它——
   这是安全换来的，不是 bug，要如实钉住（否则会被当成性能回归乱改）。
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fakes import FakeClient, msg, resp, tool_call

import game_agent.web as web
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


def _submit(narration: str, choices: list[str], cid: str = "s1"):
    return tool_call(cid, "submit_narration",
                     {"narration": narration, "choices": choices, "plot_signal": "normal"})


@pytest.fixture
def pack():
    return load_worldpack(REAL_PACK)


def _game(pack, *, scheduler=None, resolve_opening=True, **kw) -> Game:
    """造一局离线假引擎。

    `resolve_opening=False`：**不要**替调用方开局——Web 夹具要自己让 `api_new` 去开局，
    否则会开两次（第二次就报"当前没有进行中的主线节点"，本文件第一版就是这么错的）。

    假 LLM 每轮产出**不同**的正文（`第 i 段`）：否则「迟绑定」测不出来
    ——所有叙事长得一样时，判错对象和判对对象是同一串字符。
    """
    llm = LLMClient(
        FakeClient([
            resp(msg(tool_calls=[_submit(f"第{i}段正文。", ["甲", "乙", "丙"], f"s{i}")]))
            for i in range(30)
        ]),
        "fake", build_tools(pack.schedule),
    )
    game = Game(pack, GameState.from_pack(pack), llm, scheduler=scheduler, **kw)
    if resolve_opening:
        # 该包**开局即关键抉择**（`how_to_help`）——先解掉它，否则后续 `say` 会被引擎以
        # "此刻是关键抉择"拒绝（与 N7 守卫里踩的是同一个坑）。
        game.start()
        if game.state.pending_choice is not None:
            game.pick(0)
    return game


# ---------------------------------------------------------------------------
# 1. 默认不改变行为（没注入 scheduler = 内联）
# ---------------------------------------------------------------------------


def test_without_scheduler_derived_work_still_runs_inline(pack):
    """**安全退路**：没注入 scheduler 时，派生步骤必须在回合内跑完（行为不变）。

    这条守的是"这次改动是**可以打开的能力**，不是默认改变的行为"。
    CLI、单测、任何没接后台的入口都靠它。
    """
    game = _game(pack, extract_every=1, judge_every=1, factcheck_every=0, reflect_every=1)
    ran: list[str] = []
    game._extract_facts = lambda: ran.append("extract")  # type: ignore[method-assign]
    game._judge_turn = lambda n: ran.append("judge")  # type: ignore[method-assign]
    game._reflect = lambda: ran.append("reflect")  # type: ignore[method-assign]
    game.say("我上前一步。")
    assert set(ran) == {"extract", "judge", "reflect"}, f"内联路径没跑：{ran}"


# ---------------------------------------------------------------------------
# 2. 开关真的生效：`done` 不再等派生步骤
# ---------------------------------------------------------------------------


class _Scheduler:
    """把任务攒起来、由测试决定什么时候跑（确定性地观察"还没跑"）。"""

    def __init__(self):
        self.jobs: list = []

    def __call__(self, job):
        self.jobs.append(job)

    def run_all(self):
        for job in self.jobs:
            job()
        self.jobs.clear()


def test_injected_scheduler_defers_derived_work(pack):
    """**核心断言**：注入 scheduler 后，回合返回时派生步骤**还没跑**。

    这就是"一主多子"的全部收益——`done` 不再等那几次派生 LLM 调用。
    如果这条不成立，这个特性等于没做（只是把调用换了个写法）。
    """
    sched = _Scheduler()
    game = _game(pack, scheduler=sched, extract_every=1, judge_every=1,
                 factcheck_every=0, reflect_every=1)
    ran: list[str] = []
    game._extract_facts = lambda: ran.append("extract")  # type: ignore[method-assign]
    game._judge_turn = lambda n: ran.append("judge")  # type: ignore[method-assign]
    game._reflect = lambda: ran.append("reflect")  # type: ignore[method-assign]

    game.say("我上前一步。")
    assert ran == [], f"回合返回前就跑了派生步骤：{ran}"
    assert len(sched.jobs) >= 3, f"派生步骤没被交出（{len(sched.jobs)} 个）"

    sched.run_all()
    assert set(ran) == {"extract", "judge", "reflect"}, f"交出去之后没跑：{ran}"


def test_deferred_narration_is_bound_not_late_bound(pack):
    """交出去的任务必须**绑死**本轮叙事（`lambda n=...`），不能读到后文的变量。

    迟绑定会让"第 3 回合的 judge 去判第 4 回合的正文"——而且**不报错**，只是判错对象。
    假 LLM 每轮产出不同正文（`第 i 段`），所以"绑错了"是可观察的：
    若用迟绑定，两轮的任务会拿到**同一个**（最后一轮的）叙事。
    """
    sched = _Scheduler()
    game = _game(pack, scheduler=sched, extract_every=0, judge_every=1,
                 factcheck_every=0, reflect_every=0)
    # 夹具里"开局 + 解掉关键抉择"本身也是一次回合，会入队一条 judge 任务——
    # 先清空，才能把断言限定在本测试制造的这两轮上。
    sched.jobs.clear()
    seen: list[str] = []
    game._judge_turn = lambda n: seen.append(n)  # type: ignore[method-assign]

    v1 = game.say("第一轮。")
    v2 = game.say("第二轮。")
    assert v1.narration != v2.narration, "前提：两轮叙事必须不同，否则测不出迟绑定"

    sched.run_all()
    assert seen == [v1.narration, v2.narration], (
        f"交出去的任务没绑死本轮叙事（可能是迟绑定）：拿到 {seen}，"
        f"应为 [{v1.narration!r}, {v2.narration!r}]"
    )


# ---------------------------------------------------------------------------
# 3/4/5. Web 层的 worker：revision 绑定 · 失败可见 · 串行化
# ---------------------------------------------------------------------------


@pytest.fixture
def live(tmp_path, monkeypatch):
    """一个**启用**后台派生的 Web 客户端（离线假 LLM）。"""
    root = tmp_path / "world-packs"
    root.mkdir(parents=True)
    shutil.copytree(REAL_PACK, root / "probe")
    monkeypatch.setattr(web, "_pack_root", lambda: root)
    monkeypatch.setattr(web, "SAVE_ROOT", tmp_path / "saves")
    monkeypatch.setattr(web, "SESSIONS", {})
    monkeypatch.setattr(web, "DERIVED_ASYNC", True)
    monkeypatch.setattr(web, "load_settings", lambda: _FakeSettings())

    def fake_make(sid, pack_id=None, *, draft=None, mainline_enabled=True):
        game = _game(load_worldpack(root / "probe"), mainline_enabled=mainline_enabled,
                     resolve_opening=False,  # 下面 `api_new` 会开局
                     extract_every=1, judge_every=1, factcheck_every=0, reflect_every=0)
        return game, web.UsageTracker(str(tmp_path / "saves" / f"usage-{sid}.jsonl"),
                                      session=sid, pack=pack_id)

    monkeypatch.setattr(web, "_make_game", fake_make)
    monkeypatch.setattr(web, "_game_options", lambda: {})
    client = TestClient(web.app)
    sid = client.post("/api/new", json={"pack_id": "probe"}).json()["sid"]
    # 该包开局即关键抉择 → 先解掉，后面才能自由 `say`
    r = client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
    assert "event: done" in r.text, r.text[:200]
    return client, sid


class _FakeSettings:
    has_api_key = True


def _settle(worker, timeout: float = 8.0) -> None:
    """等 worker 把手上的活干完（用 `idle()`，不要自己拼计数——那样会漏掉"正在执行"）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if worker.idle():
            return
        time.sleep(0.02)
    raise AssertionError(f"worker 没在 {timeout}s 内静下来：{worker.stats()}")


def test_done_arrives_before_derived_work_finishes(live, monkeypatch):
    """**端到端**：HTTP 响应（`done`）返回时，派生工作还没跑完。

    做法：把某一个派生步骤换成"慢动作"，然后断言"POST 返回时它还没完成、
    之后才完成"。这是这个特性唯一的直接证据——否则"异步了"只是代码看起来如此。
    """
    client, sid = live
    session = web.SESSIONS[sid]
    worker = session.derived
    assert worker is not None, "启用后必须真的建了 worker"

    slow_done = threading.Event()
    original = session.game._extract_facts

    def slow_extract():
        time.sleep(0.4)
        original()
        slow_done.set()

    monkeypatch.setattr(session.game, "_extract_facts", slow_extract)
    # 开局那一轮已经跑过一次（异步），先等它静下来
    _settle(worker)
    slow_done.clear()

    r = client.post(f"/api/{sid}/turn", json={"kind": "say", "text": "我上前一步。"})
    assert r.status_code == 200
    assert not slow_done.is_set(), "POST 返回时派生工作已经做完了——那说明它还是同步的"
    assert slow_done.wait(timeout=5.0), "派生工作始终没跑（交出去就丢了？）"


def test_worker_discards_jobs_after_a_rewind(live, monkeypatch):
    """**revision 绑定**：回退（分支变了）之后，旧分支排队的派生任务必须**丢弃**。

    挡的是"迟到写入污染新剧情"：回退到第 2 回合后继续玩，而第 4 回合排队的 judge
    结果此时才跑完——若不丢弃，它会把"第 4 回合的叙事"的评价写进"刚回退出来的新线"。

    **为什么用受控的 `_current_branch` 而不是真做一次回退**：真回退的时序要靠线程争锁
    去凑（任务可能在回退之前就跑了），那样的守卫会偶发——而这里要钉的是**判定规则**：
    "入队时的分支 ≠ 执行时的分支 → 丢弃"。真回退改的是"分支确实会变"这个前提，
    由 `tests/test_timeline.py` 覆盖；本条覆盖"变了之后 worker 怎么反应"。

    （计数器**不重置**：`idle()` 靠 `done+failed+discarded == queued` 判定是否干完，
    中途重置会让它永远等不到。第一版就是这么踩坑的——用快照差值代替重置。）
    """
    client, sid = live
    session = web.SESSIONS[sid]
    worker = session.derived
    _settle(worker)
    d0 = worker.discarded

    ran: list[int] = []
    calls = {"n": 0}

    def fake_branch(_sid: str) -> str:
        # 第 1 次调用发生在 `submit`（记下入队分支），之后发生在执行前
        calls["n"] += 1
        return "b1" if calls["n"] == 1 else "b2"

    monkeypatch.setattr(web, "_current_branch", fake_branch)
    worker.submit(lambda: ran.append(1))
    _settle(worker)
    assert ran == [], "入队分支与执行分支不一致却仍执行了任务 —— 迟到写入会污染新剧情"
    assert worker.discarded == d0 + 1, worker.stats()
    assert "回退" in worker.last_error, (
        f"丢弃原因要写清（否则看不出为什么这条没跑）：{worker.last_error}"
    )

    # 反向：分支一致时**必须执行**（否则"总是丢弃"也能让上面那条通过）
    monkeypatch.setattr(web, "_current_branch", lambda _sid: "b1")
    ran.clear()
    worker.submit(lambda: ran.append(2))
    _settle(worker)
    assert ran == [2], "同一分支内的任务没被执行"
    assert worker.discarded == d0 + 1, "同一分支时不该再丢弃"


def test_derived_failure_is_visible_and_does_not_break_the_turn(live, monkeypatch):
    """派生失败 → 计数与最近错误可查，而**回合本身照常成功**。

    与 ADR 0006 同一条纪律：前台已提交的东西不因为派生失败而撤销。
    而失败**必须记账**——后台静默失败是最难查的故障。
    """
    client, sid = live
    session = web.SESSIONS[sid]
    worker = session.derived
    _settle(worker)

    def boom():
        raise RuntimeError("派生炸了")

    monkeypatch.setattr(session.game, "_extract_facts", boom)
    r = client.post(f"/api/{sid}/turn", json={"kind": "say", "text": "我上前一步。"})
    assert r.status_code == 200 and "event: done" in r.text, "派生失败不该让回合失败"
    _settle(worker)
    stats = client.get(f"/api/{sid}/derived").json()
    assert stats["enabled"] is True
    assert stats["stats"]["failed"] >= 1, stats
    assert "派生炸了" in stats["stats"]["last_error"]


def test_derived_endpoint_reports_disabled_when_off(live, monkeypatch):
    """没启用后台派生时会话照旧工作，端点如实回 `enabled: false`。"""
    client, sid = live
    session = web.SESSIONS[sid]
    session.derived.stop()
    session.derived = None
    web._attach_scheduler(sid, session)
    d = client.get(f"/api/{sid}/derived").json()
    assert d["enabled"] is False and d["stats"] == {}
    # 关掉之后回合仍然可用（退回内联执行）
    assert client.post(f"/api/{sid}/turn", json={"kind": "say", "text": "你好"}).status_code == 200


def test_worker_serializes_with_turns(live):
    """**如实钉住代价**：派生工作持会话锁，所以下一回合会等它跑完。

    这条不是"测一个缺陷"，而是把**设计取舍**写成断言：我们换来的是"不再挡住玩家
    看正文"，代价是"快速连发的客户端仍会串行"。写下来是为了让后来人知道
    这是选择、不是性能回归——改它之前先想清楚赛跑问题。
    """
    client, sid = live
    session = web.SESSIONS[sid]
    worker = session.derived
    _settle(worker)

    held = threading.Event()
    release = threading.Event()

    def slow_job():
        held.set()
        release.wait(timeout=5)

    worker.submit(slow_job)
    assert held.wait(timeout=5), "慢任务没开始"

    # 此刻 worker 持有会话锁 → 回合应当拿不到锁。用非阻塞探测来证（不真发请求，
    # 否则会卡住测试）。
    acquired = session.lock.acquire(blocking=False)
    assert acquired is False, "派生任务执行时没有持会话锁——那就可能和回合赛跑"
    release.set()
    for _ in range(200):
        if session.lock.acquire(blocking=False):
            session.lock.release()
            break
        time.sleep(0.01)
    else:
        raise AssertionError("慢任务结束后没释放锁")

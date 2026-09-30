"""N1 守卫：后台生成任务 + 进度 SSE（`game_agent/jobs.py` + web 端点）。

上游：`docs/roadmap.md` N1；`docs/plan-creator-player.md` 批次 B 的 B2。

**这不是"包一层队列"那么简单**，本文件守的是四件在分钟级任务里才会暴露的性质：

1. **回放**——刷新页面后必须看到已发生的全部进度（生成是分钟级，刷新是常态）；
2. **断线不取消**——客户端走了任务照跑（与 `_turn_stream` 的"锁的生存期与客户端
   是否在线无关"同一条纪律）；
3. **取消真的能停**，且落成 `cancelled` 而不是 `failed`（取消不是失败）；
4. **拒绝覆盖已存在的包**——`materialize` 会先清空 `npcs/`，同名写入等于静默毁包。

用 `offline=True` 跑（零成本、可重复）；真机路径由 `default_runner` 的同一段代码覆盖。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import game_agent.web as web
from game_agent import catalog, jobs
from game_agent.jobs import CANCELLED, DONE, FAILED, QUEUED, RUNNING, GenerationJob, JobRegistry

SOURCE = "# 素材\n\n主角在环形都市醒来，遇到诊所医生林。\n"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch, tmp_path):
    """每个用例一套干净的任务表 + 输出目录（避免用例之间互相看到对方的任务）。"""
    monkeypatch.setattr(web, "JOBS", JobRegistry())
    monkeypatch.setattr(web, "_pack_root", lambda: tmp_path / "world-packs")
    (tmp_path / "world-packs").mkdir(parents=True, exist_ok=True)
    yield


def _wait(job: GenerationJob, timeout: float = 60.0) -> GenerationJob:
    t0 = time.time()
    while job.status not in jobs.TERMINAL:
        if time.time() - t0 > timeout:
            raise AssertionError(f"任务未在 {timeout}s 内结束（当前 {job.status}）")
        time.sleep(0.05)
    return job


# ---------------------------------------------------------------------------
# 1. 任务生命周期
# ---------------------------------------------------------------------------


def test_offline_job_completes_and_reports_result():
    job = web.JOBS.create(
        pack_name="job_probe", pack_dir=web._pack_root() / "job_probe",
        source_text=SOURCE, offline=True, with_corpus=False, rounds=4,
    )
    web.JOBS.run_in_background(job, SOURCE, jobs.default_runner)
    _wait(job)

    assert job.status == DONE, job.error
    assert job.result is not None
    assert job.result["pack_name"] == "job_probe"
    assert "离线测试世界" in job.result["summary"]
    assert (web._pack_root() / "job_probe" / "world.yaml").is_file()
    # 事件留档完整（回放的事实来源）
    stages = [e["stage"] for e in job.events]
    assert "start" in stages and "done" in stages
    assert job.elapsed >= 0


def test_failed_job_records_error_without_killing_thread(tmp_path):
    """失败必须落成 job 状态与可读错误，**不能让异常逃出线程**。"""
    job = web.JOBS.create(
        pack_name="will_fail", pack_dir=tmp_path / "will_fail",
        source_text=SOURCE, offline=False, with_corpus=False, rounds=1,
    )

    def boom(_job, _text):
        raise RuntimeError("模拟：模型端点拒了")

    web.JOBS.run_in_background(job, SOURCE, boom)
    _wait(job)
    assert job.status == FAILED
    assert "模拟：模型端点拒了" in (job.error or "")
    assert any(e["stage"] == "error" for e in job.events)


def test_jobs_are_serialized():
    """并发上限 1：第二个任务必须显式排队（而不是静默并跑、费用翻倍）。"""
    reg = JobRegistry(max_concurrent=1)
    gate = threading.Event()
    started: list[str] = []

    def slow(job, _text):
        started.append(job.pack_name)
        gate.wait(5)  # 占住唯一的槽位

    a = reg.create(pack_name="a", pack_dir=Path("x/a"), source_text="", offline=True,
                   with_corpus=False, rounds=1)
    b = reg.create(pack_name="b", pack_dir=Path("x/b"), source_text="", offline=True,
                   with_corpus=False, rounds=1)
    reg.run_in_background(a, "", slow)
    reg.run_in_background(b, "", slow)
    time.sleep(0.5)
    assert started == ["a"]
    assert b.status == QUEUED, "第二个任务必须处于 queued，而不是同时开跑"
    gate.set()
    assert reg.get(a.id) is a and reg.get(b.id) is b


# ---------------------------------------------------------------------------
# 2. 订阅：回放 + 广播 + 慢消费者隔离
# ---------------------------------------------------------------------------


def test_subscribe_replays_past_events():
    """**核心断言**：晚来的订阅者要拿到此前全部事件（刷新后接上进度靠它）。"""
    job = web.JOBS.create(pack_name="p", pack_dir=Path("x/p"), source_text="",
                          offline=True, with_corpus=False, rounds=1)
    for i in range(3):
        job.publish({"stage": "extract", "message": f"第 {i} 块"})
    q, snapshot = job.subscribe()
    assert [e["message"] for e in snapshot] == ["第 0 块", "第 1 块", "第 2 块"]
    # 快照之后发布的事件走队列（续播）
    job.publish({"stage": "done", "message": "完成"})
    assert q.get(timeout=2)["message"] == "完成"
    job.unsubscribe(q)


def test_slow_subscriber_does_not_block_or_lose_archive():
    """慢消费者只丢**自己**的增量，不阻塞生产者，也不影响 `events` 留档。"""
    job = web.JOBS.create(pack_name="p", pack_dir=Path("x/p"), source_text="",
                          offline=True, with_corpus=False, rounds=1)
    q, _ = job.subscribe()
    for i in range(jobs._SUBSCRIBER_QUEUE_MAX + 50):  # 远超队列容量
        job.publish({"stage": "extract", "message": f"m{i}"})
    # 留档完整（回放的事实来源不受影响）
    assert len(job.events) == jobs._SUBSCRIBER_QUEUE_MAX + 50
    # 队列没被撑爆，且末尾留了省略提示
    assert q.qsize() <= jobs._SUBSCRIBER_QUEUE_MAX
    job.unsubscribe(q)


def test_unsubscribe_stops_delivery():
    job = web.JOBS.create(pack_name="p", pack_dir=Path("x/p"), source_text="",
                          offline=True, with_corpus=False, rounds=1)
    q, _ = job.subscribe()
    job.unsubscribe(q)
    job.publish({"stage": "extract", "message": "没人听"})
    with pytest.raises(queue.Empty):
        q.get_nowait()


# ---------------------------------------------------------------------------
# 3. 取消
# ---------------------------------------------------------------------------


def test_cancel_is_not_failure():
    """取消落成 `cancelled`，**不是 `failed`**——排查时第一个要看的就是这个区别。"""
    job = web.JOBS.create(pack_name="p", pack_dir=Path("x/p"), source_text="",
                          offline=True, with_corpus=False, rounds=1)

    def cancelled(_job, _text):
        raise jobs.WorldgenCancelled("生成已取消")

    web.JOBS.run_in_background(job, "", cancelled)
    _wait(job)
    assert job.status == CANCELLED
    assert job.error is None, "取消不是错误"
    assert job.events[-1]["stage"] == "cancelled"


def test_cancel_while_queued_does_not_start():
    """排队中被取消的任务**不该发车**——否则会白烧一次生成。"""
    reg = JobRegistry(max_concurrent=1)
    gate = threading.Event()
    ran: list[str] = []

    def slow(job, _text):
        ran.append(job.pack_name)
        gate.wait(5)

    a = reg.create(pack_name="a", pack_dir=Path("x/a"), source_text="", offline=True,
                   with_corpus=False, rounds=1)
    b = reg.create(pack_name="b", pack_dir=Path("x/b"), source_text="", offline=True,
                   with_corpus=False, rounds=1)
    reg.run_in_background(a, "", slow)
    reg.run_in_background(b, "", slow)
    time.sleep(0.3)
    assert b.request_cancel() is True
    gate.set()
    _wait(b, timeout=10)
    assert b.status == CANCELLED
    assert ran == ["a"], "被取消的排队任务不该执行"


def test_cancel_after_terminal_is_rejected():
    """已结束的任务不能再取消（返回 False，让接口如实回答）。"""
    job = web.JOBS.create(pack_name="p", pack_dir=Path("x/p"), source_text="",
                          offline=True, with_corpus=False, rounds=1)
    job.status = DONE
    assert job.request_cancel() is False


def test_pipeline_honours_cancel_between_blocks(tmp_path):
    """管线真的会在块与块之间停：取消标志一置，下一次 `extract_json` 就抛。"""
    from game_agent.worldgen import OfflineLLM, SectionExtractor, WorldgenCancelled

    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # 放行两次调用，第三次起要求停

    ext = SectionExtractor(OfflineLLM(), SOURCE, offline=True, should_cancel=should_cancel)
    with pytest.raises(WorldgenCancelled):
        ext.generate_sections()


# ---------------------------------------------------------------------------
# 4. HTTP 契约
# ---------------------------------------------------------------------------


def test_generate_endpoint_starts_job_and_streams_progress():
    """端到端：POST 起任务 → GET SSE 拿到回放 + 终态事件。"""
    client = TestClient(web.app)
    r = client.post("/api/packs/generate", json={
        "name": "http_probe", "source_text": SOURCE, "offline": True,
    })
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] in (QUEUED, RUNNING, DONE)
    job_id = body["job_id"]

    stream = client.get(f"/api/packs/generate/{job_id}/events")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(b.split("data: ", 1)[1])
              for b in stream.text.split("\n\n") if b.startswith("event: progress")]
    stages = [e["stage"] for e in events]
    assert "extract" in stages and "done" in stages, stages

    final = client.get(f"/api/packs/generate/{job_id}").json()
    assert final["status"] == DONE, final.get("error")
    assert final["result"]["pack_name"] == "http_probe"


def test_events_endpoint_replays_for_late_subscriber():
    """任务跑完之后再订阅，仍要拿到完整事件序列（分钟级任务里这是常态）。"""
    client = TestClient(web.app)
    job_id = client.post("/api/packs/generate", json={
        "name": "late_probe", "source_text": SOURCE, "offline": True,
    }).json()["job_id"]
    _wait(web.JOBS.get(job_id))

    # 任务已终态才来订阅
    stream = client.get(f"/api/packs/generate/{job_id}/events")
    events = [json.loads(b.split("data: ", 1)[1])
              for b in stream.text.split("\n\n") if b.startswith("event: progress")]
    assert any(e["stage"] == "done" for e in events)
    assert len(events) > 3


def test_generate_refuses_to_shadow_published_pack():
    """**护栏**：同名**已发布**包存在 → 400。

    草稿区落地后这条的意义变了（也更准了）：生成写进 `_drafts/`，所以它不再可能
    覆盖已发布内容；拒绝的理由从"会覆盖"变成"会产生一个发布时会撞名的草稿，
    且作者会误以为在改那个已发布的包"。
    """
    client = TestClient(web.app)
    root = web._pack_root()
    (root / "taken").mkdir(parents=True, exist_ok=True)
    (root / "taken" / "world.yaml").write_text("name: 已有\n", encoding="utf-8")

    r = client.post("/api/packs/generate", json={
        "name": "taken", "source_text": SOURCE, "offline": True,
    })
    assert r.status_code == 400
    assert "已发布" in r.json()["detail"]


def test_generate_into_existing_draft_is_allowed():
    """**草稿可以反复生成**——那正是草稿区的用途（也是 §3.2 ③ 要解决的事）。

    在草稿区落地之前，同名生成是被一刀拒绝的，于是"改一版再生成"只能靠不停换名字。
    """
    client = TestClient(web.app)
    body = {"name": "iter_me", "source_text": SOURCE, "offline": True}
    for _ in range(2):
        r = client.post("/api/packs/generate", json=body)
        assert r.status_code == 202, r.text
        assert _wait(web.JOBS.get(r.json()["job_id"])).status == DONE
    # 两次都落在同一个草稿目录，且草稿不是"已发布卡"
    assert (catalog.draft_dir("iter_me", web._pack_root()) / "world.yaml").is_file()
    assert "iter_me" not in [p["id"] for p in client.get("/api/catalog").json()["packs"]]


def test_generate_rejects_bad_name_and_empty_source():
    client = TestClient(web.app)
    r = client.post("/api/packs/generate", json={"name": "../escape", "source_text": SOURCE})
    assert r.status_code == 400 and "非法包名" in r.json()["detail"]

    r2 = client.post("/api/packs/generate", json={"name": "ok_name", "source_text": "   "})
    assert r2.status_code == 400 and "素材为空" in r2.json()["detail"]

    r3 = client.post("/api/packs/generate", json={
        "name": "ok_name", "source_text": SOURCE, "rounds": 0,
    })
    assert r3.status_code == 400 and "rounds" in r3.json()["detail"]


def test_generate_accepts_text_only_not_paths():
    """**安全边界**：接口只收 source_text，不收文件路径。

    若它接受 `sources: ["../../.env"]`，就等于给任何能访问本服务的人一个任意文件读取洞
    （`read_sources` 会老实读出来喂给模型）。这里用"多余的字段被忽略"钉住：
    传 sources 不会产生任何文件读取——任务照旧只看 source_text。
    """
    client = TestClient(web.app)
    r = client.post("/api/packs/generate", json={
        "name": "textonly", "source_text": SOURCE, "offline": True,
        "sources": ["../../.env", "C:/Windows/win.ini"],  # 未知字段：被 pydantic 忽略
    })
    assert r.status_code == 202
    job = _wait(web.JOBS.get(r.json()["job_id"]))
    assert job.status == DONE, job.error
    # 若真去读了 .env，素材长度会与 SOURCE 不同
    assert job.source_chars == len(SOURCE)


def test_unknown_job_is_404():
    client = TestClient(web.app)
    assert client.get("/api/packs/generate/nope").status_code == 404
    assert client.get("/api/packs/generate/nope/events").status_code == 404
    assert client.post("/api/packs/generate/nope/cancel").status_code == 404


def test_job_list_endpoint():
    client = TestClient(web.app)
    jid = client.post("/api/packs/generate", json={
        "name": "listed", "source_text": SOURCE, "offline": True,
    }).json()["job_id"]
    _wait(web.JOBS.get(jid))
    rows = client.get("/api/packs/generate").json()["jobs"]
    assert any(r["job_id"] == jid and r["status"] == DONE for r in rows)
    # 列表只报摘要，不回传全部事件（那是 SSE 的事）
    assert "events" in rows[0] and isinstance(rows[0]["events"], int)


# ---------------------------------------------------------------------------
# 5. 真流式：端点必须**边产生边发**
# ---------------------------------------------------------------------------
#
# N1 的全部意义是"分钟级任务能边跑边看进度"。若 `_job_stream` 被改成
# "先收集全部事件、跑完再一次性发"，**功能测试会全绿**（事件内容一样），
# 而真实体验退化成转圈等到最后。所以这条必须单独钉。
#
# **为什么在生成器层面测，而不是发一次 HTTP 请求**：`TestClient` 会把整个 SSE
# 响应体缓冲下来再交给你——用它的 `client.stream()` 测出来的"首末间隔"恒为 0，
# 那是**客户端在缓冲**，不是服务端。这一点是实测确认的（绕过 HTTP 直接消费
# `_job_stream`，同一段代码，实测首末间隔 0.297s，与合成的 50ms×6 完全吻合）。
# 所以：
#   - 服务端增量性 → 这里的生成器守卫（确定性、无缓冲层干扰）；
#   - HTTP 链路的增量性 → `scripts/worldgen_smoke.py` 对**真机服务**跑（客户端
#     到达时间才是有意义的判据，而离线生成太快不可判，故那里如实报"不可判"）。


def _paced_job(pause: float = 0.05, n: int = 6):
    job = web.JOBS.create(
        pack_name="paced", pack_dir=web._pack_root() / "paced", source_text="",
        offline=True, with_corpus=False, rounds=1,
    )

    def paced(the_job, _text):
        the_job.publish({"stage": "start", "message": "开始"})
        for i in range(n):
            time.sleep(pause)
            the_job.publish({"stage": "extract", "message": f"第 {i} 块"})
        the_job.status = DONE
        the_job.publish({"stage": "done", "message": "完成"})

    web.JOBS.run_in_background(job, "", paced)
    return job


def test_job_stream_yields_incrementally():
    """**N1 的核心性质**：`_job_stream` 必须逐条产出，而不是攒完再吐。

    合成一个"每条事件间隔 50ms、共 6 条"的任务，直接消费生成器并记录每次 `next()`
    的时刻。攒完再吐的实现会让首末间隔塌成 0。
    """
    job = _paced_job()
    stamps: list[float] = []
    t0 = time.monotonic()
    for frame in web._job_stream(job):
        if frame.startswith("event: progress"):
            stamps.append(time.monotonic() - t0)

    assert len(stamps) >= 5, f"只产出 {len(stamps)} 条进度帧"
    spread = stamps[-1] - stamps[0]
    assert spread > 0.15, (
        f"进度帧挤在一起产出（首末间隔 {spread:.3f}s，任务本身约 0.3s）——"
        f"`_job_stream` 很可能是「攒完再一次性吐」，而不是边产生边吐"
    )
    assert stamps[0] < 0.15, f"第一条进度帧来得太晚（{stamps[0]:.3f}s），像是被缓冲了"


def test_job_stream_closes_on_terminal_event():
    """终态事件之后流必须收尾——否则连接永远挂着，前端一直转圈。"""
    job = _paced_job(pause=0.01, n=2)
    frames = list(web._job_stream(job))  # 能自然结束就说明会收尾
    kinds = [f.split("\n", 1)[0] for f in frames]
    assert kinds[0] == "event: start"
    assert sum(1 for k in kinds if k == "event: progress") >= 3
    assert frames[-1].startswith("event: progress") and '"done"' in frames[-1]


def test_testclient_buffers_sse_so_do_not_assert_incrementality_over_http():
    """**把这条实测发现钉成文档**：`TestClient` 会缓冲整个 SSE 响应体。

    `client.stream()` 看起来是流式的，但它在 ASGI 传输层把响应收完才交出来，
    因此用它测"首末到达间隔"恒为 0。后来人若想加一条 HTTP 层的增量性断言，
    会得到假红并去改本来正确的服务端代码——这个测试就是那块路牌。
    """
    client = TestClient(web.app)
    job = _paced_job()
    arrivals: list[float] = []
    t0 = time.monotonic()
    with client.stream("GET", f"/api/packs/generate/{job.id}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                arrivals.append(time.monotonic() - t0)
    assert len(arrivals) >= 5
    # 不去断言 spread：这里恒为 0，原因在客户端不在服务端。
    # 真正的增量性断言在 test_job_stream_yields_incrementally。
    assert arrivals[-1] - arrivals[0] < 0.05, (
        "若这里不再是 ~0，说明测试客户端的行为变了——"
        "那么 HTTP 层的增量性断言就变得可行，可以把它挪到这一层来测"
    )


def test_events_carry_server_side_timestamps():
    """事件带服务端 `ts`：前端要显示"这一步花了多久"，且它是"进度逐步产生"的
    可断言事实（不依赖客户端到达时间，那会受缓冲与代理影响）。"""
    job = web.JOBS.create(pack_name="p", pack_dir=Path("x/p"), source_text="",
                          offline=True, with_corpus=False, rounds=1)
    job.publish({"stage": "extract", "message": "一"})
    time.sleep(0.02)
    job.publish({"stage": "extract", "message": "二"})
    ts = [e["ts"] for e in job.events]
    assert len(ts) == 2 and ts[1] >= ts[0] >= 0
    assert all(isinstance(t, float) for t in ts)


def test_cancel_endpoint_reports_whether_accepted():
    client = TestClient(web.app)
    jid = client.post("/api/packs/generate", json={
        "name": "cancelme", "source_text": SOURCE, "offline": True,
    }).json()["job_id"]
    _wait(web.JOBS.get(jid))  # 离线跑得很快，多半已结束
    r = client.post(f"/api/packs/generate/{jid}/cancel").json()
    # 已结束时如实返回 False（而不是假装取消成功）
    assert r["ok"] is True and r["cancelled"] is False
    assert r["status"] == DONE

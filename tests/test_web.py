"""F5 Web 前端测试（离线，P3/P3 修复批次 B）：TestClient 全流程 + 真流式时序 + 并发 + 路径安全。

使用流式 FakeClient 注入的 Game，不触网；测试后清理注入的会话。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from game_agent import web as web_module
from game_agent.compression import ensure_pairing
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = "world-packs/ancient_jianghu"

NARRATION_JSON = (
    '{"narration": "测试叙事", "choices": ["行动一", "行动二", "行动三"],'
    ' "plot_signal": "normal"}'
)


def _delta_chunk(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=None,
            delta=SimpleNamespace(content=content, reasoning_content=None, tool_calls=None),
        )],
        usage=None,
    )


def _tool_chunk():
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="tool_calls",
            delta=SimpleNamespace(
                content=None, reasoning_content=None,
                tool_calls=[SimpleNamespace(
                    index=0, id="c1",
                    function=SimpleNamespace(name="submit_narration", arguments=NARRATION_JSON),
                )],
            ),
        )],
        usage=None,
    )


class _StreamingFake:
    """Web 端点注入 on_text → run_turn 走流式路径；此 fake 每次 create 弹出一个 chunk 迭代器。"""

    def __init__(self, chunk_lists):
        self.chunk_lists = list(chunk_lists)
        self.calls: list[dict] = []

    @property
    def chat(self):
        return SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.chunk_lists:
            raise AssertionError("流式响应耗尽")
        return iter(self.chunk_lists.pop(0))


def _chunks_for_turn(delta_pieces=("测试", "叙事")):
    return [_delta_chunk(delta_pieces[0]), _delta_chunk(delta_pieces[1]), _tool_chunk(),
            SimpleNamespace(choices=[], usage=None)]


def _unused_tracker() -> web_module.UsageTracker:
    """Session 需要一个账本对象（G1），但本文件的 fake LLM 不带 tracker → 永不落盘。

    故路径只求合法、不求可写：这些守卫关心的是会话装配，不是记账
    （记账隔离由 `test_web_accounting.py` 专门守）。
    """
    return web_module.UsageTracker("saves/usage-test-only.jsonl", session="test")


def _seed_fake_session(sid: str, turns: int = 1, delta_pieces=("测试", "叙事")) -> None:
    """注入一个用流式 FakeClient 驱动的会话（离线，叙事分两段流式回传）。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    llm = LLMClient(
        _StreamingFake([_chunks_for_turn(delta_pieces) for _ in range(turns)]),
        "fake", build_tools(pack.schedule),
    )
    game = Game(pack, state, llm)
    web_module.SESSIONS[sid] = web_module.Session(game=game, lock=threading.Lock(), usage=_unused_tracker())


def _sse_payload(body: bytes) -> list[tuple[str, str]]:
    """解析 SSE 响应体为 [(event, data), ...]。"""
    events = []
    for block in body.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = next((l.split(": ", 1)[1] for l in block.splitlines() if l.startswith("event: ")), "")
        data = next((l.split(": ", 1)[1] for l in block.splitlines() if l.startswith("data: ")), "")
        events.append((ev, data))
    return events


def test_index_page_served():
    """F1（M3）：页面静态文案不含任何世界内容（标题由前端按包名动态渲染）。

    Stage B 起 `/` 返回的是 **Vite 构建产物**：一个挂载点 + 带哈希的资源引用，
    标题/卡名/模式全部由 Vue 运行时渲染。所以这条断言的落点从
    "Stage A 的 `#game-title` 挂载点"变成"`#app` 挂载点 + 正确的 /static 资源引用"。
    """
    with TestClient(web_module.app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert 'id="app"' in r.text  # Vue 挂载点
        assert "文字养成游戏" in r.text  # 通用 <title>（非世界内容）
        assert "江湖旧梦" not in r.text  # 静态文案不含世界内容
        assert "/static/assets/" in r.text  # 资源由 /static 托管（base 配对的产物）


def test_index_html_without_build_gives_actionable_error(monkeypatch, tmp_path):
    """未构建时给**可执行的**提示，而不是 StaticFiles 的难懂异常。

    这是新开发者最容易撞上的第一个错（clone 下来没跑 npm），
    所以错误信息里必须直接写着要跑什么命令。
    """
    from fastapi import HTTPException

    monkeypatch.setattr(web_module, "WEBUI_DIR", tmp_path)  # 空目录 = 没有 dist
    with pytest.raises(HTTPException) as ei:
        web_module.index_html()
    assert "尚未构建" in str(ei.value.detail)
    assert "npm run build" in str(ei.value.detail)


def test_pack_path_env_override(monkeypatch):
    """M3 换包即玩：GAME_WORLDPACK 环境变量覆盖默认世界包（web --pack 的通道）。"""
    assert web_module._pack_path() == web_module.DEFAULT_PACK
    monkeypatch.setenv("GAME_WORLDPACK", "world-packs/xianxia_wendao")
    assert web_module._pack_path() == "world-packs/xianxia_wendao"
    monkeypatch.delenv("GAME_WORLDPACK")
    assert web_module._pack_path() == web_module.DEFAULT_PACK


def test_full_flow_new_pick_say_save_load(tmp_path):
    with TestClient(web_module.app) as client:
        sid = "test-session"
        # 开局：N1 关键抉择（fake llm 无响应需求——start 返回固定选项视图）
        _seed_fake_session(sid, turns=2)
        game = web_module.SESSIONS[sid].game
        view = game.start()
        assert view.choice_prompt is not None  # N1 即触发

        # 关键抉择：SSE 流（delta 事件 = 两段叙事增量，done = 完整视图）
        events = _sse_payload(
            client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0}).content
        )
        deltas = [json.loads(d) for ev, d in events if ev == "delta"]
        assert deltas == ["测试", "叙事"]  # 流式增量逐段到达（SSE data 为 JSON）
        done = json.loads(events[-1][1])
        assert events[-1][0] == "done"
        assert done["narration"] == "测试叙事"

        # 自由发言（同一会话，N1 已完成 → 日常阶段）
        events = _sse_payload(
            client.post(f"/api/{sid}/turn", json={"kind": "say", "text": "你好"}).content
        )
        assert events[-1][0] == "done"
        assert json.loads(events[-1][1])["narration"] == "测试叙事"

        # 存档 → 读档（A-1：路径被约束到 saves/ 根 + .json 白名单）
        r = client.post(f"/api/{sid}/save", json={"path": "web-test-save.json"})
        assert r.json()["ok"] is True
        r = client.post(f"/api/{sid}/load", json={"path": "web-test-save.json"})
        assert r.json()["ok"] is True and "status" in r.json()
        (web_module.SAVE_ROOT / "web-test-save.json").unlink(missing_ok=True)  # 清理


def test_save_path_traversal_rejected():
    """A-1（C1）：任意路径读写洞——目录成分/绝对路径/非 .json 后缀一律 400。"""
    with TestClient(web_module.app) as client:
        sid = "trav-session"
        _seed_fake_session(sid)
        for bad in ("../../.env", "..\\..\\pyproject.toml", "sub/dir/x.json",
                    "C:/evil.json", "x.txt", "x", "saves/../.env"):
            r = client.post(f"/api/{sid}/save", json={"path": bad})
            assert r.status_code == 400, f"应拒绝 {bad!r}，实际 {r.status_code}"
            r = client.post(f"/api/{sid}/load", json={"path": bad})
            assert r.status_code == 400, f"应拒绝 {bad!r}，实际 {r.status_code}"
        # 直接函数级验证：严格拒绝一切目录成分（剥除式会静默改名，故取拒绝）
        import pytest

        for bad in ("a/b/c.json", "C:/tmp/evil.json"):
            with pytest.raises(Exception):
                web_module._safe_save_path(bad)
        assert web_module._safe_save_path("ok-name.json") == web_module.SAVE_ROOT / "ok-name.json"


def test_turn_errors_are_reported_via_sse():
    with TestClient(web_module.app) as client:
        sid = "err-session"
        _seed_fake_session(sid)
        web_module.SESSIONS[sid].game.start()  # 进入关键抉择锁定
        events = _sse_payload(
            client.post(f"/api/{sid}/turn", json={"kind": "say", "text": "越界发言"}).content
        )
        assert events[0][0] == "start"  # 首帧心跳（D15）：先确认连接已建立
        err = next(e for e in events if e[0] == "error")
        # B-2（M1）：error 帧数据与 delta/done 一样是 JSON——前端 JSON.parse 可解析
        assert json.loads(err[1]) is not None
        assert "关键抉择" in json.loads(err[1])


def test_unknown_action_returns_error_frame_not_broken_stream():
    """B-3（M2）：act 传未知 action_id → ScheduleError 转 error 帧，流不中途断裂。"""
    with TestClient(web_module.app) as client:
        sid = "schedule-err-session"
        _seed_fake_session(sid, turns=1)
        game = web_module.SESSIONS[sid].game
        game.start()
        # 经 API 完成 N1（消耗 1 个 fake 响应，同时给 on_text 走流式路径）
        events = _sse_payload(
            client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0}).content
        )
        assert events[-1][0] == "done"
        events = _sse_payload(
            client.post(f"/api/{sid}/turn", json={"kind": "act", "action_id": "nonexistent"}).content
        )
        assert events[0][0] == "start"  # 首帧心跳
        err = next(e for e in events if e[0] == "error")
        assert "未知日程行动" in json.loads(err[1])


def test_sse_streams_incrementally_before_llm_completes():
    """B-1（M3）时序断言：首个 delta 到达时 LLM 调用仍在进行 → 真流式。

    注：TestClient 会缓冲响应体（传输层时序测不到），故直接迭代
    StreamingResponse.body_iterator 测生成器语义；传输层由真机 uvicorn 复跑验证。
    """
    sid = "timing-session"
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    state.completed_nodes.append("n1_first_meeting")
    state.flags["met_shen"] = True
    release = threading.Event()  # 模拟「LLM 仍在生成」
    worker_done = threading.Event()

    def chunk_gen():
        yield _delta_chunk("第一段")
        release.wait(timeout=10)  # 阻塞：模拟生成中
        worker_done.set()
        yield _tool_chunk()
        yield SimpleNamespace(choices=[], usage=None)

    llm = LLMClient(_StreamingFake([chunk_gen()]), "fake", build_tools(pack.schedule))
    game = Game(pack, state, llm)
    web_module.SESSIONS[sid] = web_module.Session(game=game, lock=threading.Lock(), usage=_unused_tracker())

    resp = web_module._turn_stream(
        sid, web_module.SESSIONS[sid], web_module.TurnRequest(kind="say", text="你好")
    )
    first = next(resp)  # 首帧 = 心跳（D15）：连接建立 + 回合已受理
    assert first.startswith("event: start"), f"首帧应为心跳: {first!r}"
    # 时序断言仍落在**首个增量**上：它到达时 LLM 仍在生成
    frame = next(resp)  # 阻塞至首个 delta
    assert frame.startswith("event: delta")
    assert "第一段" in frame
    assert not worker_done.is_set(), "首个增量到达时 LLM 应仍在生成（伪流式回归）"
    release.set()
    rest = "".join(resp)
    assert "event: done" in rest
    assert worker_done.is_set()


def test_concurrent_turns_serialized_and_pairing_holds():
    """B-4（M4）：同 sid 并发回合串行执行，历史配对不变量保持。"""
    sid = "conc-session"
    _seed_fake_session(sid, turns=2)
    game = web_module.SESSIONS[sid].game
    game.start()
    results: list[tuple[str, int]] = []

    def run(name: str, payload: dict) -> None:
        with TestClient(web_module.app) as client:
            r = client.post(f"/api/{sid}/turn", json=payload)
            results.append((name, r.status_code))

    threads = [
        threading.Thread(target=run, args=("pick", {"kind": "pick", "index": 0})),
        threading.Thread(target=run, args=("say", {"kind": "say", "text": "并发发言"})),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(code == 200 for _, code in results), results
    assert ensure_pairing(game.history)  # 无 tool_calls/tool 消息交错撕裂


def test_autosave_per_session_isolated():
    """B-4（M4）：各会话自动存档按 sid 隔离，不再共享 saves/autosave.json。"""
    (web_module.SAVE_ROOT / "autosave.json").unlink(missing_ok=True)  # 清历史遗留共享文件
    for sid in ("auto-a", "auto-b"):
        pack = load_worldpack(PACK_PATH)
        state = GameState.from_pack(pack)
        llm = LLMClient(
            _StreamingFake([_chunks_for_turn()]), "fake", build_tools(pack.schedule)
        )
        # N7：自动存档路径必须**从 `SAVE_ROOT` 推**，不能写死 `"saves/..."` 相对字面量。
        # 写死了的话，`tests/conftest.py` 把 `SAVE_ROOT` 重定向到临时目录就盖不住它，
        # 产物会漏回仓库的 `saves/`（而下面的断言查的正是 `SAVE_ROOT` 下）。
        game = Game(
            pack, state, llm,
            autosave_path=web_module.SAVE_ROOT / f"autosave-{sid}.json",
        )
        web_module.SESSIONS[sid] = web_module.Session(game=game, lock=threading.Lock(), usage=_unused_tracker())
    with TestClient(web_module.app) as client:
        for sid in ("auto-a", "auto-b"):
            game = web_module.SESSIONS[sid].game
            game.start()
            events = _sse_payload(
                client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0}).content
            )
            assert events[-1][0] == "done"
    assert (web_module.SAVE_ROOT / "autosave-auto-a.json").exists()
    assert (web_module.SAVE_ROOT / "autosave-auto-b.json").exists()
    assert not (web_module.SAVE_ROOT / "autosave.json").exists()  # 旧共享路径不再出现
    (web_module.SAVE_ROOT / "autosave-auto-a.json").unlink(missing_ok=True)
    (web_module.SAVE_ROOT / "autosave-auto-b.json").unlink(missing_ok=True)


def test_status_and_actions_endpoints():
    with TestClient(web_module.app) as client:
        sid = "status-session"
        _seed_fake_session(sid)
        r = client.get(f"/api/{sid}/status")
        assert "玩家属性" in r.json()["text"]
        r = client.get(f"/api/{sid}/actions")
        assert any(a["id"] == "cultivate" for a in r.json()["actions"])
        # 未结识 → 备礼探访不可用（P2 requires 门槛在 Web 端同样生效）
        assert not any(a["id"] == "gift_visit" for a in r.json()["actions"])

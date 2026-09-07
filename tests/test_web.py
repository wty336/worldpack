"""F5 Web 前端测试（离线，P3）：TestClient 走完「开局 → 抉择 → 对话 → 存读档」全流程。

使用 FakeClient 注入的 Game，不触网；测试后清理注入的会话。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from fakes import tool_call

from game_agent import web as web_module
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
    """Web 端点注入 on_text → run_turn 走流式路径；此 fake 每次 create 弹出一组 chunk。"""

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


def _seed_fake_session(sid: str, turns: int = 1, delta_pieces=("测试", "叙事")) -> None:
    """注入一个用流式 FakeClient 驱动的会话（离线，叙事分两段流式回传）。"""
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    llm = LLMClient(
        _StreamingFake([_chunks_for_turn(delta_pieces) for _ in range(turns)]),
        "fake", build_tools(pack.schedule),
    )
    game = Game(pack, state, llm)
    web_module.SESSIONS[sid] = game


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
    with TestClient(web_module.app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "江湖旧梦" in r.text and "api/new" in r.text


def test_full_flow_new_pick_say_save_load(tmp_path):
    with TestClient(web_module.app) as client:
        sid = "test-session"
        # 开局：N1 关键抉择（fake llm 无响应需求——start 返回固定选项视图）
        _seed_fake_session(sid, turns=2)
        game = web_module.SESSIONS[sid]
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

        # 存档 → 读档
        save_path = str(tmp_path / "web.json")
        r = client.post(f"/api/{sid}/save", json={"path": save_path})
        assert r.json()["ok"] is True
        r = client.post(f"/api/{sid}/load", json={"path": save_path})
        assert r.json()["ok"] is True and "status" in r.json()


def test_turn_errors_are_reported_via_sse():
    with TestClient(web_module.app) as client:
        sid = "err-session"
        _seed_fake_session(sid)
        web_module.SESSIONS[sid].start()  # 进入关键抉择锁定
        events = _sse_payload(
            client.post(f"/api/{sid}/turn", json={"kind": "say", "text": "越界发言"}).content
        )
        assert events[0][0] == "error"
        assert "关键抉择" in events[0][1]


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

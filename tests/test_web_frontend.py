"""Web 前端选项路由回归守卫（玩家实测发现：日常选项被错接到 pick 通道）。

- 数据契约：关键抉择轮 view.choice_prompt 非空；日常轮为空且 choices 存在——
  前端据此分流 pick（序号）与 say（文本）；
- 复现钉：日常状态下调用 pick → SSE error 事件（JS 必须避免的路由）；
- 结构断言：INDEX_HTML 的 render() 必须按 choice_prompt 分流，且自由输入入口
  聚焦输入框而非把文案当发言发出（布局级不变量用结构测试，test_llm Q10 教训）。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from fakes import FakeClient, msg, resp, tool_call

import game_agent.web as web
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.storyline import FREE_INPUT_OPTION
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _submit(narration: str, choices: list[str], call_id: str = "s1"):
    return tool_call(
        call_id, "submit_narration",
        {"narration": narration, "choices": choices, "plot_signal": "normal"},
    )


def _client(monkeypatch) -> TestClient:
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)

    def fake_make_game(sid: str):
        llm = LLMClient(
            FakeClient([
                resp(msg(tool_calls=[_submit(
                    "你挡在了她身前。她敛衽一礼：多谢公子。",
                    ["拱手报上姓名", "只说姓，不提名", "（自己说些什么…）"],
                )])),
                resp(msg(tool_calls=[_submit(
                    "第二段叙事。",
                    ["选项甲", "选项乙", "选项丙"],
                    "s2",
                )])),
            ]),
            "fake", build_tools(pack.schedule),
        )
        from game_agent.game import Game

        return Game(pack, state, llm)

    monkeypatch.setattr(web, "_make_game", fake_make_game)
    return TestClient(web.app)


def _done_view(payload: str) -> dict:
    """从 SSE 文本里取 done 事件的 view JSON。"""
    for block in payload.split("\n\n"):
        lines = block.strip().splitlines()
        kind = next((l for l in lines if l.startswith("event: ")), "")
        data = next((l for l in lines if l.startswith("data: ")), "")
        if kind == "event: done" and data:
            return json.loads(data[len("data: "):])
    raise AssertionError(f"no done event in: {payload[:200]}")


def test_critical_then_daily_view_contract(monkeypatch):
    """开局 = 关键抉择（choice_prompt 非空）；抉择后 = 日常轮（空 prompt + choices）。"""
    client = _client(monkeypatch)
    d = client.post("/api/new").json()
    assert d["view"]["choice_prompt"] is not None  # 开局进 n1：关键抉择
    assert d["view"]["choices"]

    sid = d["sid"]
    r = client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
    view = _done_view(r.text)
    assert view["choice_prompt"] is None  # n1 完成 → 日常轮
    assert view["choices"] == ["拱手报上姓名", "只说姓，不提名", "（自己说些什么…）"]
    assert view["narration"]


def test_daily_pick_api_returns_error(monkeypatch):
    """钉住 JS 必须避开的路由：日常状态下 pick → 引擎拒绝（SSE error 事件）。"""
    client = _client(monkeypatch)
    d = client.post("/api/new").json()
    sid = d["sid"]
    client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})  # 解决关键抉择
    r = client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
    assert "event: error" in r.text
    assert "没有进行中的主线节点" in r.text
    # 正确路由：say 文本 → 正常回合
    r2 = client.post(f"/api/{sid}/turn", json={"kind": "say", "text": "拱手报上姓名"})
    assert "event: done" in r2.text


def test_index_html_routes_choices_by_criticality():
    """结构断言：render() 按 choice_prompt 分流；自由输入入口聚焦输入框。"""
    html = web.INDEX_HTML
    assert "const critical = !!v.choice_prompt" in html
    assert 'kind: "pick"' in html and 'kind: "say", text: c' in html
    assert '$("in").focus()' in html  # 自由输入入口 → 聚焦，不当作发言
    assert "__FREE_INPUT__" not in html  # 占位符已替换为引擎常量
    assert FREE_INPUT_OPTION in html


def test_index_html_has_generating_state():
    """结构断言（玩家实测反馈）：生成态指示器 + 忙碌禁用 + 异常也恢复可交互。"""
    html = web.INDEX_HTML
    assert 'id="gen"' in html  # 指示器元素存在
    assert "function startGen" in html and "function endGen" in html
    assert "setBusy(true)" in html and "setBusy(false)" in html
    assert ".disabled = busy" in html  # 忙碌期禁用选项与输入
    assert "已等" in html and "流式输出" in html  # 计时提示文案
    assert "finally {\n    endGen();" in html  # turn() 异常路径也恢复可交互
    assert 'startGen("模型思考中")' in html and 'startGen("正在开局")' in html

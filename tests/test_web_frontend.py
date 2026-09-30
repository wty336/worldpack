"""Web 前端选项路由回归守卫（玩家实测发现：日常选项被错接到 pick 通道）。

- 数据契约：关键抉择轮 view.choice_prompt 非空；日常轮为空且 choices 存在——
  前端据此分流 pick（序号）与 say（文本）；
- 复现钉：日常状态下调用 pick → SSE error 事件（JS 必须避免的路由）；
- 结构断言：前端 bundle（frontend_bundle() = 渲染后 HTML+CSS+JS）的 render() 必须按 choice_prompt 分流，且自由输入入口
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

    def fake_make_game(sid: str, pack_id=None, *, mainline_enabled=True):
        """签名必须与 `web._make_game` 一致（E-3/E-4 起多了选包与模式两个入参）。

        `pack_id` / `mainline_enabled` 在这里**刻意不生效**：本文件的守卫关心的是
        视图契约与前端结构分流，不是选包/模式语义（那由 `test_catalog.py` 守）。
        """
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

        # G1：`_make_game` 现在返回 (game, tracker)——tracker 由 api_new 存进 Session
        return Game(pack, state, llm), web.UsageTracker(
            "saves/usage-test-only.jsonl", session=sid
        )

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
    html = web.frontend_bundle()
    assert "const critical = !!v.choice_prompt" in html
    assert 'kind: "pick"' in html and 'kind: "say", text: c' in html
    assert '$("in").focus()' in html  # 自由输入入口 → 聚焦，不当作发言
    assert "__FREE_INPUT__" not in html  # 占位符已替换为引擎常量
    assert FREE_INPUT_OPTION in html


def test_index_html_has_generating_state():
    """结构断言（玩家实测反馈）：生成态指示器 + 忙碌禁用 + 异常也恢复可交互。"""
    html = web.frontend_bundle()
    assert 'id="gen"' in html  # 指示器元素存在
    assert "function startGen" in html and "function endGen" in html
    assert "setBusy(true)" in html and "setBusy(false)" in html
    assert ".disabled = busy" in html  # 忙碌期禁用选项与输入
    assert "已等" in html and "流式输出" in html  # 计时提示文案
    assert "finally {\n    endGen();" in html  # turn() 异常路径也恢复可交互
    assert 'startGen("模型思考中")' in html and 'startGen("正在开局")' in html


def test_index_html_has_end_day_control():
    """结构断言（玩家实测反馈）：行动区有"结束今天"入口与耗尽提示。"""
    html = web.frontend_bundle()
    assert 'kind: "end_day"' in html
    assert "结束今天" in html
    assert "行动点已用完" in html
    assert "推进时间" in html and "自动触发" in html  # 分工说明：行动 = 时间 = 剧情油门
    assert "ad.critical" in html and "行动暂不可用" in html  # 阶段分流


def test_end_day_advances_day(monkeypatch):
    """end_day 回合类型：天数推进 + 返回跨天标记叙事（离线 fake）。"""
    client = _client(monkeypatch)
    d = client.post("/api/new").json()
    sid = d["sid"]
    # 先解决开局关键抉择（C2 守卫：抉择未决时 end_day 被拒）
    client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
    r = client.post(f"/api/{sid}/turn", json={"kind": "end_day"})
    view = _done_view(r.text)
    assert "第 2 天" in view["narration"]
    pack = load_worldpack(PACK_PATH)  # 该包每天 1 点行动点
    st = client.get(f"/api/{sid}/actions").json()
    assert st["day"] == 2
    assert st["action_points_left"] == pack.schedule.day_action_points  # 行动点按新一天重置


def test_actions_phase_contract_and_critical_guard(monkeypatch):
    """阶段数据契约：关键抉择期 actions.critical=True；期间 act 被引擎拒绝（C2）。"""
    client = _client(monkeypatch)
    d = client.post("/api/new").json()
    sid = d["sid"]
    # 开局 = n1 关键抉择进行中
    st = client.get(f"/api/{sid}/actions").json()
    assert st["critical"] is True
    # 期间执行行动 → 引擎拒绝（此前会静默结算效果并扣行动点——bug）
    r = client.post(f"/api/{sid}/turn", json={"kind": "act", "action_id": "cultivate"})
    assert "event: error" in r.text and "关键抉择" in r.text
    assert client.get(f"/api/{sid}/actions").json()["action_points_left"] == 1  # 未被扣
    # 解决抉择 → 日常阶段 critical=False
    client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
    assert client.get(f"/api/{sid}/actions").json()["critical"] is False


def test_view_payload_carries_recovery_marks(monkeypatch):
    """K 系列契约：done 事件必须带 recovered/sub_turns/turn——玩家要能看见"恢复过"。

    此前一次溢出恢复会让本轮多花一次压缩调用（数秒），前端却只表现为"这轮慢"；
    熔断兜底轮则只换掉文案，玩家分不出"试过并失败"与"压根没试"。
    """
    client = _client(monkeypatch)
    d = client.post("/api/new").json()
    assert d["view"]["recovered"] == [], "正常开局不该报恢复"
    # 开局被关键抉择接管：不生成叙事 → sub_turns=0（"接管"与"生成"要能区分开）
    assert d["view"]["sub_turns"] == 0
    assert "turn" in d["view"]

    sid = d["sid"]
    view = _done_view(client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0}).text)
    assert view["recovered"] == []
    assert view["sub_turns"] >= 1  # 抉择后是正常叙事回合


def test_index_html_reports_recovery_to_player():
    """结构断言：恢复痕迹有中性文案、渲染进故事分段，且不写会被 endGen 冲掉的元素。"""
    html = web.frontend_bundle()
    assert "RECOVERY_LABELS" in html and "function recoveryNote" in html
    assert "overflow_recovered" in html and "critique" in html and "meltdown" in html
    assert "v.recovered" in html and "v.sub_turns" in html
    # genEl 在 endGen() 里被清空 → 恢复提示不能只写那里，必须落在故事分段
    assert "curEntry.textContent = text + (note" in html


def test_index_html_history_toggle():
    """结构断言（玩家反馈）：默认只显示本轮 + 剧情回顾切换 + 新回合自动回位。"""
    html = web.frontend_bundle()
    assert 'id="story" class="only-current"' in html  # 默认只看本轮
    assert "only-current .entry { display: none; }" in html
    assert 'id="historyBtn"' in html and "剧情回顾" in html and "只看本轮" in html
    assert "function toggleHistory" in html
    assert "if (fullHistory) toggleHistory();" in html  # 新回合自动回到本轮视图
    assert "story.scrollTop = fullHistory ? story.scrollHeight : 0;" in html  # 滚动语义


# ---------------------------------------------------------------------------
# Stage A（2026-10）：前端是真实文件，不是内嵌字符串
# ---------------------------------------------------------------------------


def test_frontend_is_real_files_not_inline_string():
    """回归守卫：前端必须留在 `game_agent/webui/` 的真实文件里。

    拆分前 `INDEX_HTML` 是一个 250 行的 Python 字符串常量——"改一行 CSS 也要动
    Python"，且无法被任何前端工具链消化。这份守卫钉住拆分结果，
    防止后来人图省事又把前端塞回 `web.py`。
    """
    assert not hasattr(web, "INDEX_HTML"), "前端不得退回内嵌字符串（Stage A 已拆分）"
    for name in ("index.html", "app.css", "app.js"):
        assert (web.WEBUI_DIR / name).is_file(), f"缺少前端文件 {name}"


def test_index_html_injects_free_input_from_engine_constant():
    """单一真源：自由输入文案仍来自引擎常量，不靠前后端各写一份。

    拆分前靠 `INDEX_HTML.replace("__FREE_INPUT__", ...)`；拆成静态文件后，
    注入点移到 `index_html()`，**语义必须保持一致**——否则前端与引擎会漂移，
    而漂移的症状是"自由输入那一项点了没反应"（文案对不上，走不到聚焦分支）。
    """
    html = web.index_html()
    assert "__FREE_INPUT__" not in html  # 占位符已被替换
    assert FREE_INPUT_OPTION in html
    assert FREE_INPUT_OPTION in web.frontend_bundle()


def test_static_assets_are_served():
    """`/static/*` 必须真的可达——否则页面在浏览器里是无样式、无脚本的空壳，
    而结构断言（只读文件内容）不会发现这一点。"""
    client = TestClient(web.app)
    for name in ("app.css", "app.js", "index.html"):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200, f"/static/{name} 不可达"
    assert "text/css" in client.get("/static/app.css").headers["content-type"]


def test_root_serves_injected_page():
    client = TestClient(web.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "__FREE_INPUT__" not in r.text  # 注入发生在服务端
    assert 'id="story"' in r.text
    assert "/static/app.js" in r.text

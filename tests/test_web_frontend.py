"""Web 前端守卫（Stage B：Vite + Vue 3 三栏重写之后的版本）。

**这批守卫在重写时被整体重写过一次，原因值得记下来**：
Stage A 之前/期间，前端是内嵌字符串或一份原生 JS，测试只能对**产物文本做子串断言**
（`"function startGen" in html`）。Vue 重写后产物由 Vite 生成、且组件是 `.vue` 单文件，
那类断言要么失效、要么变得与被测行为毫无关系。

现在的三层结构，每层测不同的东西：
1. **接口契约层**（本文件上半）：视图载荷、选项分流、阶段守卫 —— 用假引擎跑真 HTTP，
   断言的是 `_view()` 的形状与引擎的拒绝行为（不碰前端实现）；
2. **源码结构层**（本文件下半）：读 `webui/src/**` 断言几个**容易改错又难发现**的契约
   —— 选项按 `choice_prompt` 分流、`delta` 是裸字符串、`done.narration` 才是真值、
   禁用 `EventSource`。这些都是"写错了不报错、只表现为奇怪现象"的地方；
3. **跨层一致性**：前端 API 客户端声明的端点必须真的存在于 `web.app` 的路由表里
   （端点改名时前端会静默 404，这一条把它变成测试失败）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from fakes import FakeClient, msg, resp, tool_call

import game_agent.web as web
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.storyline import FREE_INPUT_OPTION
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"
WEBUI = Path(__file__).resolve().parent.parent / "game_agent" / "webui"


def _submit(narration: str, choices: list[str], call_id: str = "s1"):
    return tool_call(
        call_id, "submit_narration",
        {"narration": narration, "choices": choices, "plot_signal": "normal"},
    )


def _client(monkeypatch) -> TestClient:
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)

    def fake_make_game(sid: str, pack_id=None, *, draft=None, mainline_enabled=True):
        """签名必须与 `web._make_game` **逐字一致**——包括用不到的 `draft`。

        接口把新入参按关键字传下来，签名少了它就从"忽略该参数"变成
        `TypeError` → 500。签名同步不是洁癖：它在这里是接口契约的一部分。
        选包/模式/草稿在本文件的守卫里刻意不生效（那三样各有专责守卫）。
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


# ---------------------------------------------------------------------------
# 1. 接口契约层：视图载荷 + 引擎的分流/守卫行为
# ---------------------------------------------------------------------------


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
    """钉住前端必须避开的错误路由：日常状态下 pick → 引擎拒绝（SSE error 事件）。"""
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
    st = client.get(f"/api/{sid}/actions").json()
    assert st["critical"] is True
    r = client.post(f"/api/{sid}/turn", json={"kind": "act", "action_id": "cultivate"})
    assert "event: error" in r.text and "关键抉择" in r.text
    assert client.get(f"/api/{sid}/actions").json()["action_points_left"] == 1  # 未被扣
    client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
    assert client.get(f"/api/{sid}/actions").json()["critical"] is False


def test_view_payload_carries_recovery_marks(monkeypatch):
    """K 系列契约：done 事件必须带 recovered/sub_turns/turn——玩家要能看见"恢复过"。"""
    client = _client(monkeypatch)
    d = client.post("/api/new").json()
    assert d["view"]["recovered"] == [], "正常开局不该报恢复"
    assert d["view"]["sub_turns"] == 0  # "接管"与"生成"要能区分开
    assert "turn" in d["view"]

    sid = d["sid"]
    view = _done_view(client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0}).text)
    assert view["recovered"] == []
    assert view["sub_turns"] >= 1  # 抉择后是正常叙事回合


def test_config_endpoint_supplies_free_input():
    """自由输入文案由 `/api/config` 下发（Stage B 起不再改写构建产物）。"""
    d = TestClient(web.app).get("/api/config").json()
    assert d["free_input"] == FREE_INPUT_OPTION
    assert d["default_mode"] in ("story", "free")


# ---------------------------------------------------------------------------
# 2. 源码结构层：容易改错又难发现的契约
# ---------------------------------------------------------------------------


def _src(*parts: str) -> str:
    p = WEBUI.joinpath("src", *parts)
    assert p.is_file(), f"缺少前端源文件 {p.relative_to(WEBUI)}"
    return p.read_text(encoding="utf-8")


def test_column_layout_is_three_columns():
    """三栏布局（左：本局 · 中：正文+输入 · 右：状态栏）——本轮重写的核心目标。"""
    css = _src("styles.css")
    assert "grid-template-columns" in css
    assert "240px" in css and "320px" in css, "左/右栏宽度应在 CSS 里显式声明"
    app = _src("views", "PlayView.vue")
    for col in ("col-left", "col-mid", "col-right"):
        assert col in app, f"三栏缺少 {col}"


def test_choices_route_by_criticality_in_source():
    """选项分流：关键抉择走 pick(index)，日常走 say(text)，自由输入只聚焦输入框。

    这是"点错了不报错、只是被引擎拒绝"的那类契约，故源码级钉住。
    """
    c = _src("components", "ChoiceList.vue")
    assert "critical" in c and "choice_prompt" in c
    assert "emit('pick', index)" in c
    assert "emit('say', text)" in c
    assert "emit('focus-input')" in c  # 自由输入入口 → 聚焦，不当作发言
    assert "freeInput" in c  # 文案来自 /api/config，不是前后端各写一份


def test_status_panel_renders_engine_truth():
    """右栏渲染的是引擎注入给模型的**同一份** status_text()——本项目的产品差异点。"""
    s = _src("components", "StatusPanel.vue")
    assert "status" in s and "与模型看到的同源" in s
    assert "actions" in s and "critical" in s  # 关键抉择期行动区禁用并说明
    assert "结束今天" in s and "行动点已用完" in s


def test_prose_stream_keeps_player_feedback_behaviours():
    """保留三个玩家实测反馈点：默认只看本轮 / 生成计时 / 恢复痕迹落在故事分段。"""
    p = _src("components", "ProseStream.vue")
    assert "only-current" in p and "fullHistory" in p
    assert "剧情回顾" in p and "只看本轮" in p
    assert "RECOVERY_LABELS" in p and "overflow_recovered" in p and "meltdown" in p
    # 恢复痕迹写进故事分段（不能只写生成指示器——它会被清空）
    assert "recovery" in p and "recoveryNote" in p


def test_sse_contracts_are_pinned_in_source():
    """**最重要的三条前端契约**（写错了不报错、只表现为怪现象）：
    ① 不能用 EventSource（只支持 GET + 断线自动重连会把回合重跑一遍）；
    ② `delta` 的 data 是**裸 JSON 字符串**（写成 `payload.text` 会静默拿到 undefined，
       表现为"正文一直空白但状态栏正常"）；
    ③ `done.narration` 才是真值——引擎会在回合内作废重来，累积的 deltas 可能是废稿。
    """
    js = _src("composables", "useTurnStream.js")
    # 只在**注释里**提到 EventSource 是允许的（那正是"为什么不用它"的说明）；
    # 要钉的是它没有被真正**实例化**。
    assert "new EventSource" not in js, "EventSource 断线会自动重连 = 把回合重跑一遍，禁用"
    assert "getReader()" in js, "必须用 fetch + ReadableStream 手动解析 SSE"
    assert "draft.value += payload" in js and "payload // ← 裸字符串" in js
    assert "payload.narration" in js and "以终稿为准" in js
    assert "method: 'POST'" in js


def test_generation_timer_present():
    """生成计时：玩家实测"不知道是卡了还是模型在思考"。"""
    c = _src("components", "ChoiceList.vue")
    assert "已等" in c and "秒" in c and "流式输出" in c
    app = _src("views", "PlayView.vue")
    assert "setInterval" in app and "elapsed" in app


def test_stage_a_assets_are_gone():
    """回归守卫：Stage A 的原生 JS/CSS 不应复活（已由 Vue 组件取代）。"""
    for name in ("app.js", "app.css"):
        assert not (WEBUI / name).exists(), f"{name} 应已被 Vue 组件取代"
    assert not hasattr(web, "INDEX_HTML"), "前端不得退回内嵌字符串"
    assert not hasattr(web, "frontend_bundle"), "Stage A 的拼接辅助已随守卫重写移除"


# ---------------------------------------------------------------------------
# 3. 跨层一致性：前端声明的端点必须真的存在
# ---------------------------------------------------------------------------


def test_api_client_endpoints_exist_in_backend_routes():
    """端点改名时前端会**静默 404**——这一条把它变成测试失败。

    做法：从 `client.js` 的 `ENDPOINTS` 里取出 `"METHOD /path"` 字符串，
    与 `web.app` 的路由表逐个核对（`{sid}` 占位符按 FastAPI 的原样比对）。
    """
    js = _src("api", "client.js")
    # ENDPOINTS 块里的 "METHOD /api/..." 字面量
    declared = set(re.findall(r"'((?:GET|POST) /api[^']*)'", js))
    assert declared, "没有从 client.js 里解析出任何端点——ENDPOINTS 的形状变了？"

    routes: set[str] = set()
    for r in web.app.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None)
        if methods and path:
            for m in methods:
                if m in ("GET", "POST"):
                    routes.add(f"{m} {path}")

    missing = sorted(d for d in declared if d not in routes)
    assert not missing, f"前端声明了后端没有的端点：{missing}\n后端现有：{sorted(routes)}"


def test_api_client_covers_all_play_endpoints():
    """反向覆盖：玩法必需的端点一个都不能漏（防止漏改客户端）。"""
    js = _src("api", "client.js")
    for needle in (
        "GET /api/config",
        "GET /api/catalog",
        "POST /api/new",
        "GET /api/{sid}/status",
        "GET /api/{sid}/actions",
        "POST /api/{sid}/turn",
        "POST /api/{sid}/save",
        "POST /api/{sid}/load",
    ):
        assert needle in js, f"API 客户端缺少端点声明：{needle}"

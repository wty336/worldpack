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
from types import SimpleNamespace

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


def _function_body(src: str, name: str) -> str:
    """取出 `.vue`/`.js` 里 `function <name>(…)` 的函数体文本（到下一个顶层函数为止）。

    **为什么需要它**：`assert "某行代码" in 整个文件` 这种断言会被**文件里别处的
    同一行**满足。本仓库已经踩过两次同一形状的假绿：
    ① 判据写在注释里也能满足（改用 ast.unparse 剥注释）；
    ② `say(e.message, true)` 在别的 catch 块里也有，于是把 publish 里那行改成
       `say('发布失败', true)` 之后守卫照样绿。
    要钉住"**这个函数**里必须这样写"时，就必须真的只看那个函数。
    """
    key = f"function {name}("
    assert key in src, f"找不到函数 {name}——它被改名或删了？"
    rest = src[src.index(key):]
    nxt = re.search(r"\n(?:async )?function ", rest[1:])
    return rest if nxt is None else rest[: nxt.start() + 1]


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

    **方法白名单含 DELETE**（2026-10 补）：原先只收 GET/POST，于是
    `DELETE /api/packs/drafts/{name}` 声明错了也无人发现——"漏检一个方法"和
    "漏检一个端点"是同一类洞，只是更隐蔽（正则不匹配就静默跳过）。
    """
    js = _src("api", "client.js")
    # ENDPOINTS 块里的 "METHOD /api/..." 字面量
    declared = set(re.findall(r"'((?:GET|POST|DELETE) /api[^']*)'", js))
    assert declared, "没有从 client.js 里解析出任何端点——ENDPOINTS 的形状变了？"

    routes: set[str] = set()
    for r in web.app.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None)
        if methods and path:
            for m in methods:
                if m in ("GET", "POST", "DELETE"):
                    routes.add(f"{m} {path}")

    missing = sorted(d for d in declared if d not in routes)
    assert not missing, f"前端声明了后端没有的端点：{missing}\n后端现有：{sorted(routes)}"


def test_api_client_covers_all_workbench_endpoints():
    """反向覆盖（创作工作台 N2a）：生成 → 进度 → 草稿 → 发布，一个都不能漏。

    与上面那条的分工：那条防"声明了后端没有的"，这条防"后端有、客户端忘了接"
    ——后者表现为界面上少一个功能，而不是报错，最难被发现。
    """
    js = _src("api", "client.js")
    for needle in (
        "POST /api/packs/generate",
        "GET /api/packs/generate",
        "GET /api/packs/generate/{job_id}",
        "GET /api/packs/generate/{job_id}/events",
        "POST /api/packs/generate/{job_id}/cancel",
        "GET /api/packs/drafts",
        "POST /api/packs/publish",
        "DELETE /api/packs/drafts/{name}",
    ):
        assert needle in js, f"API 客户端缺少工作台端点声明：{needle}"
    # `ENDPOINTS` 只是**声明**，上面那条核对的是声明与后端路由是否一致；
    # 真正发出去的请求方法还得对得上——声明 DELETE、实发 POST 的话后端回 405，
    # 而"端点存在"这条守卫照样绿。
    #
    # ⚠️ 这里**不能**写 `assert "method: 'DELETE'" in js`：那被 `del` 辅助函数的
    # **定义**满足了，把 `deleteDraft` 改成调 `post` 之后守卫照样绿（本仓库第三个
    # 同一形状的假绿："字符串在文件里出现过"）。要钉的是**调用点**。
    #
    # 做法对**每一个** DELETE 端点逐一核对（不写死某一个名字）：漏检一个端点
    # 和漏检一个方法是同一类洞，所以这里按声明自动遍历。
    delete_keys = re.findall(r"(\w+):\s*'DELETE ", js)
    assert delete_keys, "没有从 client.js 里解析出任何 DELETE 端点——形状变了？"
    for key in delete_keys:
        m = re.search(rf"{key}:\s*\([^)]*\)\s*=>\s*(\w+)\(", js)
        assert m, f"找不到 {key} 的调用行——形状变了？"
        assert m.group(1) == "del", (
            f"{key} 调的是 {m.group(1)}()，而端点声明是 DELETE——后端会回 405"
        )


def test_workbench_view_contracts_in_source():
    """工作台的三条契约（都属于"写错了不报错、只表现成怪现象"）：

    ① **进度流用 fetch + ReadableStream，不用 `EventSource`**——本端点的订阅语义是
       "回放 + 续播"，`EventSource` 的无状态自动重连会让每次重连重收一遍历史，
       日志里出现重复行；
    ② **重连要从空列表重建**（因为服务端总会回放完整历史）——这正是"不需要去重代码"
       的原因；写成 append 就会重复；
    ③ **发布被拒时显示后端原文**——`check_worldpack` 的报错就是拿去喂模型修的燃料，
       前端把它改写成"发布失败"等于把这条链的价值丢掉一半。
    """
    js = _src("composables", "useJobStream.js")
    assert "new EventSource" not in js, "进度流不用 EventSource（重连会重收历史，见文件头）"
    assert "getReader()" in js, "必须用 fetch + ReadableStream 手动解析 SSE"
    assert "events.value = []" in js, "重连必须从空列表重建（服务端会回放完整历史）"
    assert "MAX_RECONNECT" in js, "重连要有上限，接不上就如实显示断开"

    v = _src("views", "StudioView.vue")
    assert "未通过 check-worldpack" in v, "闸门不过时必须明确说出来"
    # 报错原文有两处出口，都要在：① 报告栏渲染草稿当前的校验失败原文；
    # ② 发布被拒时把后端的 detail 原样转给提示条（不是改写成"发布失败"）。
    assert "pickedDraft.error" in v, "报告栏要渲染草稿当前校验失败的**原文**"
    # ⚠️ 这一条必须**限定在 publish 函数体内**：只查"文件里有没有
    # `say(e.message, true)`"是查不出问题的——文件里别的 catch 块也有同一行，
    # 把 publish 里的那行改成 `say('发布失败', true)` 之后守卫照样绿。
    # （这是本仓库第二次踩"字符串在文件里出现过"这种假绿，第一次是被注释满足。）
    assert "say(e.message, true)" in _function_body(v, "publish"), (
        "发布被拒时必须原样呈现后端原文（喂模型修的燃料），不能改写成「发布失败」"
    )
    assert "试玩这一版" in v, "草稿试玩入口"
    assert "offline: true" in v, "离线试跑默认开（真实生成要花钱，默认花钱是错的默认值）"
    assert "¥0.1–0.3" in v, "真实生成的成本必须前置说清楚"


def test_workbench_payloads_carry_the_fields_the_view_reads(tmp_path, monkeypatch):
    """前端从工作台端点**读**的字段，真实后端必须真的发得出来。

    **为什么单独要有这一条**（这是"桩会同意我"的洞）：
    浏览器真机冒烟（`scripts/webui_smoke.mjs`）用的是**桩后端**——不花钱、可重复，
    代价是它由我手写，于是**它会同意我关于后端形状的任何假设**。我把
    `/api/packs/generate` 的响应键记成 `jobId`、把报告字段记成 `repair_rounds`，
    桩就会照着我错的样子实现，冒烟照样 36/36，而真机上是一片空白。
    反向的那一半也有人管（`scripts/worldgen_smoke.py` 打真后端），但它验的是
    **后端自己**对不对，不看前端读了什么。这一条把两侧接上：用**真** app 跑一次
    离线生成，然后逐个断言前端依赖的键确实在。

    字段清单来自 `StudioView.vue` / `useJobStream.js` 的读取点，改名时这里必须一起改
    ——这正是我们要的（比"运行时发现是 undefined"早得多）。
    """
    import shutil

    from tests.test_jobs import _wait

    root = tmp_path / "world-packs"
    root.mkdir(parents=True)
    monkeypatch.setattr(web, "_pack_root", lambda: root)

    client = TestClient(web.app)

    # ---- 1. 起任务：前端读 d.job_id ----
    r = client.post("/api/packs/generate", json={
        "name": "contract_probe", "source_text": "# 素材\n\n某人在城里醒来。\n", "offline": True,
    })
    assert r.status_code == 202
    body = r.json()
    for key in ("ok", "job_id", "pack_name", "status"):
        assert key in body, f"前端读 /api/packs/generate 的 {key!r}，后端没发"
    job_id = body["job_id"]
    assert _wait(web.JOBS.get(job_id)).status == "done"

    # ---- 2. 终态快照：报告栏读这些 ----
    snap = client.get(f"/api/packs/generate/{job_id}").json()
    for key in ("job_id", "pack_name", "status", "error", "cost", "elapsed", "result"):
        assert key in snap, f"前端读任务快照的 {key!r}，后端没发"
    for key in ("repairs", "corpus_written", "stages", "summary"):
        assert key in (snap["result"] or {}), f"报告栏读 result.{key}，后端没发"

    # ---- 3. 进度事件：时间线读 stage / message ----
    text = client.get(f"/api/packs/generate/{job_id}/events").text
    progress = [
        json.loads(b.split("data: ", 1)[1])
        for b in text.split("\n\n")
        if b.startswith("event: progress")
    ]
    assert progress, "事件流里没有任何 progress 帧——进度面板会是空的"
    for ev in progress:
        assert "stage" in ev and "message" in ev, f"进度事件缺 stage/message：{ev}"
    assert any(ev["stage"] == "done" for ev in progress), "没有终态事件，前端会一直等"

    # ---- 4. 草稿项：列表与报告栏读这些 ----
    draft = next(d for d in client.get("/api/packs/drafts").json()["drafts"]
                 if d["id"] == "contract_probe")
    for key in ("id", "name", "npcs", "nodes", "endings", "lore", "locations",
                "digest", "path", "draft", "playable", "error"):
        assert key in draft, f"前端读草稿项的 {key!r}，后端没发"
    assert draft["draft"] is True and draft["playable"] is True

    # ---- 5. 草稿试玩：App.vue 读 d.draft 决定要不要打「未发布」水印 ----
    # 只换掉"花钱的那部分"（LLM），选包与建局都跑**真实**代码——否则测的就是假件了。
    from game_agent.game import Game
    from game_agent.worldpack import load_worldpack as _load

    def fake_make_game(sid, pack_id=None, *, draft=None, mainline_enabled=True):
        path, _ = web._resolve_pack(pack_id, draft)  # 真实选包（含草稿查表）
        pack = _load(path)
        llm = LLMClient(
            FakeClient([resp(msg(tool_calls=[_submit("开场。", ["甲", "乙", "丙"])]))]),
            "fake", build_tools(pack.schedule),
        )
        return (
            Game(pack, GameState.from_pack(pack), llm, mainline_enabled=mainline_enabled),
            web.UsageTracker(str(tmp_path / "u.jsonl"), session=sid),
        )

    monkeypatch.setattr(web, "_make_game", fake_make_game)
    d = client.post("/api/new", json={"draft": "contract_probe"}).json()
    assert d.get("draft") is True, "草稿试玩没有 draft 标记 → 前端不会打「未发布」水印"
    assert d["pack_id"] == "contract_probe"
    for key in ("sid", "pack_id", "mode", "view"):
        assert key in d, f"前端开局读 {key!r}，后端没发"

    shutil.rmtree(root, ignore_errors=True)


def test_library_exposes_studio_entry_and_hides_drafts():
    """选卡屏要有工作台入口，且**草稿不许混进"选一张卡"**（玩家会点到半成品）。"""
    lib = _src("views", "LibraryView.vue")
    assert "emit('studio')" in lib and "创作工作台" in lib
    assert "drafts" not in lib, "草稿的试玩入口在工作台里，不在这屏"

    app = _src("App.vue")
    assert "StudioView" in app and "'studio'" in app
    assert "session.draft" in app, "草稿试玩要在顶栏打「未发布」水印"


def test_creator_chat_contracts_in_source():
    """创作者 Agent 对话流的三条契约（N6）。

    ① **断线不重连**——这一条与 `useJobStream` 正好相反，两者容易互相抄错：
       后台任务流是 GET + 服务端回放，重连安全；对话流是一个 POST 触发一次
       "跑一段就没了"的工作，重连会把同一句话**执行第二遍**（重复扣费、重复改草稿）。
       所以这里断言它**没有**重连机制，而 `useJobStream` 有——两条一起钉住，
       才不会有人"统一一下风格"把其中一个改坏。
    ② 同样不能用 `EventSource`（只支持 GET + 无状态自动重连）。
    ③ `done.reply` 才是最终答复（中间 `text` 事件只是过程）。
    """
    c = _src("composables", "useCreatorStream.js")
    assert "new EventSource" not in c, "EventSource 只支持 GET，且重连会把这一轮重跑"
    assert "getReader()" in c, "必须用 fetch + ReadableStream 手动解析 SSE"
    assert "method: 'POST'" in c
    assert "MAX_RECONNECT" not in c, (
        "对话流**不许**自动重连：重连 = 把同一句话再执行一遍（重复改草稿）"
    )
    # 流断了且没收到 done 时必须**如实说明**，而不是静默当成成功
    assert "连接中断" in c, "断线要如实告诉作者（改动可能只落了一部分）"
    assert "这一轮连接中断" in c or "改动可能只落了一部分" in c
    assert "result.value = payload" in c, "done 载荷要留着（校验结论 + diff 靠它）"

    j = _src("composables", "useJobStream.js")
    assert "MAX_RECONNECT" in j, (
        "后台任务流**必须**能重连（服务端会回放完整历史，重连是安全的）"
        "——与对话流的取舍相反，两条一起钉住"
    )


def test_creator_panel_is_wired_into_the_workbench():
    """工作台要有两个模式与对话面板；**右栏仍然不许有输入框**。"""
    v = _src("views", "StudioView.vue")
    assert "从素材生成一张卡" in v and "和 Agent 改这一版" in v, "两个模式入口"
    assert "useCreatorStream" in v
    assert "chatInput" in v and "发送" in v, "对话输入"
    assert "清空对话上下文" in v, "重置只丢上下文、不动内容——这个动作要可见"
    assert "复制成草稿" in v and "api.fork" in v, "改现成的卡要先 fork"
    assert "工作版 vs 会话基线" in v, "diff 要给人看（确认改了什么再发布）"
    assert "creator.result.value.validate_text" in v, (
        "Agent 改完后的校验原文要显示——不许只显示一个红点"
    )
    # 右栏"没有输入框"这条**不在源码层断言**：`<input>` 出现在中栏是合法的，
    # 按文本判断左右栏只会写出一个脆弱的正则。它由浏览器真机冒烟直接断言
    # `#app .studio .col-right input` 数量为 0——那才是这个契约真正成立的地方。


def test_workbench_payloads_carry_the_fields_the_view_reads_creator(tmp_path, monkeypatch):
    """创作者端点的载荷必须带上前端读的键（N6 版的"桩会同意我"防线）。

    与工作台那条同一理由：浏览器冒烟用桩后端，桩由我手写，于是**它会同意我关于
    后端形状的任何假设**。这里用真 app 跑一轮脚本化的对话，逐个断言键真的存在。
    """
    import shutil

    from fastapi.testclient import TestClient
    from fakes import FakeClient, msg, resp, tool_call

    import game_agent.creator as creator_mod
    import game_agent.web as web
    from game_agent.llm import LLMClient

    root = tmp_path / "world-packs"
    root.mkdir(parents=True)
    shutil.copytree(PACK_PATH, root / "published")
    shutil.copytree(PACK_PATH, root / "_drafts" / "probe")
    monkeypatch.setattr(web, "_pack_root", lambda: root)
    monkeypatch.setattr(web, "CREATORS", {})
    monkeypatch.setattr(web, "load_settings", lambda: SimpleNamespace(has_api_key=True))

    npc = next((root / "_drafts" / "probe" / "npcs").glob("*.yaml")).stem
    fake = LLMClient(FakeClient([
        resp(msg(tool_calls=[tool_call(
            "c1", "update_npc_field",
            {"npc_id": npc, "field": "personality", "value": "改过"})])),
        resp(msg(tool_calls=[tool_call("c2", "validate_pack", {})])),
        resp(msg(content="改好了。")),
    ]), "fake", [])
    monkeypatch.setattr(creator_mod, "build_creator_llm",
                        lambda settings, tracker=None: fake)
    client = TestClient(web.app)

    r = client.post("/api/creator/probe/chat", json={"message": "改性格"})
    frames = {}
    for block in r.text.split("\n\n"):
        if not block.strip():
            continue
        event, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if data:
            frames.setdefault(event, []).append(json.loads(data))

    assert "start" in frames, "首帧心跳（前端据此切'处理中'）"
    assert "tool" in frames, "工具过程事件"
    for ev in frames["tool"]:
        for key in ("type", "name", "status", "result"):
            assert key in ev, f"tool 事件缺 {key}：{ev}"
    done = frames["done"][0]
    for key in ("reply", "tools", "validate_ok", "validate_text", "changed", "diff",
                "truncated"):
        assert key in done, f"done 事件缺前端要读的 {key}"

    state = client.get("/api/creator/probe").json()
    for key in ("ok", "name", "open", "messages", "summary", "diff", "changed",
                "validate_ok", "validate_text"):
        assert key in state, f"创作会话现状缺 {key}"

    fork = client.post("/api/packs/fork", json={"name": "published"}).json()
    assert "draft" in fork and fork["draft"]["id"] == "published"

    shutil.rmtree(root, ignore_errors=True)


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

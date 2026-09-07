"""F5（P3）Web 前端：FastAPI 会话 API + SSE 流式回合 + 极简聊天页。

端点：
- GET  /                    → 极简聊天页（内嵌单文件）
- POST /api/new             → 新会话（开局），返回 {sid, view}
- POST /api/{sid}/turn      → SSE 流式回合（kind: say/pick/act/start）
- GET  /api/{sid}/status    → 状态栏文本
- GET  /api/{sid}/actions   → 可用日程行动
- POST /api/{sid}/save      → 存档；POST /api/{sid}/load → 读档

验收流：浏览器内完成开局 → 关键抉择 → 存档读档全流程。
MVP 边界：单进程内存会话；需 DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import json
import queue
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .config import load_settings
from .game import Game, GameError
from .llm import LLMClient, LLMTurnError, build_tools
from .save import load_game, load_history, save_game
from .state import GameState
from .storyline import StorylineError
from .usage import UsageTracker
from .worldpack import WorldPackError, load_worldpack

DEFAULT_PACK = "world-packs/ancient_jianghu"

app = FastAPI(title="game-agent web", docs_url=None, redoc_url=None)

SESSIONS: dict[str, Game] = {}


# ---------------------------------------------------------------------------
# 会话与视图序列化
# ---------------------------------------------------------------------------


def _make_game() -> Game:
    settings = load_settings()
    if not settings.has_api_key:
        raise HTTPException(500, "未配置 DEEPSEEK_API_KEY")
    pack = load_worldpack(DEFAULT_PACK)
    state = GameState.from_pack(pack)
    llm = LLMClient.from_settings(
        settings, build_tools(pack.schedule), tracker=UsageTracker("saves/usage-web.jsonl")
    )
    return Game(
        pack, state, llm, autosave_path="saves/autosave.json",
        extract_every=2, compress_threshold=30000, judge_every=5, reflect_every=10,
    )


def _ensure_game(sid: str) -> Game:
    game = SESSIONS.get(sid)
    if game is None:
        raise HTTPException(404, f"会话不存在: {sid}")
    return game


def _view(game: Game, view) -> dict:
    """TurnView → JSON（含结局/关键抉择信息，前端据此渲染）。"""
    return {
        "narration": view.narration,
        "choices": view.choices,
        "briefing": view.briefing,
        "ending": (
            {"title": view.ending.title, "text": view.ending.text} if view.ending else None
        ),
        "choice_prompt": (
            {"prompt": view.choice_prompt.prompt} if view.choice_prompt else None
        ),
    }


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


class TurnRequest(BaseModel):
    kind: str  # "say" / "pick" / "act" / "start"
    text: str | None = None
    index: int | None = None
    action_id: str | None = None


class SaveRequest(BaseModel):
    path: str = "saves/web.json"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.post("/api/new")
def api_new() -> dict:
    game = _make_game()
    view = game.start()
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = game
    return {"sid": sid, "view": _view(game, view)}


@app.get("/api/{sid}/status")
def api_status(sid: str) -> dict:
    game = _ensure_game(sid)
    return {"text": game.status_text(), "ending": bool(game.ending)}


@app.get("/api/{sid}/actions")
def api_actions(sid: str) -> dict:
    game = _ensure_game(sid)
    return {
        "day": game.state.day,
        "action_points_left": game.state.action_points_left,
        "actions": [{"id": a.id, "label": a.label} for a in game.actions_available()],
    }


@app.post("/api/{sid}/turn")
def api_turn(sid: str, req: TurnRequest) -> StreamingResponse:
    """SSE 流式回合：delta 事件 = 叙事增量；done 事件 = 完整视图 JSON。"""
    game = _ensure_game(sid)
    deltas: queue.Queue[tuple[str, str]] = queue.Queue()

    def on_text(piece: str) -> None:
        deltas.put(("delta", piece))

    game.on_text = on_text

    def gen():
        try:
            if req.kind == "start":
                view = game.start()
            elif req.kind == "say":
                if not req.text or not req.text.strip():
                    yield _sse("error", "输入不能为空")
                    return
                view = game.say(req.text)
            elif req.kind == "pick":
                view = game.pick(req.index or 0)
            elif req.kind == "act":
                view = game.act(req.action_id or "")
            else:
                yield _sse("error", f"未知回合类型 {req.kind}")
                return
        except GameError as e:
            yield _sse("error", str(e))
            return
        except StorylineError as e:
            yield _sse("error", str(e))
            return
        except LLMTurnError as e:
            yield _sse("error", f"生成失败（协议熔断）: {e}")
            return
        while not deltas.empty():
            event, piece = deltas.get_nowait()
            yield _sse(event, json.dumps(piece, ensure_ascii=False))
        yield _sse("done", json.dumps(_view(game, view), ensure_ascii=False))

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/{sid}/save")
def api_save(sid: str, req: SaveRequest) -> dict:
    game = _ensure_game(sid)
    save_game(game.state, req.path, game.history)
    return {"ok": True, "path": req.path}


@app.post("/api/{sid}/load")
def api_load(sid: str, req: SaveRequest) -> dict:
    game = _ensure_game(sid)
    try:
        game.state = load_game(req.path)
        game.history = load_history(req.path)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, f"读档失败: {e}")
    game.ending = None
    return {"ok": True, "path": req.path, "status": game.status_text()}


# ---------------------------------------------------------------------------
# 极简聊天页（内嵌单文件，无构建）
# ---------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>江湖旧梦 · Web</title>
<style>
  body { font-family: "Noto Serif SC", serif; max-width: 760px; margin: 0 auto;
         padding: 16px; background: #faf6ef; color: #333; }
  h1 { font-size: 1.3em; margin: 8px 0; }
  #story { white-space: pre-wrap; line-height: 1.9; background: #fff;
           border: 1px solid #e6dcc8; border-radius: 8px; padding: 16px; min-height: 160px; }
  #prompt { color: #8a6d3b; margin: 10px 0; white-space: pre-wrap; }
  #choices { margin: 10px 0; }
  button { background: #f7efe0; border: 1px solid #d9c9a8; border-radius: 6px;
           padding: 8px 14px; margin: 4px 6px 4px 0; cursor: pointer; font-size: .95em; }
  button:hover { background: #efe3c8; }
  #inputrow { display: flex; gap: 6px; margin-top: 12px; }
  #in { flex: 1; padding: 8px; border: 1px solid #d9c9a8; border-radius: 6px; }
  #status { margin-top: 14px; font-size: .85em; color: #777; white-space: pre-wrap;
            border-top: 1px dashed #d9c9a8; padding-top: 10px; }
  .t { color: #999; font-size: .8em; }
</style>
</head>
<body>
<h1>江湖旧梦（Web 演示）</h1>
<div id="story">（正在开局……）</div>
<div id="prompt"></div>
<div id="choices"></div>
<div id="inputrow">
  <input id="in" placeholder="说些什么……（回车发送）">
  <button onclick="say()">发言</button>
  <button onclick="doSave()">存档</button>
  <button onclick="doLoad()">读档</button>
</div>
<div id="status"></div>
<script>
let sid = null;
const $ = (id) => document.getElementById(id);
const story = $("story"), promptEl = $("prompt"), choicesEl = $("choices"), statusEl = $("status");

async function start() {
  const r = await fetch("/api/new", { method: "POST" });
  const d = await r.json();
  sid = d.sid;
  render(d.view);
  await refreshStatus();
}
function render(v) {
  if (v.narration) story.textContent = v.narration;
  if (v.briefing) story.textContent = v.briefing + "\\n";
  promptEl.textContent = v.choice_prompt ? ("【关键抉择】" + v.choice_prompt.prompt) : "";
  choicesEl.innerHTML = "";
  (v.choices || []).forEach((c, i) => {
    const b = document.createElement("button");
    b.textContent = c;
    b.onclick = () => turn({ kind: "pick", index: i });
    choicesEl.appendChild(b);
  });
  if (v.ending) {
    promptEl.textContent = "『" + v.ending.title + "』\\n" + (v.ending.text || "");
    choicesEl.innerHTML = "";
  }
}
async function turn(req) {
  const resp = await fetch("/api/" + sid + "/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  story.textContent = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\\n\\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const ev = block.match(/^event: (\\w+)/m);
      const data = block.match(/^data: (.*)$/m);
      if (!ev || !data) continue;
      if (ev[1] === "delta") story.textContent += JSON.parse(data[1]);
      else if (ev[1] === "done") render(JSON.parse(data[1]));
      else if (ev[1] === "error") promptEl.textContent = "[错误] " + JSON.parse(data[1]);
    }
  }
  await refreshStatus();
}
function say() {
  const t = $("in").value.trim();
  if (!t) return;
  $("in").value = "";
  turn({ kind: "say", text: t });
}
$("in").addEventListener("keydown", (e) => { if (e.key === "Enter") say(); });
async function refreshStatus() {
  const r = await fetch("/api/" + sid + "/status");
  const d = await r.json();
  statusEl.textContent = d.text || "";
  const ar = await fetch("/api/" + sid + "/actions");
  const ad = await ar.json();
  statusEl.textContent += "\\n[第 " + ad.day + " 天 · 行动点 " + ad.action_points_left + "] ";
  ad.actions.forEach((a) => {
    const b = document.createElement("button");
    b.textContent = a.label;
    b.onclick = () => turn({ kind: "act", action_id: a.id });
    statusEl.appendChild(b);
  });
}
async function doSave() { const r = await fetch("/api/" + sid + "/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: "saves/web.json" }) }); alert((await r.json()).ok ? "已存档" : "失败"); }
async function doLoad() { const r = await fetch("/api/" + sid + "/load", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: "saves/web.json" }) }); const d = await r.json(); if (d.ok) { story.textContent = "（已读档）"; promptEl.textContent = ""; choicesEl.innerHTML = ""; statusEl.textContent = d.status; } else alert("读档失败"); }
start();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML

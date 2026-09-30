"""F5（P3）Web 前端：FastAPI 会话 API + SSE 流式回合 + 极简聊天页。

端点：
- GET  /                    → 聊天页（`game_agent/webui/index.html`，注入自由输入文案）
- GET  /static/*            → 前端静态资源（app.css / app.js）
- POST /api/new             → 新会话（开局），返回 {sid, name, view}
- POST /api/{sid}/turn      → SSE 流式回合（kind: say/pick/act/end_day/start）
- GET  /api/{sid}/status    → 状态栏文本
- GET  /api/{sid}/actions   → 可用日程行动
- POST /api/{sid}/save      → 存档；POST /api/{sid}/load → 读档

验收流：浏览器内完成开局 → 关键抉择 → 存档读档全流程。
MVP 边界：单进程内存会话；需 DEEPSEEK_API_KEY。

**平台化批次 1（2026-10）**：
- Stage A：前端从本文件的内嵌字符串拆成 `game_agent/webui/` 下的真实文件
  （`index.html` / `app.css` / `app.js`），本模块只管服务与注入，不再承载 250 行前端源码；
- G1 记账：`UsageTracker` 由**进程级单例**改为**每会话一个账本**（`usage-<sid>.jsonl`），
  修掉"多会话共写一个文件、无法回答某局花多少钱"的数据错误；
- G2 存档：存档带剧本身份戳（`pack: {id, digest}`），读档不一致**拒绝并提示**。
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_settings, resolve_context_window
from .game import Game, GameError
from .llm import LLMClient, LLMTurnError, build_tools
from .save import PackMismatchError, load_game, load_history, save_game
from .schedule import ScheduleError
from .state import GameState
from .storyline import FREE_INPUT_OPTION, StorylineError
from .usage import UsageTracker
from .worldpack import WorldPackError, load_worldpack

DEFAULT_PACK = "world-packs/ancient_jianghu"
SAVE_ROOT = Path("saves").resolve()  # A-1：存档根目录（路径穿越防御）
WEBUI_DIR = Path(__file__).resolve().parent / "webui"  # Stage A：前端真实文件目录

app = FastAPI(title="game-agent web", docs_url=None, redoc_url=None)


def _pack_path() -> str:
    """当前 Web 服务使用的世界包路径（M3 换包即玩）。

    默认 DEFAULT_PACK；CLI `web --pack` 通过环境变量 GAME_WORLDPACK 覆盖——
    uvicorn 以 "game_agent.web:app" 启动时无法传参，环境变量是免改代码的通道。

    （会话级选包见 `docs/plan-tavern-shaped-product.md` §2.1 的 E-3，尚未实现。）
    """
    return os.environ.get("GAME_WORLDPACK", DEFAULT_PACK)


def _usage_path(sid: str) -> str:
    """G1：**按会话隔离**的成本账本路径。

    为什么用独立文件而不是"一个文件加 session 字段"：文件级隔离让"这一局花了多少钱"
    成为一次读文件即可回答的问题，也不存在并发追加的串写面。条目里同时带 `session`
    字段（见 `UsageTracker`），跨会话聚合读仍然可行。
    """
    return f"saves/usage-{sid}.jsonl"


@dataclass
class Session:
    """B-4（M4）：一个游戏会话 = game + 串行化锁。"""

    game: Game
    lock: threading.Lock
    usage: UsageTracker  # G1：本会话专属账本（此前是进程级共享单例）


SESSIONS: dict[str, Session] = {}


# ---------------------------------------------------------------------------
# 会话与视图序列化
# ---------------------------------------------------------------------------


def _make_game(sid: str) -> tuple[Game, UsageTracker]:
    """建一局。返回 (game, tracker)——tracker 由调用方存进 Session（G1）。"""
    settings = load_settings()
    if not settings.has_api_key:
        raise HTTPException(500, "未配置 DEEPSEEK_API_KEY")
    pack = load_worldpack(_pack_path())
    state = GameState.from_pack(pack)
    tracker = UsageTracker(_usage_path(sid), session=sid)  # G1：本会话专属账本
    llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)
    game = Game(
        pack, state, llm,
        autosave_path=f"saves/autosave-{sid}.json",  # B-4：按会话隔离，避免互覆
        extract_every=2, compress_threshold=30000, judge_every=5, reflect_every=10,
        critique_on_critical=True,  # agent-first 第 2 件：关键节点内轮自校正
        plan_node=True,  # agent-first 第 4 件：节点目标拆子步骤
        factcheck_every=1,  # 设计加固 B1：缺席证据检查每轮常开（确定性层）
        context_window=resolve_context_window(settings),  # J 系列：显式配置 > 端点自报
    )
    return game, tracker


def _ensure_session(sid: str) -> Session:
    session = SESSIONS.get(sid)
    if session is None:
        raise HTTPException(404, f"会话不存在: {sid}")
    return session


def _view(game: Game, view) -> dict:
    """TurnView → JSON（含结局/关键抉择信息，前端据此渲染）。

    `recovered` / `sub_turns` / `turn`（J 系列）让前端能说清"这一轮内部发生了什么"：
    此前玩家只感知到"这轮慢"或"本轮生成失败"，分不出"发生过恢复并成功"与"压根没试过"
    ——而溢出恢复会让一轮多花一次压缩调用（数秒），熔断兜底则直接换成了保守文案。
    """
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
        "recovered": list(view.recovered),  # 本回合用过的恢复手段（正常回合为空）
        "sub_turns": view.sub_turns,  # 本回合实际跑了几次生成（含级联）
        "turn": game.state.turn_count,  # 统一回合轴（与 trace 的 game_turn 同源）
    }


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


class TurnRequest(BaseModel):
    kind: str  # "say" / "pick" / "act" / "end_day" / "start"
    text: str | None = None
    index: int | None = None
    action_id: str | None = None


class SaveRequest(BaseModel):
    path: str = "web.json"  # A-1：仅接受 saves/ 内的裸文件名


def _safe_save_path(raw: str) -> Path:
    """A-1（审查修复 C1）：存档路径约束——**严格拒绝**一切非裸文件名。

    客户端可控的 path 直传文件 API 是任意路径读写洞（../ 覆写 .env/世界包等）。
    规则：path 必须是 saves/ 根下的 .json 裸文件名（不含目录成分/盘符）。
    """
    name = Path(raw).name
    if raw != name or not re.fullmatch(r"[\w\-.]{1,128}\.json", name):
        raise HTTPException(400, "非法存档路径：仅允许 saves/ 内的 .json 文件名")
    p = (SAVE_ROOT / name).resolve()
    if not p.is_relative_to(SAVE_ROOT):  # 防御纵深
        raise HTTPException(400, "非法存档路径")
    return p


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.post("/api/new")
def api_new() -> dict:
    sid = uuid.uuid4().hex[:12]
    game, tracker = _make_game(sid)  # B-4：autosave 按 sid 命名，需先生成 sid
    session = Session(game=game, lock=threading.Lock(), usage=tracker)
    with session.lock:
        view = game.start()
        SESSIONS[sid] = session
    # name：世界包名随会话返回，前端据此渲染标题（F1 修复：引擎页面不含世界内容文案）
    return {"sid": sid, "name": game.pack.world.name, "view": _view(game, view)}


@app.get("/api/{sid}/status")
def api_status(sid: str) -> dict:
    game = _ensure_session(sid).game
    return {"text": game.status_text(), "ending": bool(game.ending)}


@app.get("/api/{sid}/actions")
def api_actions(sid: str) -> dict:
    game = _ensure_session(sid).game
    return {
        "day": game.state.day,
        "action_points_left": game.state.action_points_left,
        "critical": game.story.choice_locked(game.state),  # 前端据此禁用行动区
        "actions": [{"id": a.id, "label": a.label} for a in game.actions_available()],
    }


@app.get("/api/{sid}/cost")
def api_cost(sid: str) -> dict:
    """G1 配套：**本会话**的成本报告（此前无法回答"这一局花了多少钱"）。

    这是 G1 修复的可观测面：账本按会话隔离后，本端点的数字天然只含本局。
    `calls` 统计本进程内记过的调用次数；`report` 是同一份数据的人读形式。
    """
    session = _ensure_session(sid)
    return {
        "ok": True,
        "calls": len(session.usage.entries),
        "path": str(session.usage.path),
        "report": session.usage.cost_report(),
    }


@app.post("/api/{sid}/turn")
def api_turn(sid: str, req: TurnRequest) -> StreamingResponse:
    """SSE 流式回合：delta 事件 = 叙事增量（真流式，边生成边到达）；done = 完整视图。

    B-1（M3）：LLM 调用在工作线程执行，生成器阻塞消费队列——客户端在生成期间
    即收到增量；B-2（M1）：error 帧与 delta/done 一样经 json.dumps（前端可解析、
    含换行不撕裂帧）；B-3（M2）：ScheduleError 等全部转 error 帧而非断流；
    B-4（M4）：整个回合持有会话锁，同 sid 回合串行化。
    """
    session = _ensure_session(sid)
    return StreamingResponse(_turn_stream(session, req), media_type="text/event-stream")


def _turn_stream(session: Session, req: TurnRequest):
    """回合流生成器（同步）：工作线程跑 dispatch，本生成器阻塞消费队列至哨兵。

    首帧心跳（D15 待办）：生成器体要先被消费才会执行，而此前**第一个 `yield`
    发生在收到 delta 或哨兵之时**——纯生成式的回合（模型先长时间思考再调工具）
    没有任何 delta，于是客户端要等到**整轮结束**才收到第一个字节，
    "流式"在体感上退化成"转圈等到最后一次性出结果"。
    故在**启动工作线程之后、消费队列之前**先 `yield` 一帧 `start` 心跳：
    它既是"连接已建立"的确认，也确实发生在回合进行中（前端据此切"生成中"指示器）。
    """
    deltas: queue.Queue = queue.Queue()
    holder: dict = {}

    def dispatch() -> None:
        """工作线程：执行回合分发，把流式增量与结果放入队列，以 None 哨兵收尾。

        **锁在 worker 里取**（T4 修复）：此前 `with session.lock` 包的是下面的
        生成器循环，客户端断线时生成器被 `GeneratorExit` 关闭、锁在 `yield` 处释放，
        而本线程仍在改 `session.game` —— B-4 的"同 sid 回合串行化"静默失效，
        且被放弃的回合的效果会落到之后重绑的状态上（例如刚 /load 的存档）。
        锁的生存期应当 = **本地真值可能被修改的时长**，与客户端是否在线无关。
        """
        with session.lock:  # B-4：同 sid 回合串行化（覆盖整个回合，含断线后）
            game = session.game
            game.on_text = _make_stream_sink(deltas)
            try:
                if req.kind == "start":
                    view = game.start()
                elif req.kind == "say":
                    if not req.text or not req.text.strip():
                        raise ValueError("输入不能为空")
                    view = game.say(req.text)
                elif req.kind == "pick":
                    view = game.pick(req.index or 0)
                elif req.kind == "act":
                    view = game.act(req.action_id or "")
                elif req.kind == "end_day":
                    # 玩家反馈补齐：Web 此前没有结束今天的入口（CLI 有 /end）——
                    # 行动点耗尽后玩家会被永远卡在同一天。
                    # end_day 现在是叙事化回合（时序过渡场景），不再是裸日期标记
                    view = game.end_day()
                else:
                    raise ValueError(f"未知回合类型 {req.kind}")
                holder["view"] = view
            except (GameError, StorylineError, ScheduleError) as e:
                deltas.put(("error", str(e)))
            except LLMTurnError as e:
                deltas.put(("error", f"生成失败（协议熔断）: {e}"))
            except ValueError as e:
                deltas.put(("error", str(e)))
            except Exception as e:  # noqa: BLE001 — 审查修复：未预期异常转 error 事件，不静默杀回合
                deltas.put(("error", f"内部错误: {type(e).__name__}: {e}"))
            finally:
                deltas.put(None)  # 结束哨兵

    threading.Thread(target=dispatch, daemon=True).start()
    # 心跳放在**启动工作线程之后**：这样"首帧到达"确实意味着回合已在处理中，
    # 而不只是"连接建立了"。（放在线程启动之前也改善体感，但语义弱一档。）
    yield _sse("start", json.dumps({"kind": req.kind}, ensure_ascii=False))
    try:
        while True:
            item = deltas.get()  # 阻塞等待流式增量与哨兵
            if item is None:
                break
            event, payload = item
            yield _sse(event, json.dumps(payload, ensure_ascii=False))  # B-2
        if "view" in holder:
            yield _sse("done", json.dumps(_view(session.game, holder["view"]), ensure_ascii=False))
    finally:
        # T5：客户端断线时生成器被关闭（GeneratorExit），但**工作线程仍在跑**——
        # 这是故意的（B-4/T4：锁的生存期与客户端是否在线无关，回合必须跑完、
        # 效果必须落盘）。这里只做两件不干扰 worker 的清理：解绑 on_text、把
        # 无人消费的队列排空，避免"被遗弃的队列 + 仍在推送的回调"在长局里积压。
        _release_stream(session, deltas)


def _make_stream_sink(deltas: "queue.Queue"):
    """`game.on_text` 的 SSE 转发回调，并**自报归属队列**。

    `_stream_queue` 属性让断线清理能判断"现在绑的是不是我这条流"——同一 sid 的新回合
    会重绑，此时旧流不得解绑（否则会把新流的回调清掉，玩家看不到后续增量）。
    """

    def sink(piece: str) -> None:
        deltas.put(("delta", piece))

    sink._stream_queue = deltas  # type: ignore[attr-defined]
    return sink


def _release_stream(session: Session, deltas: "queue.Queue") -> None:
    """断开连接后的清理（幂等；worker 可能仍在跑，故不得抛异常）。

    只解绑**确实属于本流**的回调：worker 每次回合开始都重绑 `game.on_text`，
    若此刻绑的已不是本流的回调，说明同一 sid 的新回合已接管，不能动。
    判据是回调上的显式标记 `_stream_queue`（不是内省 `__closure__` 单元——
    后者依赖实现细节，改名/包装一层就会静默失效）。
    """
    game = session.game
    handler = getattr(game, "on_text", None)
    if handler is not None and getattr(handler, "_stream_queue", None) is deltas:
        game.on_text = None
    while True:  # 排空被遗弃的增量（哨兵之后 worker 不再推送）
        try:
            deltas.get_nowait()
        except queue.Empty:
            break


@app.post("/api/{sid}/save")
def api_save(sid: str, req: SaveRequest) -> dict:
    session = _ensure_session(sid)
    path = _safe_save_path(req.path)  # A-1：约束后的存档路径
    with session.lock:  # B-4：与流式回合互斥，存档的是完整回合后的状态
        # G2：写入剧本身份戳，读档时据此拒绝"改版后读旧档"
        save_game(
            session.game.state, path, session.game.history, pack_meta=session.game.pack_meta
        )
    return {"ok": True, "path": str(path)}


@app.post("/api/{sid}/load")
def api_load(sid: str, req: SaveRequest) -> dict:
    session = _ensure_session(sid)
    path = _safe_save_path(req.path)  # A-1：约束后的存档路径
    with session.lock:
        try:
            # G2：先校验剧本身份（不一致抛 PackMismatchError，属 ValueError）；
            # 校验发生在赋值之前，因此失败时不会留下半应用的状态。
            state = load_game(path, pack_meta=session.game.pack_meta)
            history = load_history(path)
        except FileNotFoundError as e:
            raise HTTPException(400, f"读档失败: 存档不存在（{e}）")
        except PackMismatchError as e:
            raise HTTPException(400, f"读档被拒绝：{e}")  # 明确拒绝，不静默降级
        except ValueError as e:
            raise HTTPException(400, f"读档失败: {e}")
        session.game.state = state
        session.game.history = history
        session.game.ending = None
        status = session.game.status_text()
    return {"ok": True, "path": str(path), "status": status}



# ---------------------------------------------------------------------------
# 前端（Stage A：真实文件，不是内嵌字符串）
# ---------------------------------------------------------------------------
#
# 拆分理由见 docs/plan-tavern-shaped-product.md §5.3：内嵌字符串让"改一行 CSS"
# 也要动 Python，且无法被前端工具链消化。拆开后 web.py 只负责**服务与注入**。


def index_html() -> str:
    """渲染入口页：把自由输入文案注入占位符。

    每次请求都读盘（本地单用户应用，一次文件读可忽略）：这样改前端不必重启服务，
    迭代体感与"热更新"接近。文案仍来自引擎常量 `FREE_INPUT_OPTION`，单一真源。
    """
    html = (WEBUI_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__FREE_INPUT__", FREE_INPUT_OPTION)


def frontend_bundle() -> str:
    """渲染后的 HTML + CSS + JS 拼接（**供结构守卫用**）。

    Stage A 之前 `INDEX_HTML` 是一个字符串，前端结构断言直接对它做子串检查。
    拆成三个文件后，那些守卫（选项按 choice_prompt 分流、生成态、结束今天、恢复
    痕迹、回顾切换）需要同一个可断言的整体——本函数就是那个整体，
    语义等价于拆分前的 `INDEX_HTML`（占位符已替换）。
    """
    return "\n".join([
        index_html(),
        (WEBUI_DIR / "app.css").read_text(encoding="utf-8"),
        (WEBUI_DIR / "app.js").read_text(encoding="utf-8"),
    ])


app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return index_html()

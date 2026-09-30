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

from . import catalog, creator, jobs
from .config import load_settings, resolve_context_window
from .game import Game, GameError
from .jobs import GenerationJob
from .llm import LLMClient, LLMTurnError, build_tools
from .save import (
    PackMismatchError,
    load_game,
    load_history,
    save_game,
    save_summary,
    state_pack_mismatch,
)
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
    """当前 Web 服务的**默认**世界包路径（M3 换包即玩）。

    默认 DEFAULT_PACK；CLI `web --pack` 通过环境变量 GAME_WORLDPACK 覆盖——
    uvicorn 以 "game_agent.web:app" 启动时无法传参，环境变量是免改代码的通道。

    E-3 起它降级为**缺省值**：会话可以在 `POST /api/new` 里显式指定 `pack_id`
    （见 `_resolve_pack`）。环境变量仍然有效，且允许指向 `world-packs/` 之外的
    任意目录（CLI `--pack some/dir` 的既有用法）。
    """
    return os.environ.get("GAME_WORLDPACK", DEFAULT_PACK)


def _pack_root() -> Path:
    """世界包目录（目录层的扫描根）。"""
    return Path(DEFAULT_PACK).parent


def _require_pack(pack_id: str) -> catalog.PackEntry:
    """校验客户端传来的 `pack_id`；不合法直接 400。

    **刻意放在 HTTP 层**（`api_new` 里调），而不是只埋在 `_make_game` 内部：
    请求参数的校验属于接口契约，测试替换掉 `_make_game`（离线夹具的常规做法）
    不该让契约一起消失。`_resolve_pack` 里仍保留一次检查做纵深防御。
    """
    entry = catalog.resolve_pack(pack_id, _pack_root())
    if entry is None:
        raise HTTPException(400, f"未知的世界包: {pack_id!r}")
    if not entry.playable:
        raise HTTPException(400, f"世界包无法加载: {pack_id}——{entry.error}")
    return entry


def _resolve_pack(pack_id: str | None, draft: str | None = None) -> tuple[Path, str]:
    """决定这一局用哪个包 → (包目录, pack_id)。

    优先级：
    1. 显式 `draft`（§3.2 ③ 的草稿试玩）——**只能经 `catalog.resolve_draft()` 查表得到**；
    2. 显式 `pack_id`——**只能经 `catalog.resolve_pack()` 查表得到**，绝不拼路径；
       不认识的 id 直接 400，不做"猜一个相近的"这种兜底。
    3. `GAME_WORLDPACK` 环境变量（CLI `--pack`）——按**路径**加载，可指向目录外。
    4. 目录兜底：`ancient_jianghu` 优先，否则第一个可玩的包。

    第 4 条刻意不硬编码 DEFAULT_PACK：目录是内容，不该有"某个包必须在库"的假设——
    包被移走时应当退到"还有什么能玩"，而不是启动即 500。
    """
    if draft:
        entry = catalog.resolve_draft(draft, _pack_root())
        if entry is None:
            raise HTTPException(400, f"草稿不存在: {draft!r}（草稿区：{catalog.drafts_root(_pack_root())}）")
        if not entry.playable:
            raise HTTPException(400, f"草稿无法加载: {draft}——{entry.error}")
        return Path(entry.path), entry.id

    if pack_id:
        entry = _require_pack(pack_id)
        return Path(entry.path), entry.id

    env_path = os.environ.get("GAME_WORLDPACK", "").strip()
    if env_path:
        return Path(env_path), Path(env_path).name

    fallback = catalog.default_pack_id(_pack_root())
    if fallback is None:
        raise HTTPException(500, f"{_pack_root()}/ 下没有任何可玩的世界包")
    return _pack_root() / fallback, fallback


def _usage_path(sid: str) -> str:
    """G1：**按会话隔离**的成本账本路径。

    为什么用独立文件而不是"一个文件加 session 字段"：文件级隔离让"这一局花了多少钱"
    成为一次读文件即可回答的问题，也不存在并发追加的串写面。条目里同时带 `session`
    字段（见 `UsageTracker`），跨会话聚合读仍然可行。
    """
    return f"saves/usage-{sid}.jsonl"


MODE_STORY = "story"
MODE_FREE = "free"


def _validate_mode(mode: str) -> str:
    """游玩模式白名单（E-4）。未知值直接 400——不静默当 story。

    静默兜底会让前端把 "Free" 拼错时表现为"模式开关没反应"，
    而这类"点了没效果"最难排查。
    """
    if mode not in (MODE_STORY, MODE_FREE):
        raise HTTPException(400, f"未知的游玩模式: {mode!r}（可选 {MODE_STORY} / {MODE_FREE}）")
    return mode


@dataclass
class Session:
    """B-4（M4）：一个游戏会话 = game + 串行化锁 + 本会话账本。

    **刻意只存这三样**：`pack_id` 与 `mode` 都能从 `game` 推出
    （`game.pack_meta["id"]` / `game.mainline_enabled`），再存一份只会多出一个
    能进入非法组合的维度——改了 game 忘了改 Session，列表与真值就分叉。
    这与 `state.py` 把 `Appointment.is_overdue` **算出来而不落库**是同一条纪律。

    而 `usage` 恰恰相反：它必须存，且**不给默认值**——默认值意味着"漏传就静默共享
    一个 tracker"，那正是 G1 那个数据错误的形状。
    """

    game: Game
    lock: threading.Lock
    usage: UsageTracker  # G1：本会话专属账本（此前是进程级共享单例）


def session_pack_id(session: Session) -> str:
    """本局绑定的世界包 id——唯一真源是 `game.pack_meta`（与存档身份戳同源）。"""
    return session.game.pack_meta["id"]


def session_mode(session: Session) -> str:
    """本局游玩模式——由 `mainline_enabled` 推出，不另存一份。"""
    return MODE_STORY if session.game.mainline_enabled else MODE_FREE


def session_is_draft(session: Session) -> bool:
    """本局是否在试玩草稿——**从包的真实位置推出**（父目录名是 `_drafts`）。

    与 `session_pack_id` 同一纪律：不另存一份布尔量，就没有"存了但忘了同步"的可能。
    """
    return session.game.pack.root.parent.name == catalog.DRAFTS_DIRNAME


SESSIONS: dict[str, Session] = {}

# N1：后台生成任务表（进程级；见 `jobs.py` 的"为什么不能照抄 _turn_stream"）
JOBS = jobs.JobRegistry()

# 进度的终态事件：SSE 流收到它就收尾关流
_TERMINAL_STAGES = {"done", "error", "cancelled"}


# ---------------------------------------------------------------------------
# 会话与视图序列化
# ---------------------------------------------------------------------------


def _make_game(
    sid: str,
    pack_id: str | None = None,
    *,
    draft: str | None = None,
    mainline_enabled: bool = True,
) -> tuple[Game, UsageTracker]:
    """建一局。返回 (game, tracker)——tracker 由调用方存进 Session（G1）。

    选定的是哪个包不在这里回传：答案就在 `game.pack_meta["id"]`（唯一真源）。
    """
    settings = load_settings()
    if not settings.has_api_key:
        raise HTTPException(500, "未配置 DEEPSEEK_API_KEY")
    pack_path, _ = _resolve_pack(pack_id, draft)
    pack = load_worldpack(pack_path)
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
        mainline_enabled=mainline_enabled,  # E-4：自由游玩不进入主线节点
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


class NewRequest(BaseModel):
    """E-3/E-4：开局参数。两个字段都有默认值 → 请求体可省略（旧前端/旧测试不受影响）。"""

    pack_id: str | None = None  # None = 用 GAME_WORLDPACK / 目录兜底
    mode: str = MODE_STORY  # story = 沿主线推进；free = 自由游玩
    draft: str | None = None  # §3.2 ③：试玩草稿（未发布的包）


class GenerateRequest(BaseModel):
    """N1：起一个后台生成任务。

    **只收文本，不收路径**（见 `api_generate` 的说明）。`with_corpus` 默认关：
    语料要多打 12 次调用，而草稿阶段通常不需要。
    """

    name: str
    source_text: str
    offline: bool = False  # 默认真机（与 CLI 一致）；离线是给回归/演示用的
    with_corpus: bool = False
    rounds: int = 4


class PublishRequest(BaseModel):
    """§3.2 ③：把草稿发布到已发布区（过 `check_worldpack` 才允许）。"""

    name: str


class ForkRequest(BaseModel):
    """N6：把已发布包复制成草稿（"拿现成的卡来改"）。"""

    name: str


class CreatorChatRequest(BaseModel):
    """N6：对创作者 Agent 说一句话。"""

    message: str


# ---------------------------------------------------------------------------
# N6 / E-8：创作者 Agent（对话式改人物设定与世界书）
# ---------------------------------------------------------------------------

CREATORS: dict[str, creator.CreatorSession] = {}
"""进程内创作会话表（按**草稿名**索引）。

为什么按草稿名而不是随机 sid：作者的"这一版"就是草稿目录本身，会话只是它的
对话上下文。刷新页面、甚至服务重启之后，作者的"这一版"还在原处——
按名字索引让前后端都少一个需要同步的 id（与 `session_pack_id` 同一条纪律：
能从真实位置推出来的东西，不要再存一份）。
"""


def _creator_dir(name: str) -> Path:
    """草稿目录（**只能经 catalog 查表得到**，绝不拼路径）。"""
    entry = catalog.resolve_draft(name, _pack_root())
    if entry is None:
        raise HTTPException(400, f"草稿不存在: {name!r}（创作 Agent 只改草稿，不改已发布包）")
    return Path(entry.path)


def _creator_session(name: str) -> creator.CreatorSession:
    """取（或开）一个创作会话。"""
    s = CREATORS.get(name)
    if s is None:
        s = creator.CreatorSession.open(_creator_dir(name), name)
        CREATORS[name] = s
    return s


def _make_generation_starter(name: str, events: "queue.Queue"):
    """造一个"从素材起一张新卡"的注入器，交给创作者 Agent 当工具用（N6 补）。

    **它是"转交"不是"代跑"**：生成是分钟级后台任务，塞进一次工具调用里同步等会把
    一轮对话挂死几分钟、且用户看不到任何进度。所以这里只做四件事：
    校验参数 → 丢进已有的任务表 → 给前端发一个 `job` 事件（它会自动切到进度页签）→
    告诉模型**别等**。进度、取消、结果全部复用 N1 那一套。

    `draft_name` 参数（当前正在改的那张草稿）只用于生成失败时的提示文案——
    新卡与当前草稿是两张不同的卡，不该混。
    """
    del name  # 新卡名由模型给，与当前草稿无关；留着参数是为了可读的调用点

    def start(new_name: str, source_text: str, offline: bool) -> str:
        if (bad := catalog.can_create(new_name, _pack_root())) is not None:
            raise creator.CreatorError(bad)
        job = JOBS.create(
            pack_name=new_name,
            pack_dir=catalog.draft_dir(new_name, _pack_root()),
            source_text=source_text,
            offline=offline,
            with_corpus=False,
            rounds=4,
        )
        JOBS.run_in_background(job, source_text, jobs.default_runner)
        # 结构化事件（而不是让前端去解析工具返回的字符串）：前端据此**自动切到
        # 「从素材生成」页签并开始订阅这条进度流**，作者不用自己找。
        events.put({
            "type": "job", "job_id": job.id, "pack_name": new_name, "offline": offline,
        })
        return (
            f"已起任务 {job.id}（{'离线试跑，不花钱' if offline else '真机生成，约 ¥0.1–0.3'}，"
            f"需要几分钟）。**不要等它、也不要反复查**——"
            f"工作台已自动切到「从素材生成」页签显示进度；跑完新包会出现在左栏草稿里，"
            f"用户可以在那里试玩或发布。你现在可以直接把这件事告诉用户。"
        )

    return start


@app.post("/api/packs/fork")
def api_fork(req: ForkRequest) -> dict:
    """把已发布包复制成草稿——**没有这条路径，创作 Agent 就只能改刚生成的空包**。

    库里的 8 张卡都已发布，而工作版是草稿区（§4.2"原始包只读"），
    所以"改一张现成的卡"必须先 fork。**是复制不是移动**：已发布内容原地不动。
    """
    try:
        entry = catalog.fork_to_draft(req.name, _pack_root())
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "draft": entry.to_dict()}


@app.get("/api/creator/{name}")
def api_creator_state(name: str) -> dict:
    """创作会话现状：对话记录 + 工作版状态 + diff。刷新页面靠它接上。"""
    _creator_dir(name)  # 草稿不存在 → 400（而不是开一个指向空气的会话）
    s = CREATORS.get(name)
    if s is None:
        return {
            "ok": True, "name": name, "open": False, "messages": [],
            "summary": creator.WorkingCopy(root=_creator_dir(name)).summary(),
            "diff": "", "changed": [], "validate_ok": None, "validate_text": "",
        }
    ok, text = s.wc.validate()
    return {
        "ok": True, "name": name, "open": True,
        "messages": s.history(),
        "summary": s.wc.summary(),
        "diff": s.wc.diff(), "changed": s.wc.changed_files(),
        "validate_ok": ok, "validate_text": text,
        "baseline_note": s.baseline_note, "turns": s.turns,
    }


@app.delete("/api/creator/{name}")
def api_creator_reset(name: str) -> dict:
    """清掉会话上下文（**不动草稿内容**）——"换个话题重说"用。

    为什么与"删草稿"分开：作者常常想丢掉一轮越聊越乱的上下文，但保留已经改好的
    内容。基线同时重置，于是 diff 从"此刻"重新开始记（这一点在返回里说明）。
    """
    CREATORS.pop(name, None)
    return {"ok": True, "reset": name,
            "note": "已清空对话上下文；草稿内容未动。diff 基线已重置为当前状态。"}


@app.post("/api/creator/{name}/chat")
def api_creator_chat(name: str, req: CreatorChatRequest):
    """和创作者 Agent 说一句话（SSE 推过程）。

    **为什么用 SSE 而不是普通 JSON**：一轮对话是 读 → 改 → 校验 → （可能再改）
    好几次模型调用，几十秒没有反馈就是"卡了还是在想"的老问题——
    与 N1 后台任务同一个理由。事件：`tool`（哪一步、成了没有）、`text`（模型的
    中间话）、`done`（最终答复 + 校验结论 + diff）、`error`。
    """
    text = (req.message or "").strip()
    if not text:
        raise HTTPException(400, "消息为空")
    _creator_dir(name)  # 契约校验放在 HTTP 层（同 `_require_pack` 的理由）
    settings = load_settings()
    if not settings.has_api_key:
        raise HTTPException(500, "未配置 DEEPSEEK_API_KEY")
    return StreamingResponse(_creator_stream(name, text), media_type="text/event-stream")


def _creator_stream(name: str, text: str):
    """创作会话的 SSE 生成器：工作线程跑循环，本生成器消费队列。

    与 `_turn_stream` 同一形状（首帧心跳 + 哨兵收尾 + worker 里持锁）。
    锁在 worker 里取的理由与那边完全相同：**对话上下文的生存期不该取决于
    客户端是否还在线**——作者误刷新一下，不能让已经发出去的这一轮半途而废
    （改动会落盘一部分，而对话历史却少了对应的一轮，两边就对不上了）。
    """
    s = _creator_session(name)
    events: "queue.Queue" = queue.Queue()
    holder: dict = {}

    def worker() -> None:
        with _creator_lock(name):
            try:
                settings = load_settings()
                # N6 前置：**创作者调用带 pack 归因轴**（`usage-creator-<name>.jsonl`
                # + 条目里的 pack 字段），于是"改这一版花了多少钱"从第一天就答得上。
                tracker = UsageTracker(
                    f"saves/usage-creator-{name}.jsonl", session=f"creator-{name}",
                    pack=name,
                )
                llm = creator.build_creator_llm(settings, tracker)
                turn = s.send(text, llm, on_event=events.put,
                              start_generation=_make_generation_starter(name, events))
                holder["turn"] = turn
            except Exception as e:  # noqa: BLE001 — 未预期异常转 error 事件，不静默断开
                events.put({"type": "error", "message": f"内部错误: {type(e).__name__}: {e}"})
            finally:
                events.put(None)

    threading.Thread(target=worker, daemon=True).start()
    yield _sse("start", json.dumps({"name": name}, ensure_ascii=False))
    while True:
        ev = events.get()
        if ev is None:
            break
        yield _sse(ev.get("type", "message"), json.dumps(ev, ensure_ascii=False))
    turn = holder.get("turn")
    if turn is not None:
        yield _sse("done", json.dumps({
            "reply": turn.reply,
            "tools": [{"name": t["name"], "status": t["status"]} for t in turn.tools_used],
            "validate_ok": turn.validate_ok,
            "validate_text": turn.validate_text,
            "changed": turn.changed,
            "diff": s.wc.diff(),
            "truncated": turn.truncated,
        }, ensure_ascii=False))


_CREATOR_LOCKS: dict[str, threading.Lock] = {}
_CREATOR_LOCKS_GUARD = threading.Lock()


def _creator_lock(name: str) -> threading.Lock:
    """每个草稿一把锁（同草稿的对话串行，不同草稿互不阻塞）。"""
    with _CREATOR_LOCKS_GUARD:
        lock = _CREATOR_LOCKS.get(name)
        if lock is None:
            lock = _CREATOR_LOCKS[name] = threading.Lock()
        return lock


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


@app.get("/api/catalog")
def api_catalog() -> dict:
    """E-3：可选的"卡"列表（剧本市场的**数据面**）。

    坏包也返回（`playable=false` + `error` 原文）——让玩家看见"这张卡坏了"，
    比整块界面白屏或静默少一张卡都好排查。
    """
    entries = catalog.list_packs(_pack_root())
    return {
        "ok": True,
        "root": str(_pack_root()),
        "default": catalog.default_pack_id(_pack_root()),
        "packs": [e.to_dict() for e in entries],
    }


@app.get("/api/sessions")
def api_sessions() -> dict:
    """E-9：进程内活跃会话列表（前端左栏）。

    只报会话级事实（哪张卡、什么模式、跑到第几回合），不含剧情内容——
    列表接口不该把长局历史拖着走。
    """
    return {
        "ok": True,
        "sessions": [
            {
                "sid": sid,
                "pack_id": session_pack_id(s),
                "name": s.game.pack.world.name,
                "mode": session_mode(s),
                "turn": s.game.state.turn_count,
                "day": s.game.state.day,
                "ending": bool(s.game.ending),
            }
            for sid, s in SESSIONS.items()
        ],
    }


@app.get("/api/saves")
def api_saves() -> dict:
    """E-9：存档列表（只读顶层摘要，反序列化留给真正读档时）。

    按 mtime 倒序 —— 玩家找的是"最近那局"，不是按文件名字典序。
    """
    if not SAVE_ROOT.is_dir():
        return {"ok": True, "saves": []}
    out: list[dict] = []
    for p in SAVE_ROOT.glob("*.json"):
        try:
            out.append(save_summary(p))
        except (ValueError, OSError) as e:
            # 单个坏档不拖垮列表（与 catalog 同一姿态）
            out.append({"path": p.name, "error": f"{type(e).__name__}: {e}"})
    out.sort(key=lambda r: r.get("mtime", 0), reverse=True)
    return {"ok": True, "saves": out}


# ---------------------------------------------------------------------------
# N1：后台生成任务（素材 → 世界包）
# ---------------------------------------------------------------------------


@app.post("/api/packs/generate", status_code=202)
def api_generate(req: GenerateRequest) -> dict:
    """起一个后台生成任务，立刻返回 `job_id`（生成要几分钟，不能塞在请求里）。

    **只收 `source_text`，不收文件路径**。这不是简化，是安全边界：本端点若接受
    `sources: ["../../.env"]`，就等于给任何能访问本服务的人一个**任意文件读取**洞
    （`read_sources` 会老老实实读它并把内容喂给模型）。CLI 收路径没问题——那是本机
    用户手敲的；HTTP 接口不行。将来要传文件，走带沙箱的工作区文件服务。

    **成本提醒**：`offline=false`（默认，与 CLI 一致）会真的调用模型，
    一次生成约 ¥0.1–0.3（`with_corpus=true` 更多，因为它多打 12 次调用）。
    调用方（创作工作台）负责在点之前把这件事说清楚。

    **拒绝遮蔽已发布的包**（`catalog.can_create`）：`materialize` 会先清空 `npcs/`，
    所以同名写入已发布区等于静默毁掉一个线上包。同名**草稿**则允许反复覆盖——
    那正是草稿区的用途（§3.2 ③ 落地后，"改一版再生成"不再需要不停换名字）。
    """
    if (bad := catalog.can_create(req.name, _pack_root())) is not None:
        raise HTTPException(400, bad)
    if not req.source_text.strip():
        raise HTTPException(400, "素材为空：请提供 source_text（小说/大纲/设定的正文）")
    if req.rounds < 1:
        raise HTTPException(400, "rounds 至少为 1（没有任何修复轮次的生成不符合质量门口径）")

    # §3.2 ③：**生成写进草稿区，不写已发布区**。这样反复生成同一个名字是安全的
    # （草稿本来就是给反复改用的），而"发布"才是那个需要过闸门的显式动作。
    job = JOBS.create(
        pack_name=req.name,
        pack_dir=catalog.draft_dir(req.name, _pack_root()),
        source_text=req.source_text,
        offline=req.offline,
        with_corpus=req.with_corpus,
        rounds=req.rounds,
    )
    JOBS.run_in_background(job, req.source_text, jobs.default_runner)
    return {"ok": True, **job.snapshot()}


@app.get("/api/packs/drafts")
def api_drafts() -> dict:
    """草稿列表（含未通过校验的，附 `error` 原文）。"""
    entries = catalog.list_drafts(_pack_root())
    return {"ok": True, "drafts": [e.to_dict() for e in entries]}


@app.post("/api/packs/publish")
def api_publish(req: PublishRequest) -> dict:
    """把草稿发布到已发布区。**闸门 = `check_worldpack` 必须过**（§3.2 ③）。

    这是"唯一写口"的最后一环：Web 界面从不直写文件系统——它生成到草稿区、
    校验、然后发布。发布不过关时把**报错原文**回给调用方（工作台会拿它去喂模型修）。
    """
    try:
        entry = catalog.publish(req.name, _pack_root())
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "pack": entry.to_dict()}


@app.delete("/api/packs/drafts/{name}")
def api_delete_draft(name: str) -> dict:
    """删除草稿（清理用）。**只删草稿区**——不提供"删除已发布包"的接口：
    那是文件系统层面的事，不该由 HTTP 顺手做掉。"""
    try:
        catalog.delete_draft(name, _pack_root())
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "deleted": name}


@app.get("/api/packs/generate")
def api_generate_list() -> dict:
    """任务列表（新的在前）——刷新页面后靠它找回正在跑的任务。"""
    return {"ok": True, "jobs": [j.snapshot() for j in JOBS.list()]}


@app.get("/api/packs/generate/{job_id}")
def api_generate_get(job_id: str) -> dict:
    return {"ok": True, **_get_job(job_id).snapshot()}


@app.post("/api/packs/generate/{job_id}/cancel")
def api_generate_cancel(job_id: str) -> dict:
    """请求取消。**取消点是块与块之间**——一次 LLM 调用中途没法安全打断，
    也不该打断（半截响应没有意义，而钱已经花了）。"""
    job = _get_job(job_id)
    accepted = job.request_cancel()
    return {"ok": True, "cancelled": accepted, **job.snapshot()}


def _get_job(job_id: str) -> GenerationJob:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"任务不存在: {job_id}")
    return job


@app.get("/api/packs/generate/{job_id}/events")
def api_generate_events(job_id: str) -> StreamingResponse:
    """SSE 进度流：**先回放已发生的事件，再续播**。

    回放是必需的（不是锦上添花）：生成是分钟级任务，刷新页面是常态。只做"从此刻起
    的增量"，刷新一次就丢掉全部进度，而任务还在烧钱。

    断线**不取消任务**（与 `_turn_stream` 的"锁的生存期与客户端是否在线无关"同一条
    纪律）——分钟级任务被一次误刷新杀掉是不可接受的。
    """
    job = _get_job(job_id)
    return StreamingResponse(_job_stream(job), media_type="text/event-stream")


def _job_stream(job: GenerationJob):
    q, snapshot = job.subscribe()
    try:
        yield _sse("start", json.dumps(job.snapshot(), ensure_ascii=False))
        for ev in snapshot:  # 回放
            yield _sse("progress", json.dumps(ev, ensure_ascii=False))
            if ev.get("stage") in _TERMINAL_STAGES:
                return
        # 回放完毕、任务已终态却没有终态事件（理论上不该发生）：收流而不是挂死
        if job.status in jobs.TERMINAL:
            yield _sse("end", json.dumps(job.snapshot(), ensure_ascii=False))
            return
        while True:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                # 心跳：一次 LLM 调用可能几十秒没有事件，没有心跳的话浏览器/中间层
                # 会判定连接已死。SSE 注释帧是标准做法，客户端会忽略它。
                yield ": keepalive\n\n"
                continue
            yield _sse("progress", json.dumps(ev, ensure_ascii=False))
            if ev.get("stage") in _TERMINAL_STAGES:
                return
    finally:
        job.unsubscribe(q)


@app.post("/api/new")
def api_new(req: NewRequest | None = None) -> dict:
    """开局。`pack_id` 选已发布的卡，`draft` 试玩草稿，`mode` 选"跟主线走 / 自由探索"。"""
    req = req or NewRequest()
    mode = _validate_mode(req.mode)
    if req.pack_id:  # 接口层先校验契约（见 `_require_pack` 的说明）
        _require_pack(req.pack_id)
    if req.draft and catalog.resolve_draft(req.draft, _pack_root()) is None:
        raise HTTPException(400, f"草稿不存在: {req.draft!r}")
    sid = uuid.uuid4().hex[:12]
    # B-4：autosave 按 sid 命名，需先生成 sid
    game, tracker = _make_game(
        sid, req.pack_id, draft=req.draft, mainline_enabled=(mode == MODE_STORY)
    )
    session = Session(game=game, lock=threading.Lock(), usage=tracker)
    with session.lock:
        view = game.start()
        SESSIONS[sid] = session
    # name：世界包名随会话返回，前端据此渲染标题（F1 修复：引擎页面不含世界内容文案）
    return {
        "sid": sid,
        "name": game.pack.world.name,
        "pack_id": session_pack_id(session),  # E-3：前端据此显示"在玩哪张卡"
        "mode": mode,  # E-4：前端据此显示模式徽标
        "draft": session_is_draft(session),  # §3.2 ③：草稿试玩 → 前端打"未发布"水印
        "view": _view(game, view),
    }


@app.get("/api/config")
def api_config() -> dict:
    """前端启动配置。

    **为什么要有这个端点**（Stage B 的一个简化）：Stage A 靠服务端把
    `storyline.FREE_INPUT_OPTION` 替换进 `index.html` 的占位符；换成 Vite 构建后
    `dist/index.html` 是**构建产物**，服务端再去改它既别扭（改了就不等于构建输出、
    "dist 是否过期"的判定也失去意义）又易错。改为构建产物**只读**、配置**运行时拉取**：
    单一真源仍在引擎常量，但注入点从"服务端改写 HTML"移到"一个 JSON 字段"。
    """
    return {
        "ok": True,
        "free_input": FREE_INPUT_OPTION,
        "default_mode": MODE_STORY,
        "app": "game-agent",
    }


@app.get("/api/{sid}/meta")
def api_meta(sid: str) -> dict:
    """本会话的绑定信息（前端刷新后重新对齐标题/卡/模式）。"""
    s = _ensure_session(sid)
    return {
        "ok": True,
        "sid": sid,
        "pack_id": session_pack_id(s),
        "name": s.game.pack.world.name,
        "mode": session_mode(s),
        "draft": session_is_draft(s),
        "mainline": s.game.state.current_node,
        "turn": s.game.state.turn_count,
        "day": s.game.state.day,
    }


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
            # G2 补漏（内容级）：**旧档没有身份戳**，上面那道比对会放行（只补不漏）。
            # 但"把 A 卡的档读进 B 卡"仍然会让 state 里出现当前包不认识的键，
            # 之后 context.status_text() 直接 KeyError → 500。
            # 这里用内容兜底，把它变成一句能读懂、能照做的拒绝。
            mismatch = state_pack_mismatch(state, session.game.pack)
            if mismatch:
                raise PackMismatchError(mismatch)
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
# 前端（Stage B：Vite + Vue 3 构建产物，FastAPI 只负责托管）
# ---------------------------------------------------------------------------
#
# 演进：内嵌字符串（Stage A 之前）→ 拆分出的真实文件（Stage A）→ Vite 构建产物（Stage B）。
# web.py 的职责一路收窄到"托管 + 给一个配置端点"，这是对的：
# 前端怎么组织、怎么构建，不该由服务端知道。


def dist_dir() -> Path:
    return WEBUI_DIR / "dist"


def index_html() -> str:
    """读构建产物的入口页。

    **不再做占位符替换**（Stage A 的遗留）：`dist/index.html` 是构建输出，
    服务端去改写它会让"dist 是否与源码一致"的判定失去意义。自由输入文案改由
    `GET /api/config` 运行时下发（见 `api_config`），单一真源不变。

    构建产物缺失时给出**可行动的**错误，而不是让 StaticFiles 抛出难懂的异常。
    """
    entry = dist_dir() / "index.html"
    if not entry.is_file():
        raise HTTPException(
            500,
            "前端尚未构建：缺少 game_agent/webui/dist/index.html。"
            "请在 game_agent/webui/ 下运行 `npm install && npm run build`。"
            "（开发模式可直接用 `npm run dev`，Vite 会把 /api 代理到本服务。）",
        )
    return entry.read_text(encoding="utf-8")


# 挂载构建产物。`html=True` 让 /static/ 下的目录请求也能落到 index.html。
# 构建产物不存在时不挂载——否则 StaticFiles 会在导入期直接抛错，
# 连"前端未构建"这个可执行的提示都来不及给。
if dist_dir().is_dir():
    app.mount("/static", StaticFiles(directory=str(dist_dir())), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return index_html()

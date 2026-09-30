"""SSE 会话锁生存期守卫：锁必须覆盖**整个回合**，而不是只覆盖客户端连接期间。

**缺陷背景**：`with session.lock:` 此前包着**生成器循环**。客户端断线时生成器被
`GeneratorExit` 关闭，锁在 `yield` 处释放——而 `dispatch` 工作线程还在改
`session.game`。于是：

- B-4「同 sid 回合串行化」静默失效：下一个 `/turn`、`/save`、`/load` 可与被放弃的
  回合**并发**跑在同一个 Game 上；
- `dispatch` 每次访问都重新读 `game.state`，所以被放弃的回合的效果会落到**之后**
  绑定的状态上（例如刚 `/load` 进来的存档被上一轮的残留效果改掉）。

契约：**锁的生存期 = 本地真值可能被修改的时长**，与客户端是否在线无关。

测试纪律（本文件第一版踩过的坑）：
- 不在生成器上做**无超时**的 `next()`——旧实现会挂住整个测试会话；
- 不靠 sleep 猜时序定论——用**事件**在确定的时刻探测锁，
  "回合进行中"由 `in_turn` 标志显式标记，sleep 只用来制造重叠窗口。
"""

from __future__ import annotations

import threading
import time

import game_agent.web as web


class _FakeView:
    narration = "叙事。"
    choices = ["一", "二", "三"]
    ending = None
    choice_prompt = None
    briefing = None


class _SlowGame:
    """say() 期间标记「回合进行中」并探测锁的持有情况。

    - `in_turn`：进入 say() 时置位、退出时清除——判定"回合是否正在进行"的唯一依据；
    - `release`：测试用它**显式**结束回合（而不是 sleep 猜时序）——
      `next(gen)` 会一直阻塞到出现第一个事件或哨兵，所以"回合中途"这个状态
      只能由测试自己制造：先放行 worker 进入 say()，再在它等 release 时探测锁；
    - `lock_free_during_turn`：若**回合进行中**锁却空闲 → 串行化失效（待修状态）。
    """

    def __init__(self, session_box: dict):
        self.session_box = session_box
        self.in_turn = threading.Event()
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []
        self.lock_free_during_turn: bool | None = None
        self._probe_done = threading.Event()

    def say(self, text: str):
        self.calls.append(text)
        self.in_turn.set()
        self.started.set()
        try:
            self.probe_lock()  # 在回合**进行中**探测锁
            self.release.wait(timeout=5.0)  # 由测试显式放行，绝不 sleep 猜
            return _FakeView()
        finally:
            self.in_turn.clear()

    def probe_lock(self) -> None:
        if self.lock_free_during_turn is not None:
            return
        session = self.session_box.get("s")
        if session is None:
            return
        got = session.lock.acquire(blocking=False)
        self.lock_free_during_turn = got
        if got:
            session.lock.release()
        self._probe_done.set()

    @property
    def probed(self) -> bool:
        return self._probe_done.is_set()

    def status_text(self):
        return "状态"


class _FakeSession:
    def __init__(self, game):
        self.game = game
        self.lock = threading.Lock()


def _install(monkeypatch, box: dict):
    session = _FakeSession(_SlowGame(box))
    box["s"] = session
    monkeypatch.setitem(web.SESSIONS, "sid001", session)
    return session


def _wait_until(pred, timeout: float = 2.0, tick: float = 0.01) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(tick)
    return bool(pred())


def _pump(gen, timeout: float = 2.0) -> threading.Thread:
    """后台消费生成器（有限等待），避免无超时 next() 挂住会话。"""
    def run():
        try:
            for _ in gen:
                pass
        except Exception:  # noqa: BLE001 — 关闭/断线都算正常收场
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return t


# ---------------------------------------------------------------------------
# 核心：回合进行中锁必须被持有（断线也不例外）
# ---------------------------------------------------------------------------


def test_lock_held_while_turn_in_progress(monkeypatch):
    """T4 复现钉：回合进行中锁空闲 = 断线会提前释放 = 串行化失效。"""
    box: dict = {}
    session = _install(monkeypatch, box)
    gen = web._turn_stream(session, web.TurnRequest(kind="say", text="你好"))
    thread = _pump(gen, timeout=0.2)  # 后台消费；worker 会卡在 release.wait 上

    assert session.game.started.wait(timeout=2), "工作线程未启动"
    assert _wait_until(lambda: session.game.probed), "锁探测未完成"
    assert session.game.lock_free_during_turn is False, (
        "回合进行中锁是空闲的——客户端断线会释放锁，串行化保证失效"
    )

    session.game.release.set()  # 放行回合
    thread.join(timeout=3.0)


def test_lock_survives_generator_teardown(monkeypatch):
    """生成器被销毁（≈客户端断线）后，锁**不**随之释放——直到回合真正结束。

    实现说明：生成器正在别的线程里执行时不能并发 `close()`（ValueError:
    generator already executing），所以这里用**立刻退出的消费者**制造"没人再读流"
    的局面（等价于客户端走人），再从测试线程观察锁。
    """
    box: dict = {}
    session = _install(monkeypatch, box)
    gen = web._turn_stream(session, web.TurnRequest(kind="say", text="你好"))
    pump = _pump(gen, timeout=0.05)  # 启动 worker 后立刻返回（消费者"走了"）

    assert session.game.started.wait(timeout=2), "工作线程未启动（worker 未进入 say）"
    assert session.game.in_turn.is_set(), "回合不在进行中，本用例前提不成立"

    # 消费者已经离场，但回合仍在跑 → 锁必须被持有
    got = session.lock.acquire(blocking=False)
    if got:
        session.lock.release()
    assert got is False, (
        "消费者离场后锁被释放（工作线程仍在改状态）——串行化保证失效"
    )

    session.game.release.set()  # 放行回合
    assert _wait_until(lambda: not session.game.in_turn.is_set(), timeout=3.0)
    assert _wait_until(lambda: session.lock.acquire(blocking=False), timeout=2.0), (
        "回合结束后锁未释放——后续请求会永久阻塞"
    )


def test_disconnect_cleanup_detaches_sink_and_drains_queue(monkeypatch):
    """T5：消费者离场后，本流的 on_text 必须解绑、队列必须排空。

    为什么值得管：`deltas` 队列与 `game.on_text` 是按 sid 绑到 game 实例上的。
    客户端断线时 worker 仍会跑完（B-4/T4 的刻意设计），于是"没人消费的队列 +
    仍在推送的回调"会一直挂着——长回合 + 长局下是实打实的积压。
    """
    box: dict = {}
    session = _install(monkeypatch, box)
    gen = web._turn_stream(session, web.TurnRequest(kind="say", text="你好"))
    pump = _pump(gen, timeout=0.2)

    assert session.game.started.wait(timeout=2)
    session.game.release.set()  # 放行，让 worker 跑完（避免 close 正在执行的生成器）
    assert _wait_until(lambda: not session.game.in_turn.is_set(), timeout=3.0)

    pump.join(timeout=2.0)
    gen.close()  # ≈ 客户端断线：触发 finally 里的清理

    assert session.game.on_text is None, "断线后 on_text 未解绑（会一直被旧闭包持有）"


def test_new_stream_keeps_its_own_sink():
    """清理**不得**误伤新流：队列不是本流的，就不能解绑 on_text。

    判据是回调上的 `_stream_queue` 标记——若改用内省 `__closure__` 单元的方式，
    这里就会静默失效（把别人流的回调一并清掉，玩家看不到后续增量）。
    """
    import queue as _queue

    other_queue = _queue.Queue()
    mine = _queue.Queue()

    class _G:
        def __init__(self):
            self.on_text = web._make_stream_sink(other_queue)

    session = _FakeSession(_G())
    web._release_stream(session, mine)  # 我的队列 ≠ 当前绑定的队列
    assert session.game.on_text is not None, "清理动了别的流的回调"

    web._release_stream(session, other_queue)  # 正是本流 → 应当解绑
    assert session.game.on_text is None


def test_release_stream_drains_abandoned_queue():
    """清理排空被遗弃的增量：断线后没人消费，worker 仍在推（长局会积压）。"""
    import queue as _queue

    class _G:
        def __init__(self):
            self.on_text = None

    q = _queue.Queue()
    for i in range(5):
        q.put(("delta", f"第{i}片"))
    web._release_stream(_FakeSession(_G()), q)
    assert q.empty(), "被遗弃的队列未排空"


def test_release_stream_is_idempotent_and_never_raises():
    """worker 可能仍在跑：清理必须幂等、绝不抛异常（否则会打断 finally 链）。"""
    import queue as _queue

    class _G:
        def __init__(self):
            self.on_text = None

    session = _FakeSession(_G())
    q = _queue.Queue()
    for _ in range(3):
        web._release_stream(session, q)  # 重复调用不抛
    assert session.game.on_text is None


# ---------------------------------------------------------------------------
# 回归保护：正常路径不得因改动而阻塞
# ---------------------------------------------------------------------------


def test_lock_released_after_normal_completion(monkeypatch):
    box: dict = {}
    session = _install(monkeypatch, box)
    gen = web._turn_stream(session, web.TurnRequest(kind="say", text="你好"))
    session.game.release.set()
    _pump(gen, timeout=2.0)

    assert _wait_until(lambda: session.lock.acquire(blocking=False), timeout=2.0), (
        "回合结束后锁未释放"
    )
    session.lock.release()


def test_worker_records_call_under_lock(monkeypatch):
    """正常回合：工作线程在锁内完成调用（回归：别把 dispatch 锁到死）。"""
    box: dict = {}
    session = _install(monkeypatch, box)
    gen = web._turn_stream(session, web.TurnRequest(kind="say", text="第一句"))
    session.game.release.set()
    _pump(gen, timeout=2.0)

    assert session.game.calls == ["第一句"]


# ---------------------------------------------------------------------------
# 首帧心跳：纯生成式回合里客户端不该等到整轮结束才有响应
# ---------------------------------------------------------------------------


def test_first_frame_arrives_before_turn_completes(monkeypatch):
    """复现钉：第一个 SSE 帧必须在**回合仍在进行时**到达。

    此前第一个 `yield` 要等 delta 或哨兵；纯生成式回合（模型先长时间思考再调工具）
    没有 delta，于是客户端等到整轮结束才见到第一个字节——"流式"体感退化为转圈。

    注意：生成器体要消费才执行，而 `next(gen)` 现在**立即**返回心跳帧
    （旧实现下它会阻塞到回合结束，本用例因此会超时/拿到哨兵）。
    """
    box: dict = {}
    session = _install(monkeypatch, box)
    gen = web._turn_stream(session, web.TurnRequest(kind="say", text="你好"))

    first = next(gen, None)  # 旧实现：阻塞到回合结束；新实现：立刻返回心跳

    assert first is not None, "生成器没有立即给出首帧"
    assert "event: start" in first, f"首帧不是心跳帧: {first!r}"
    assert "event: delta" not in first and "event: done" not in first, (
        f"心跳帧混用了内容/结束事件类型: {first!r}"
    )
    # 首帧到达时回合仍在进行（被 release 卡住）→ 确实做到了"提前反馈"
    assert session.game.started.wait(timeout=2), "工作线程未启动"
    assert session.game.in_turn.is_set(), (
        "首帧到达时回合已经结束了——等于没有提前反馈"
    )

    session.game.release.set()
    gen.close()


def test_first_frame_is_heartbeat_not_content(monkeypatch):
    """心跳帧不得伪装成 delta——前端靠事件类型分流，混用会让空叙事污染故事框。"""
    box: dict = {}
    session = _install(monkeypatch, box)
    gen = web._turn_stream(session, web.TurnRequest(kind="say", text="你好"))
    first = next(gen, None)
    session.game.release.set()

    assert "event: start" in first
    assert "event: delta" not in first
    assert "event: done" not in first

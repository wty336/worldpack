"""后台生成任务（roadmap N1 / E-6 / 批次 B2）。

上游：`docs/plan-creator-player.md` §3.3 批次 B 的 B2；`docs/roadmap.md` N1。

**为什么不能照抄 `_turn_stream` 就完事**：那个流是"一个请求 = 一个回合"，秒级、
单一消费者、断了就断了。生成是**分钟级**任务，形状完全不同：

| | 回合流（`_turn_stream`） | 生成任务（本模块） |
| --- | --- | --- |
| 时长 | 秒级 | **分钟级** |
| 消费者 | 一个（发起请求的那个） | 可能多个；**刷新页面后还要能接上** |
| 断线 | 回合照跑，但没人再看 | **必须能重连并看到已发生的全部进度** |
| 结果 | SSE 的 `done` 帧带完整视图 | 是个目录 + 一份成本报告，要能**事后**取 |

所以这里不是"包一层队列"，而是三件具体的事：

1. **事件留档 + 回放**：`events` 是任务的持久记录；订阅时先回放再续播。
   这是"刷新后还能接上"的唯一实现方式——分钟级任务里刷新页面是常态，不是边缘情况。
2. **多订阅者广播**：每个观察者一个队列。慢消费者**只丢自己**（有界队列），
   绝不阻塞生产者，也绝不影响 `events`（那是回放的事实来源）。
3. **取消**：生成要花钱，必须有停止入口。取消检查点在**块与块之间**
   （`worldgen.WorldgenCancelled`），因为一次 LLM 调用中途没法安全打断——
   半截响应没有意义，而钱已经花了。

**断线与取消是两件事**：客户端断开**不取消**任务（与 `_turn_stream` 的
"锁的生存期与客户端是否在线无关"同一条纪律）；只有显式取消才停。

**并发上限 = 1**：单用户本机应用，同时跑两个生成只会让两边都慢、费用翻倍且难以归因。
排队是显式的（`status="queued"`），不是静默等待。
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import worldgen
from .worldgen import GenerateOptions, WorldgenCancelled, WorldgenError

# 单个观察者的队列上限。满了就丢**这个观察者**的增量（并给它一个提示事件），
# 因为 `events` 才是事实来源——慢客户端的正确补救是重连后回放，而不是拖慢生产者。
_SUBSCRIBER_QUEUE_MAX = 512

MAX_CONCURRENT_JOBS = 1

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = (DONE, FAILED, CANCELLED)


@dataclass
class GenerationJob:
    """一次生成任务。

    `events` 是**唯一事实来源**（回放靠它）；`subscribers` 只是唤醒机制。
    这个区分是刻意的：把"谁能看到"和"发生过什么"混在一起，就会出现
    "唯一的客户端断了 → 进度也没了"。
    """

    id: str
    pack_name: str
    pack_dir: Path
    offline: bool
    with_corpus: bool
    rounds: int
    source_chars: int

    status: str = QUEUED
    events: list[dict] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    cost: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False

    _subscribers: list[queue.Queue] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- 事件 -------------------------------------------------------------

    def publish(self, event: dict) -> None:
        """记档 + 广播。**记档永不失败**；广播只影响当前在看的客户端。

        每条事件带 `ts`（自任务创建起的秒数）：前端要拿它显示"这一步花了多久"，
        而且它让"进度是逐步产生的"成为**可断言的服务端事实**——不依赖客户端
        观察到的到达时间（那会受缓冲、代理、网络影响）。
        """
        stamped = {**event, "ts": round(time.time() - self.created_at, 3)}
        with self._lock:
            self.events.append(stamped)
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(stamped)
            except queue.Full:
                # 慢客户端：丢它自己的增量，并在它的队列里留个提示。
                # 不动 `events`——它重连时会回放到完整进度。
                try:
                    q.get_nowait()
                    q.put_nowait({"stage": "warn", "message": "（进度过快，部分增量已省略）"})
                except queue.Empty:  # pragma: no cover - 竞态兜底
                    pass

    def subscribe(self) -> tuple[queue.Queue, list[dict]]:
        """订阅：返回 (队列, 已发生事件的快照)。

        调用方**必须先发快照再读队列**，否则会漏掉两者之间的窗口。
        顺序是"先入列、后取快照"：宁可重复一条事件（前端按 stage 幂等），不可漏。
        """
        q: queue.Queue = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        with self._lock:
            self._subscribers.append(q)
            snapshot = list(self.events)
        return q, snapshot

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # -- 取消 -------------------------------------------------------------

    def request_cancel(self) -> bool:
        """请求取消。返回"是否真的会被取消"（已终态的任务返回 False）。"""
        with self._lock:
            if self.status in TERMINAL:
                return False
            self.cancel_requested = True
            return True

    def _cancelled(self) -> bool:
        with self._lock:
            return self.cancel_requested

    # -- 视图 -------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return round(end - (self.started_at or self.created_at), 1)

    def snapshot(self) -> dict:
        """任务摘要（列表与首帧用）。**不含全部事件**——那是 SSE 的事。"""
        with self._lock:
            last = self.events[-1]["message"] if self.events else ""
            n_events = len(self.events)
        return {
            "job_id": self.id,
            "pack_name": self.pack_name,
            "status": self.status,
            "offline": self.offline,
            "with_corpus": self.with_corpus,
            "source_chars": self.source_chars,
            "events": n_events,
            "last_message": last,
            "result": self.result,
            "error": self.error,
            "cost": self.cost,
            "elapsed": self.elapsed,
        }


class JobRegistry:
    """任务表 + 单槽执行器。

    **串行执行**（`MAX_CONCURRENT_JOBS = 1`）：同时跑多个生成会让两边都慢、
    费用翻倍且难以归因。排队是显式的（`status="queued"`），不是静默等待。
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_JOBS):
        self._jobs: dict[str, GenerationJob] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(max_concurrent)

    def get(self, job_id: str) -> GenerationJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[GenerationJob]:
        with self._lock:
            return [self._jobs[j] for j in reversed(self._order)]  # 新的在前

    def create(
        self,
        *,
        pack_name: str,
        pack_dir: Path,
        source_text: str,
        offline: bool,
        with_corpus: bool,
        rounds: int,
    ) -> GenerationJob:
        job = GenerationJob(
            id=uuid.uuid4().hex[:12],
            pack_name=pack_name,
            pack_dir=pack_dir,
            offline=offline,
            with_corpus=with_corpus,
            rounds=rounds,
            source_chars=len(source_text),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        return job

    def run_in_background(self, job: GenerationJob, source_text: str,
                          runner: Callable[[GenerationJob, str], None]) -> None:
        """起一个守护线程执行 `runner(job, source_text)`。

        守护线程：进程退出时不该被一个分钟级任务挂住（与 `_turn_stream` 的 worker 同理）。
        """
        threading.Thread(
            target=self._execute, args=(job, source_text, runner),
            daemon=True, name=f"worldgen-{job.id}",
        ).start()

    def _execute(self, job: GenerationJob, source_text: str,
                 runner: Callable[[GenerationJob, str], None]) -> None:
        self._slots.acquire()
        try:
            if job._cancelled():
                # 排队期间就被取消：不发车，直接落终态（否则会白烧一次生成）
                job.status = CANCELLED
                job.finished_at = time.time()
                job.publish({"stage": "cancelled", "message": "已取消（排队中）"})
                return
            job.status = RUNNING
            job.started_at = time.time()
            runner(job, source_text)
        except WorldgenCancelled:
            job.status = CANCELLED
            job.error = None
            job.finished_at = time.time()
            job.publish({"stage": "cancelled", "message": "已取消"})
        except Exception as e:  # noqa: BLE001 — 任何异常都必须落成 job 状态，不能杀线程
            job.status = FAILED
            job.error = f"{type(e).__name__}: {e}" if not isinstance(e, WorldgenError) else str(e)
            job.finished_at = time.time()
            job.publish({"stage": "error", "message": job.error})
        finally:
            self._slots.release()


def default_runner(job: GenerationJob, source_text: str) -> None:
    """默认执行：跑 `worldgen.generate`，把进度事件转进任务。

    成本从**本次任务专属**的账本增量算（`UsageTracker.entries` 是进程内的，
    新建的那个 tracker 只装本次调用），因此"这个包花了多少钱"是准确归因。
    """
    llm, tracker = worldgen.build_llm(job.offline)
    try:
        result = worldgen.generate(
            job.pack_dir, source_text, llm,
            options=GenerateOptions(
                rounds=job.rounds, with_corpus=job.with_corpus, draft_only=False,
            ),
            offline=job.offline,
            on_progress=job.publish,
            should_cancel=job._cancelled,
        )
    finally:
        if tracker is not None:
            job.cost = tracker.cost_report() if tracker.entries else None
    job.result = {
        "pack_dir": str(result.pack_dir),
        "pack_name": job.pack_name,
        "repairs": result.repairs,
        "corpus_written": result.corpus_written,
        "stages": result.stages,
        "summary": result.summary,
    }
    job.status = DONE
    job.finished_at = time.time()
    job.publish({"stage": "done", "message": f"[✓] 世界包已生成 → {result.pack_dir}"})

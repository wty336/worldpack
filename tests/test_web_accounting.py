"""G1 守卫：usage 记账按会话隔离（`docs/plan-tavern-shaped-product.md` §6 E-1）。

**被钉住的缺陷**：`web.py` 的 `_shared_tracker()` 是**全进程单例**，
所有会话、所有剧本、所有玩家共写同一个 `saves/usage-web.jsonl`。
单包单会话时代这只是"合并口径"；多会话/多剧本下变成**数据错误**——
账本无法回答"这一局花了多少钱""谁在烧钱"，而这正是玩家模式的第一性数据。

本文件守四件事：
1. **路径按会话**：每个 sid 一个账本文件；
2. **条目带会话轴**：`session` 与提供方 token 口径分开，两种读法（按会话精确 / 跨会话聚合）都成立；
3. **会话间不串号**：A 局的开销不出现在 B 局的 `/cost` 里；
4. **回归守卫**：进程级共享单例不得被重新引入。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

import game_agent.web as web
from game_agent.usage import UsageTracker


# ---------------------------------------------------------------------------
# 1. 路径与条目
# ---------------------------------------------------------------------------


def test_usage_path_is_per_session():
    assert web._usage_path("aaaa") != web._usage_path("bbbb")
    assert "aaaa" in web._usage_path("aaaa")


def test_record_carries_session_axis(tmp_path):
    """`session` 是引擎的**会话轴**，与提供方的 token 口径分开落字段。"""
    p = tmp_path / "u.jsonl"
    t = UsageTracker(p, session="sid-1")
    t.record("deepseek-v4-flash", "turn", {"prompt_tokens": 10, "completion_tokens": 2})
    line = json.loads(p.read_text(encoding="utf-8").strip())
    assert line["session"] == "sid-1"
    assert line["purpose"] == "turn"
    assert line["prompt_tokens"] == 10


def test_record_without_session_omits_axis(tmp_path):
    """不传 session（CLI/脚本）→ 不写该字段，旧口径保持不变。"""
    p = tmp_path / "u.jsonl"
    UsageTracker(p).record("m", "turn", {"prompt_tokens": 1})
    assert "session" not in json.loads(p.read_text(encoding="utf-8").strip())


def test_two_trackers_write_to_separate_files(tmp_path):
    """文件级隔离：两个会话各写各的，互不污染。"""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    UsageTracker(a, session="a").record("m", "turn", {"prompt_tokens": 100})
    UsageTracker(b, session="b").record("m", "turn", {"prompt_tokens": 7})
    assert json.loads(a.read_text(encoding="utf-8").strip())["prompt_tokens"] == 100
    assert json.loads(b.read_text(encoding="utf-8").strip())["prompt_tokens"] == 7


# ---------------------------------------------------------------------------
# 2. 回归守卫：不得退回进程级单例
# ---------------------------------------------------------------------------


def test_no_process_global_tracker():
    """G1 的成因就是进程级共享账本；这两个名字不得复活。

    钉名字而不是钉行为：`_shared_tracker` 一旦回归，症状是"多会话静默共写"，
    不会报错、也不会让任何既有断言变红——所以只能直接钉住它的存在。
    """
    assert not hasattr(web, "_shared_tracker"), "进程级共享 tracker 不得回归（G1）"
    assert not hasattr(web, "_WEB_TRACKER"), "进程级 tracker 单例不得回归（G1）"


def test_session_dataclass_requires_tracker():
    """Session 必须持有本会话账本——`usage` 无默认值，漏传会当场失败而不是静默共享。"""
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(web.Session)}
    assert "usage" in fields
    assert fields["usage"].default is dataclasses.MISSING


# ---------------------------------------------------------------------------
# 3. 会话间不串号（端到端）
# ---------------------------------------------------------------------------


def _seed(sid: str, tokens: int, base: Path) -> None:
    """注入一个会话 + 它自己的账本，并记一次开销。

    `base` 必须是 tmp 目录：`record()` 会真的落盘，而 `saves/` 是仓库工作区
    （本文件第一版把账本写在 `saves/usage-<sid>.jsonl`，每次跑测试都会污染工作区）。
    """
    tracker = web.UsageTracker(base / f"usage-{sid}.jsonl", session=sid)
    tracker.record("deepseek-v4-flash", "turn", {"prompt_tokens": tokens})
    web.SESSIONS[sid] = web.Session(game=None, lock=threading.Lock(), usage=tracker)


def test_cost_endpoint_is_per_session(tmp_path):
    """**核心断言**：A 局的开销不出现在 B 局的成本报告里。

    修复前两个会话共用一个 tracker，本断言必红（两边的 calls 会相同且互相累加）。
    """
    web.SESSIONS.clear()
    _seed("sidA", 1000, tmp_path)
    _seed("sidB", 7, tmp_path)

    client = TestClient(web.app)
    a = client.get("/api/sidA/cost").json()
    b = client.get("/api/sidB/cost").json()

    assert a["calls"] == 1 and b["calls"] == 1
    assert a["path"].endswith("usage-sidA.jsonl")
    assert b["path"].endswith("usage-sidB.jsonl")
    # 报告里必须各自只见本局的 token 量级（1000 vs 7 不可能相等）
    assert a["report"] != b["report"]
    assert "1,000" in a["report"]
    assert "7" in b["report"] and "1,000" not in b["report"]
    # 账本确实各写各的文件（文件级隔离，不只是内存里的两个对象）
    assert (tmp_path / "usage-sidA.jsonl").is_file()
    assert (tmp_path / "usage-sidB.jsonl").is_file()


def test_cost_unknown_session_is_404():
    web.SESSIONS.clear()
    client = TestClient(web.app)
    assert client.get("/api/nope/cost").status_code == 404

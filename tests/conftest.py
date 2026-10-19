"""测试全局夹具。

只有一件事：**把所有 Web 会话级产物重定向到临时目录**。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _derived_work_is_inline(tmp_path, monkeypatch):
    """默认把 N8 的后台派生工作**关掉**，让既有守卫保持确定性。

    理由写在 `web.DERIVED_ASYNC` 的 docstring 里：异步执行意味着"回合返回之后
    状态还会变"，那会让"回合 N 之后立刻断言 state"的既有守卫变成偶发失败源。
    一千多条守卫不该因为一个性能特性开始闪。

    **异步路径由 `tests/test_derived_worker.py` 显式打开并单独覆盖**——
    不是让它顺带被测到，而是它有它自己的守卫。
    """
    from game_agent import web

    monkeypatch.setattr(web, "DERIVED_ASYNC", False)


@pytest.fixture(autouse=True)
def _isolate_session_artifacts(tmp_path, monkeypatch):
    """把 `web.SAVE_ROOT` 指到本测试的临时目录。

    **为什么需要它**（有实证，不是洁癖）：`saves/` 是仓库里**有意维护的证据库**
    （smoke-* / baseline-* / playthrough-* 等 90 余份带语义名字的运行记录），
    但跑测试的会话级产物名字里带**随机会话号**：

    - `saves/<pack_id>/autosave-<sid>.json`（B-4 按会话隔离的自动存档；N4 之后按卡分目录）
    - `saves/usage-<sid>.jsonl`（G1 每会话账本）
    - `saves/timeline-<sid>/`（N7 每会话时间线）

    测试**没法按名字清理后两类**，于是每跑一次全量就多一批——2026-10 实测跑完一轮
    漏出 21 个 `timeline-*` 目录（0.4 MB），而 `test_web.py` 里那些
    `unlink(missing_ok=True)` 的清理只覆盖它自己知道的固定文件名，盖不住这件事。

    只改 `SAVE_ROOT` 一个值就够：它的读取点（`_safe_save_path` / `api_saves` /
    `_timeline`）全都在**运行时**，没有 import 期捕获的副本。
    已经自己 patch 过它的测试照旧工作——monkeypatch 后写的覆盖前写的，
    两者都在测试结束时还原。
    """
    from game_agent import web

    monkeypatch.setattr(web, "SAVE_ROOT", tmp_path / "saves")

"""C5 守卫：质量门禁按钮（E1 / 冒烟），**成本前置确认必须在服务端强制**。

上游：`docs/plan-creator-player.md` 批次 C 的 C5——"E1 / 冒烟门禁按钮（手动，**成本前置
确认**）"，以及决策表第 4 条："生成时是否顺带生成 `judge_corpus`？**默认否**（¥0.5–1
且只服务 E1）；**'想上精选'时再点按钮**"。

**本文件里最重要的一条是 `test_confirm_is_required`**：这两道门会真机调模型花钱，
而 `confirm=true` 是**服务端**校验的——能被脚本直接调用的接口不该有"不确认就花钱"的
默认行为（与 `catalog.can_create` 拒绝遮蔽已发布包同一条纪律）。

故意**不测真机跑门禁**：那要几分钟且花钱。这里测的是"接口契约与控制流"，
用假 runner 替换子进程调用；真机跑门禁由 `scripts/judge_sensitivity.py` /
`scripts/worldpack_smoke.py` 各自的冒烟负责。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import game_agent.web as web

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "world-packs"
    root.mkdir(parents=True)
    shutil.copytree(REAL_PACK, root / "probe")
    monkeypatch.setattr(web, "_pack_root", lambda: root)
    monkeypatch.setattr(web, "SAVE_ROOT", tmp_path / "saves")
    monkeypatch.setattr(web, "GATES", {})
    return TestClient(web.app), root


def _fake_run(exit_code: int = 0, out: str = "[✓] 都过了"):
    """替掉 `web._run_gate` 里的子进程调用：记录命令 + 直接写结果。"""
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = exit_code
            stdout = out
            stderr = ""
        return R()

    return run, calls


# ---------------------------------------------------------------------------
# 1. 成本前置确认（最重要）
# ---------------------------------------------------------------------------


def test_confirm_is_required(client):
    """**核心断言**：不带 `confirm` 一律 400，且提示里要写清**花多少钱**。

    为什么要在服务端强制而不是只做个前端弹窗：接口能被脚本直接调用，
    "不确认就花钱"的默认行为不该存在。而且拒绝时必须把**成本**说出来——
    只说"需要确认"等于让调用方盲签一张金额未知的支票。
    """
    c, _ = client
    for kind, cost_hint in (("e1", "¥"), ("smoke", "¥")):
        r = c.post("/api/packs/probe/gate", json={"kind": kind})
        assert r.status_code == 400, f"{kind} 没确认就放行了"
        detail = r.json()["detail"]
        assert "确认" in detail and cost_hint in detail, detail
        assert "confirm=true" in detail, "要告诉调用方怎么继续（可照做）"


def test_unknown_kind_and_pack_are_rejected(client):
    c, _ = client
    r = c.post("/api/packs/probe/gate", json={"kind": "no_such", "confirm": True})
    assert r.status_code == 400 and "未知的门禁类型" in r.json()["detail"]
    r = c.post("/api/packs/no_such_pack/gate", json={"kind": "e1", "confirm": True})
    assert r.status_code == 400 and "未知的世界包" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 2. 控制流：事后可查、一次一道
# ---------------------------------------------------------------------------


def test_confirmed_gate_runs_and_records_result(client, monkeypatch):
    """确认之后真的起了一道门，结果可事后取（刷新页面也还在）。"""
    c, _ = client
    run, calls = _fake_run(exit_code=0, out="[✓] E1 通过\n细项…")
    monkeypatch.setattr(web.subprocess, "run", run)

    d = c.post("/api/packs/probe/gate", json={"kind": "e1", "confirm": True}).json()
    assert d["ok"] is True and d["gate_id"]
    assert "¥" in d["cost"], "返回值要带成本口径（前端据此显示）"

    # 等它跑完（假 runner 是瞬时的）
    for _ in range(50):
        g = c.get(f"/api/gates/{d['gate_id']}").json()
        if g["status"] != "running":
            break
        time.sleep(0.05)
    assert g["status"] == "done", g
    assert g["exit_code"] == 0
    assert "E1 通过" in g["output"]
    # 命令形状：脚本 + --pack + 包目录，逐项传参（不过 shell）
    assert calls and calls[0][0].endswith("python.exe") or calls[0][0]
    assert calls[0][1] == "scripts/judge_sensitivity.py"
    assert calls[0][2] == "--pack"
    assert calls[0][3].endswith("probe"), calls[0]

    # 列表里也能看到（刷新页面后还知道上次跑过什么）
    rows = c.get("/api/gates").json()
    assert any(x["gate_id"] == d["gate_id"] for x in rows["gates"])
    assert "e1" in rows["kinds"] and "smoke" in rows["kinds"]


def test_failed_gate_is_recorded_as_failed_not_done(client, monkeypatch):
    """退出码非零 → `failed`（门禁脚本用退出码表达"没过"），且输出照留。"""
    c, _ = client
    run, _ = _fake_run(exit_code=1, out="[✗] 拦截率不足")
    monkeypatch.setattr(web.subprocess, "run", run)
    d = c.post("/api/packs/probe/gate", json={"kind": "smoke", "confirm": True}).json()
    for _ in range(50):
        g = c.get(f"/api/gates/{d['gate_id']}").json()
        if g["status"] != "running":
            break
        time.sleep(0.05)
    assert g["status"] == "failed" and g["exit_code"] == 1
    assert "拦截率不足" in g["output"]


def test_only_one_gate_at_a_time(client, monkeypatch):
    """一次只允许跑一道门——两道门同时跑会两边都慢、成本还难以归因。"""
    c, _ = client
    started = []

    def slow_run(cmd, **kw):
        started.append(cmd)
        time.sleep(0.5)  # 占住"running"

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()

    monkeypatch.setattr(web.subprocess, "run", slow_run)
    first = c.post("/api/packs/probe/gate", json={"kind": "e1", "confirm": True})
    assert first.status_code == 200
    second = c.post("/api/packs/probe/gate", json={"kind": "smoke", "confirm": True})
    assert second.status_code == 400
    assert "已经有一道门在跑" in second.json()["detail"]


def test_subprocess_failure_is_reported_not_swallowed(client, monkeypatch):
    """起不了进程（脚本不在 / 环境坏）也要如实呈现——不能静默留一个 running。"""
    c, _ = client

    def boom(cmd, **kw):
        raise OSError("找不到脚本")

    monkeypatch.setattr(web.subprocess, "run", boom)
    d = c.post("/api/packs/probe/gate", json={"kind": "e1", "confirm": True}).json()
    for _ in range(50):
        g = c.get(f"/api/gates/{d['gate_id']}").json()
        if g["status"] != "running":
            break
        time.sleep(0.05)
    assert g["status"] == "failed"
    assert "无法执行" in g["output"]


def test_gate_thread_survives_the_registry_being_replaced(client, monkeypatch):
    """**背景线程不许带着异常死掉**——这条是真机全量测试里报出来的。

    原先 `_run_gate` 收尾时重新 `GATES[gid]` 取自己那条记录。脆弱点：这段时间里那个
    **模块全局**若被替换/清理过（测试的 monkeypatch、将来可能的清理策略），就 KeyError
    → 线程死掉、记录永远停在 `running`，而**外面看不到任何错误**
    （只留一条 pytest 的未处理线程异常警告）。

    现在线程拿到的是**自己那条 record 的引用**，与全局表怎么变无关。
    这里的断言方式：先抓住线程持有的那条记录，**再把全局换掉**，然后断言
    **那条记录**照样跑到了终态——线程真的活到了最后。
    """
    c, _ = client
    started = []

    def slow_run(cmd, **kw):
        started.append(cmd)
        time.sleep(0.3)

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()

    monkeypatch.setattr(web.subprocess, "run", slow_run)
    d = c.post("/api/packs/probe/gate", json={"kind": "e1", "confirm": True}).json()
    record = web.GATES[d["gate_id"]]  # 线程持有的就是这一条
    monkeypatch.setattr(web, "GATES", {})  # 门还在跑 → 把全局整个换掉

    for _ in range(60):
        if record["status"] != "running":
            break
        time.sleep(0.05)
    assert started, "假 runner 根本没被调用"
    assert record["status"] == "done", "线程没能更新自己的记录 → 它带着异常死了"
    assert record["finished_at"] is not None
    assert record["output"], "结果文本也要落到记录里"


def test_unknown_gate_id_is_404(client):
    c, _ = client
    assert c.get("/api/gates/nope").status_code == 404

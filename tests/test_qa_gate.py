"""B2（Track B）守卫测试：qa_gate 计划构建与报告渲染（离线，不嵌套跑 pytest）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.qa_gate import GateResult, build_plan, render_report

REPO = Path(__file__).resolve().parent.parent


def _ids(plan):
    return [g.id for g in plan]


def test_plan_default_l1_l2_order():
    plan = build_plan("world-packs/ancient_jianghu", ["l1", "l2"])
    assert _ids(plan) == ["l1-check", "l1-pytest", "l2-judge", "l2-smoke"]
    assert plan[1].cmd[:3] == [sys.executable, "-m", "pytest"]
    assert plan[2].needs_api and plan[3].needs_api
    assert not plan[0].needs_api and not plan[1].needs_api


def test_plan_l1_only_has_no_api_gates():
    plan = build_plan("world-packs/ancient_jianghu", ["l1"])
    assert _ids(plan) == ["l1-check", "l1-pytest"]
    assert all(not g.needs_api for g in plan)


def test_plan_l3_includes_longrun_with_turns():
    plan = build_plan("world-packs/xianxia_wendao", ["l3"], turns=50)
    assert _ids(plan) == ["l3-longrun"]
    assert "--turns" in plan[0].cmd and "50" in plan[0].cmd
    assert plan[0].needs_api


def test_plan_offline_passes_flag_and_drops_api_requirement():
    """offline 档：judge 门禁无法离线 → 不进计划；smoke/longrun 带 --offline 且不需要 key。"""
    plan = build_plan("world-packs/ancient_jianghu", ["l2", "l3"], offline=True)
    assert _ids(plan) == ["l2-smoke", "l3-longrun"]
    assert "--offline" in plan[0].cmd and "--offline" in plan[1].cmd
    assert not plan[0].needs_api and not plan[1].needs_api


def test_plan_resolves_pack_under_repo_root():
    plan = build_plan("world-packs/ancient_jianghu", ["l1"])
    assert plan[0].cmd[-1] == str(REPO / "world-packs" / "ancient_jianghu")


def test_report_fails_when_any_gate_fails():
    results = [
        GateResult("l1-check", "a", "l1", [], True, exit_code=0),
        GateResult("l1-pytest", "b", "l1", [], False, exit_code=1),
    ]
    report = render_report("world-packs/x", ["l1"], False, False, results)
    assert report["passed_all"] is False and report["executed"] == 2


def test_report_skipped_only_is_not_pass():
    """全部被跳过 ≠ 通过（'没测出来'不等于'没问题'）。"""
    results = [GateResult("l2-judge", "a", "l2", [], None, skipped_reason="无 key")]
    report = render_report("world-packs/x", ["l2"], False, False, results)
    assert report["passed_all"] is False
    assert report["executed"] == 0 and report["skipped"] == 1


def test_report_pass_with_skips_mixed():
    results = [
        GateResult("l1-check", "a", "l1", [], True, exit_code=0),
        GateResult("l2-judge", "b", "l2", [], None, skipped_reason="无 key"),
    ]
    report = render_report("world-packs/x", ["l1", "l2"], False, False, results)
    assert report["passed_all"] is True and report["skipped"] == 1
    assert report["gates"][1]["skipped_reason"] == "无 key"


def test_cli_dry_run_exits_zero_and_writes_report(tmp_path):
    """CLI 冒烟：--dry-run 不执行任何门禁，退出码 0，报告带 dry_run 标记。"""
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "qa_gate.py"),
         "--pack", "world-packs/ancient_jianghu", "--levels", "l1",
         "--dry-run", "--report-dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, "dry-run 应以 0 退出"
    files = list(tmp_path.glob("qa_gate_*.json"))
    assert len(files) == 1
    report = json.loads(files[0].read_text(encoding="utf-8"))
    assert report["dry_run"] is True
    assert report["executed"] == 0 and report["skipped"] == 2
    assert [g["id"] for g in report["gates"]] == ["l1-check", "l1-pytest"]

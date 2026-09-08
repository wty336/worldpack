"""③ 统一冒烟脚本测试（离线）：三包 profile 完整性 + --offline 全流程回归。

质量门命令链中的 `worldpack_smoke --pack` 环节在此离线验证（不触网）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "scripts" / "worldpack_smoke.py"

REQUIRED_KEYS = ("picks", "action", "days", "lines", "forbidden_scan", "target")


def _load_module():
    spec = importlib.util.spec_from_file_location("worldpack_smoke", SMOKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_offline(pack: str, days: int = 3) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SMOKE), "--pack", f"world-packs/{pack}", "--offline", "--days", str(days)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )


def test_profiles_cover_all_packs_with_required_keys():
    """三包 profile 齐全且字段完整（合并迁移无遗漏）。"""
    mod = _load_module()
    assert set(mod.PROFILES) == {"ancient_jianghu", "xianxia_wendao", "urban_neon"}
    for name, profile in mod.PROFILES.items():
        for key in REQUIRED_KEYS:
            assert profile.get(key), f"{name} 缺 profile 字段 {key}"
        assert profile["days"] >= 1
        # 禁表方向随包反转：都市包禁奇幻，古代/仙侠包禁现代
        assert any("修仙" in w or "魔法" in w for w in profile["forbidden_scan"]) if name == "urban_neon" else any(
            "手机" in w for w in profile["forbidden_scan"]
        )


def test_offline_smoke_all_three_packs():
    """--offline 全流程：三个包用同一脚本跑通（协议闭环 + 审计零偏差 + 禁表扫描）。"""
    for pack in ("ancient_jianghu", "xianxia_wendao", "urban_neon"):
        r = _run_offline(pack)
        assert r.returncode == 0, f"{pack} 离线冒烟失败:\n{r.stdout}\n{r.stderr}"
        assert "审计通过" in r.stdout
        assert "禁表扫描通过" in r.stdout


def test_offline_unknown_pack_reports_missing_profile():
    """未配置 profile 的包（如 baseline_probe）应报错而非静默错跑。"""
    r = _run_offline("baseline_probe")
    assert r.returncode != 0


def test_longrun_probe_offline_loop_regression():
    """④ 长局脚本 --offline：30 回合循环完整跑通（无结局路线 + 审计零偏差）。"""
    r = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "scripts" / "longrun_probe.py"),
            "--pack", "world-packs/xianxia_wendao", "--offline", "--turns", "30",
        ],
        capture_output=True, text=True, timeout=300, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, f"longrun 离线回归失败:\n{r.stdout}\n{r.stderr}"
    assert "审计通过" in r.stdout
    assert "意外提前结局" not in r.stdout  # 无结局路线在离线 30 回合内不被突破

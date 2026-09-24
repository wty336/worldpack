"""B2（Track B）：一键质量门 runner —— 把分散的门禁收进一条命令、一张报告卡。

README「质量门四连」+ 深度检查，按层执行：

- L1（离线）：check-worldpack → 全量 pytest（不需要 API key）；
- L2（真机）：judge_sensitivity → worldpack_smoke（需 DEEPSEEK_API_KEY，缺 key 跳过并报数）；
- L3（深度）：longrun_probe（可选，约 ¥1-2）。

用法::

    python scripts/qa_gate.py --pack world-packs/xianxia_wendao
    python scripts/qa_gate.py --pack world-packs/xianxia_wendao --levels l1,l2,l3 --turns 100
    python scripts/qa_gate.py --pack world-packs/xianxia_wendao --offline   # 真机门禁用 --offline 档
    python scripts/qa_gate.py --pack world-packs/xianxia_wendao --dry-run   # 只打印计划

报告：``reports/qa_gate_<时间戳>.json``（逐门 pass/skip/exit_code/耗时/输出尾部）。
退出码：全部已执行门禁通过 → 0；任一失败或**全部被跳过** → 1
（"没测出来"不等于"没问题"，与 judge 三态判定同纪律）。

子进程输出走**文件重定向**而非管道捕获：既避免平台管道缓冲差异，
也让每个门禁的完整 stdout 留在临时文件里、报告只收尾部摘要。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable  # 与宿主同解释器（uv run 下即 venv python，不依赖 uv 本体）


@dataclass
class Gate:
    id: str
    name: str
    cmd: list[str]
    level: str
    needs_api: bool = False


def _pack_arg(pack: str) -> str:
    """相对路径按仓库根解析（与门禁脚本的既有用法一致）。"""
    p = Path(pack)
    return str(p if p.is_absolute() else REPO_ROOT / p)


def build_plan(
    pack: str, levels: list[str], offline: bool = False, turns: int = 100
) -> list[Gate]:
    gates: list[Gate] = []
    if "l1" in levels:
        gates.append(
            Gate("l1-check", "世界包离线校验",
                 [PY, "-m", "game_agent", "check-worldpack", _pack_arg(pack)], "l1")
        )
        gates.append(
            Gate("l1-pytest", "全量离线测试", [PY, "-m", "pytest", "-q"], "l1")
        )
    if "l2" in levels:
        # judge_sensitivity 没有 --offline 档（Judge 门禁无法离线）→ offline 模式不进计划
        if not offline:
            gates.append(
                Gate("l2-judge", "Judge 对抗语料门禁",
                     [PY, str(REPO_ROOT / "scripts" / "judge_sensitivity.py"),
                      "--pack", _pack_arg(pack)], "l2", needs_api=True)
            )
        smoke_cmd = [PY, str(REPO_ROOT / "scripts" / "worldpack_smoke.py"),
                     "--pack", _pack_arg(pack)]
        if offline:
            smoke_cmd.append("--offline")
        gates.append(
            Gate("l2-smoke", "真机冒烟（通关+审计+禁表）", smoke_cmd, "l2",
                 needs_api=not offline)
        )
    if "l3" in levels:
        long_cmd = [PY, str(REPO_ROOT / "scripts" / "longrun_probe.py"),
                    "--pack", _pack_arg(pack), "--turns", str(turns)]
        if offline:
            long_cmd.append("--offline")
        gates.append(
            Gate("l3-longrun", f"深度长局（{turns} 回合）", long_cmd, "l3",
                 needs_api=not offline)
        )
    return gates


@dataclass
class GateResult:
    id: str
    name: str
    level: str
    command: list[str]
    passed: bool | None  # None = 跳过（dry-run / 缺 API key）
    skipped_reason: str = ""
    exit_code: int | None = None
    duration_s: float = 0.0
    stdout_tail: str = ""


def _has_api_key() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())


def run_plan(plan: list[Gate], dry_run: bool = False) -> list[GateResult]:
    results: list[GateResult] = []
    for gate in plan:
        if dry_run:
            results.append(GateResult(
                gate.id, gate.name, gate.level, gate.cmd, None,
                skipped_reason="dry-run 未执行"))
            continue
        if gate.needs_api and not _has_api_key():
            results.append(GateResult(
                gate.id, gate.name, gate.level, gate.cmd, None,
                skipped_reason="未配置 DEEPSEEK_API_KEY（真机门禁跳过）"))
            continue
        t0 = time.monotonic()
        # 文件重定向而非管道：完整 stdout 留在临时文件，报告只收尾部摘要
        with tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", errors="replace",
            suffix=f"-{gate.id}.log", delete=False,
        ) as out:
            tmp_path = out.name
            try:
                proc = subprocess.run(
                    gate.cmd, stdout=out, stderr=subprocess.STDOUT,
                    cwd=REPO_ROOT, check=False,
                )
                exit_code = proc.returncode
            except Exception as e:  # noqa: BLE001
                out.write(f"\n[qa_gate 异常] {type(e).__name__}: {e}")
                exit_code = -1
        tail = ""
        try:
            tail = Path(tmp_path).read_text(encoding="utf-8", errors="replace")[-800:]
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        results.append(GateResult(
            gate.id, gate.name, gate.level, gate.cmd, exit_code == 0,
            exit_code=exit_code, duration_s=round(time.monotonic() - t0, 1),
            stdout_tail=tail,
        ))
    return results


def render_report(
    pack: str,
    levels: list[str],
    offline: bool,
    dry_run: bool,
    results: list[GateResult],
) -> dict:
    executed = [r for r in results if r.passed is not None]
    skipped = [r for r in results if r.passed is None]
    passed_all = bool(executed) and all(r.passed for r in executed)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pack": pack,
        "levels": levels,
        "offline": offline,
        "dry_run": dry_run,
        "passed_all": passed_all,
        "executed": len(executed),
        "skipped": len(skipped),
        "gates": [
            {
                "id": r.id,
                "name": r.name,
                "level": r.level,
                "command": r.command,
                "passed": r.passed,
                "skipped_reason": r.skipped_reason,
                "exit_code": r.exit_code,
                "duration_s": r.duration_s,
                "stdout_tail": r.stdout_tail,
            }
            for r in results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qa_gate", description="一键质量门：L1 离线 / L2 真机 / L3 深度长局"
    )
    parser.add_argument("--pack", default="world-packs/ancient_jianghu")
    parser.add_argument("--levels", default="l1,l2",
                        help="逗号分隔：l1 离线 / l2 真机 / l3 深度长局（默认 l1,l2）")
    parser.add_argument("--turns", type=int, default=100, help="L3 长局回合数（默认 100）")
    parser.add_argument("--offline", action="store_true",
                        help="真机门禁改用 --offline 档（本地回归，不需要 API key）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划命令，不执行")
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "reports"),
                        help="报告输出目录（默认 reports/）")
    args = parser.parse_args(argv)

    levels = [x.strip() for x in args.levels.split(",") if x.strip()]
    plan = build_plan(args.pack, levels, args.offline, args.turns)

    print(f"qa_gate · 包 {args.pack} · levels {levels} · "
          f"{'OFFLINE' if args.offline else 'LIVE'}{' · DRY-RUN' if args.dry_run else ''}")
    if args.offline and "l2" in levels:
        print("[offline] l2-judge 不进计划（Judge 门禁无法离线）——真机发版必须单独补跑")
    for g in plan:
        print(f"  [{g.id:<12}] {' '.join(g.cmd)}")

    results = run_plan(plan, dry_run=args.dry_run)
    report = render_report(args.pack, levels, args.offline, args.dry_run, results)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"qa_gate_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== qa_gate 报告卡 =====")
    for r in results:
        if r.passed is None:
            print(f"  [·] {r.id:<12} 跳过：{r.skipped_reason}")
        elif r.passed:
            print(f"  [✓] {r.id:<12} {r.duration_s:>6}s")
        else:
            print(f"  [✗] {r.id:<12} exit={r.exit_code} {r.duration_s:>6}s")
    skipped = report["skipped"]
    if skipped:
        print(f"[!] {skipped} 个门禁被跳过——'没测出来'不等于'没问题'")
    print(f"报告已写入 {out}")
    if args.dry_run:
        print("[dry-run] 未执行任何门禁")
        return 0
    if report["passed_all"]:
        print("[✓] 全部已执行门禁通过")
        return 0
    print("[✗] 门禁未全部通过（或全部被跳过）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

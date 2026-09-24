"""A5（runtime 平台化 ①）：记忆冲突判定门禁——三态语料真机评测（基线期）。

- 语料：``eval-sets/conflict_cases.yaml``（取代/并存/无冲突 各 8 条）；
- 判据：``game_agent/memory.py judge_conflict``（写路径同款判定，纯函数）；
- 口径：每条 3 轮多数票（判定类噪声纪律）；按类报准确率；
  **基线期不做硬门**（阈值待第一批基线后定，plan-runtime-platform §1.3）；
- 已知口径洞：判定失败与"无冲突"都落在 ('none', [])——报告注明，
  失败率经 usage/trace 另行对账。

用法::

    python scripts/conflict_gate.py [--rounds 3]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import yaml

from game_agent.config import load_settings
from game_agent.llm import LLMClient
from game_agent.memory import judge_conflict
from game_agent.state import MemoryEntry
from game_agent.usage import UsageTracker

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = REPO_ROOT / "eval-sets" / "conflict_cases.yaml"

KIND_TO_LABEL = {"supersede": "取代", "coexist": "并存", "none": "无冲突"}
LABELS = ("取代", "并存", "无冲突")


def load_cases(path: str | Path = DEFAULT_CORPUS) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases: list[dict] = []
    seen: set[str] = set()
    for item in data.get("cases", []):
        cid = str(item["id"])
        if cid in seen:
            raise ValueError(f"冲突语料 id 重复: {cid}")
        seen.add(cid)
        expect = str(item["expect"])
        if expect not in LABELS:
            raise ValueError(f"未知期望标签 {expect!r}（用例 {cid}）")
        cases.append({
            "id": cid,
            "existing": [str(x) for x in item["existing"]],
            "new": str(item["new"]),
            "expect": expect,
        })
    if not cases:
        raise ValueError(f"冲突语料为空: {path}")
    return cases


def summarize(results: list[dict]) -> dict:
    """按类统计准确率（多数票口径；unknown = 判定失败按 'none' 计的已知口径洞）。"""
    out: dict[str, dict] = {}
    for label in LABELS:
        items = [r for r in results if r["expect"] == label]
        correct = sum(1 for r in items if r["majority"] == label)
        out[label] = {
            "n": len(items),
            "correct": correct,
            "accuracy": round(correct / len(items), 3) if items else None,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="conflict_gate", description="记忆冲突判定门禁（基线期）")
    parser.add_argument("--rounds", type=int, default=3, help="每条用例重复轮数（多数票）")
    args = parser.parse_args(argv)

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（本门禁是真机门禁）")
        return 1
    cases = load_cases()
    tracker = UsageTracker("reports/usage-conflict-gate.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)

    results: list[dict] = []
    for case in cases:
        entries = [MemoryEntry(fact=f, day=1, round=0) for f in case["existing"]]
        rounds = []
        for _ in range(args.rounds):
            kind, _ = judge_conflict(llm, entries, case["new"])
            rounds.append(KIND_TO_LABEL.get(kind, "无冲突"))
        from collections import Counter

        majority = Counter(rounds).most_common(1)[0][0]
        hit = majority == case["expect"]
        print(f"  [{'✓' if hit else '✗'}] {case['id']:<10} 期望 {case['expect']:<4} "
              f"多数票 {majority:<4} 各轮 {rounds}")
        results.append({
            "id": case["id"], "expect": case["expect"], "majority": majority,
            "hit": hit, "rounds": rounds,
        })

    summary = summarize(results)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": settings.model,
        "rounds": args.rounds,
        "content_version": "conflict-v1-20260924",
        "note": "基线期：无硬门阈值；判定失败与无冲突同落 none 口径，失败率经 usage 另行对账",
        "summary": summary,
        "cases": results,
    }
    reports_dir = REPO_ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    out = reports_dir / f"conflict_gate_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 冲突判定基线 =====")
    for label, s in summary.items():
        print(f"  {label:<4} {s['correct']}/{s['n']} = {s['accuracy']}")
    print(f"\n报告已写入 {out}（基线期：阈值待定，不设硬门）\n" + tracker.cost_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""A4 语义去重评测（Phase 1 · Step 1）：在**冻结评测集**上测拦截率与误拦率。

用法：
    python scripts/dedup_test.py            # 基线模式：只出报告，退出码 0
    python scripts/dedup_test.py --gate     # 门禁：拦截率 ≥0.80 且 误拦率 ≤0.10
    python scripts/dedup_test.py --dry-run  # 只打印用例（零 API）
    python scripts/dedup_test.py --limit N  # 抽样调试

评测集：`eval-sets/dedup/pairs.yaml`（冻结，见 `eval-sets/MANIFEST.md`）
- 正例 `expect_duplicate: true`  → 应拦截（同义改写）
- 负例 `expect_duplicate: false` → 应放行（近义不同/极性相反/边界）

为什么必须看误拦率：旧版 10 组**全是正例**，一个"全部判重复"的模型同样能拿 10/10。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from game_agent.budgets import DEDUP_MAX_TOKENS
from game_agent.config import load_settings
from game_agent.llm import LLMClient
from game_agent.memory import MemorySystem
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_SET = REPO_ROOT / "eval-sets" / "dedup" / "pairs.yaml"
PACK = "world-packs/ancient_jianghu"  # 仅为取得记忆系统配置；事实文本与题材无关
INTERCEPT_MIN = 0.80  # 正例拦截率下限
FALSE_BLOCK_MAX = 0.10  # 负例误拦率上限


def evaluate(pairs: list[dict], intercepted: list[bool]) -> dict:
    """纯函数：按正负例分别统计（便于离线单测）。"""
    pos = [(p, hit) for p, hit in zip(pairs, intercepted) if p["expect_duplicate"]]
    neg = [(p, hit) for p, hit in zip(pairs, intercepted) if not p["expect_duplicate"]]
    pos_hits = sum(1 for _, hit in pos if hit)
    neg_hits = sum(1 for _, hit in neg if hit)  # 负例被拦 = 误拦
    intercept_rate = pos_hits / len(pos) if pos else 0.0
    false_block_rate = neg_hits / len(neg) if neg else 0.0
    return {
        "n": len(pairs),
        "positives": len(pos),
        "intercepted": pos_hits,
        "intercept_rate": round(intercept_rate, 3),
        "negatives": len(neg),
        "false_blocked": neg_hits,
        "false_block_rate": round(false_block_rate, 3),
        "gate_ok": intercept_rate >= INTERCEPT_MIN and false_block_rate <= FALSE_BLOCK_MAX,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语义去重评测（冻结评测集）")
    parser.add_argument("--gate", action="store_true", help="门禁模式：不达标退出 1")
    parser.add_argument("--dry-run", action="store_true", help="只打印用例，零 API")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 对（调试）")
    args = parser.parse_args(argv)

    pairs = yaml.safe_load(EVAL_SET.read_text(encoding="utf-8"))["pairs"]
    if args.limit:
        pairs = pairs[: args.limit]
    eval_sha = hashlib.sha256(EVAL_SET.read_bytes()).hexdigest()
    n_pos = sum(1 for p in pairs if p["expect_duplicate"])
    print(f"评测集 {EVAL_SET.relative_to(REPO_ROOT)} · {len(pairs)} 对"
          f"（正例 {n_pos} / 负例 {len(pairs) - n_pos}）· sha256={eval_sha[:12]}")
    print(f"预算 DEDUP_MAX_TOKENS={DEDUP_MAX_TOKENS}\n")

    if args.dry_run:
        for p in pairs:
            flag = "应拦" if p["expect_duplicate"] else "应放"
            print(f"  [{flag}] {p['id']:<18} {p['kind']:<12} A「{p['a']}」 B「{p['b']}」")
        return 0

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（.env）")
        return 1
    tracker = UsageTracker("reports/usage-dedup.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    pack = load_worldpack(PACK)
    memory = MemorySystem(pack, llm)
    print(f"模型 {llm.model_for('dedup')}\n")

    intercepted: list[bool] = []
    records: list[dict] = []
    for p in pairs:
        state = GameState.from_pack(pack)
        memory.add(state, "player", p["a"])
        result = memory.add(state, "player", p["b"])
        hit = "跳过" in result
        intercepted.append(hit)
        ok = hit if p["expect_duplicate"] else not hit
        records.append({"id": p["id"], "kind": p["kind"], "expect_duplicate": p["expect_duplicate"],
                        "intercepted": hit, "correct": ok, "result": result[:40]})
        print(f"  [{'✓' if ok else '✗'}] {p['id']:<18} {p['kind']:<12}"
              f" → {'拦截' if hit else '放行'}（{result[:32]}）")

    s = evaluate(pairs, intercepted)
    print(f"\n正例拦截率 {s['intercepted']}/{s['positives']} = {s['intercept_rate']:.0%}"
          f"（要求 ≥{INTERCEPT_MIN:.0%}）")
    print(f"负例误拦率 {s['false_blocked']}/{s['negatives']} = {s['false_block_rate']:.0%}"
          f"（要求 ≤{FALSE_BLOCK_MAX:.0%}）")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": llm.model_for("dedup"),
        "baseline": "zero-shot",
        "temperature": 0.0,
        "budget_policy": f"game_agent/budgets.py DEDUP_MAX_TOKENS={DEDUP_MAX_TOKENS}",
        "eval_set": str(EVAL_SET.relative_to(REPO_ROOT)),
        "eval_set_sha256": eval_sha,
        "thresholds": {"intercept_min": INTERCEPT_MIN, "false_block_max": FALSE_BLOCK_MAX},
        "summary": s,
        "cases": records,
    }
    out_path = Path("reports") / f"dedup-eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {out_path}")
    print("\n" + tracker.cost_report())

    if args.gate:
        print("\n[✓] 门禁通过" if s["gate_ok"] else "\n[✗] 门禁未通过")
        return 0 if s["gate_ok"] else 1
    print("\n（基线模式：仅记录，不作门禁判定；加 --gate 启用门禁）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

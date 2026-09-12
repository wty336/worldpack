"""路线 A 工厂的成本回填（Task 11 Step 3）：从 usage JSONL 汇总实付，与 spec §10.2 估算并列。

**为什么需要工具**：`UsageTracker.cost_report()` 只统计**本实例内存**里的记录（不读文件），
而工厂跑批是"一次进程一条批"——事后回填只能读盘。本工具读 JSONL 重算，价格口径与
`game_agent.usage`（`PRICES` + `_price_of`）**同源**，不另写一份价格表。

**用途标签（`purpose`）怎么分组**（标签由 Task 11 加的 `purpose=` 参数写入，见验收记录）：
演绎 → `verbalize_extract` / `verbalize_judge` / `verbalize_compress`；
摘要生成 → `compress`；拒绝采样选优 → `rubric_select`；质检员 → `rubric_quality`；
轨道 2 评委 → `rubric_score` / `rubric_pairwise`。
**认不出的标签一律进「其他」并单独列行**——绝不能让花掉的钱从表里消失。

用法：
    python scripts/route_a_cost.py                     # 汇总缺省日志，打印 Markdown
    python scripts/route_a_cost.py --out reports/route-a-cost-YYYYMMDD.md
    python scripts/route_a_cost.py --samples 2180      # 按样本数外推到生产规模
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from game_agent.usage import _price_of

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO_ROOT / "reports" / "usage-route-a.jsonl"

# 用途标签 → §10.2 的组件行（未列出的一律「其他」，见模块 docstring）
COMPONENT_OF = {
    "verbalize_extract": "演绎·extract",
    "verbalize_judge": "演绎·judge",
    "verbalize_compress": "演绎·compress",
    "compress": "摘要生成（compress 侧信道，含拒绝采样候选）",
    "rubric_select": "拒绝采样选优",
    "rubric_quality": "质检员抽检",
    "rubric_score": "轨道 2 评委打分",
    "rubric_pairwise": "轨道 2 评委成对",
    "aux": "其他·未分类（早期标签：演绎/质检混记）",
}


def load_usage(path: Path) -> list[dict]:
    """读 JSONL。空行跳过；**坏行直接报错**（宁可停下，也不静默少算钱）。"""
    rows: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:  # pragma: no cover - 防御性
            raise ValueError(f"{path}:{i} 不是合法 JSON 行：{e}") from e
    return rows


def entry_cost(entry: dict) -> float:
    """单条调用的费用（元）。口径与 `usage.UsageTracker.cost_report()` 逐项一致。"""
    hit = entry.get("cache_hit_tokens", 0)
    miss = entry.get("cache_miss_tokens", 0)
    prompt = hit + miss if (hit or miss) else entry.get("prompt_tokens", 0)
    out = entry.get("completion_tokens", 0)
    model = entry.get("model", "")
    # 缓存未命中按未命中价、命中按命中价；无缓存字段则整体按未命中价（与 usage.py 同）
    if hit or miss:
        return (hit * _price_of(model, "cache_hit")
                + miss * _price_of(model, "cache_miss")
                + out * _price_of(model, "output")) / 1_000_000
    return (prompt * _price_of(model, "cache_miss")
            + out * _price_of(model, "output")) / 1_000_000


def summarize(rows: list[dict]) -> dict:
    """按 (组件, model, purpose) 汇总 → 组件级 + 总计。金额与 token 都算。"""
    by_key: dict[tuple[str, str, str], dict] = {}
    total = {"calls": 0, "prompt": 0, "completion": 0, "cache_hit": 0, "cache_miss": 0, "cost": 0.0}
    for e in rows:
        purpose = e.get("purpose", "?")
        component = COMPONENT_OF.get(purpose, "其他·未分类（认不出的标签）")
        model = e.get("model", "?")
        key = (component, model, purpose)
        row = by_key.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0,
                                      "cache_hit": 0, "cache_miss": 0, "cost": 0.0})
        row["calls"] += 1
        row["prompt"] += e.get("prompt_tokens", 0)
        row["completion"] += e.get("completion_tokens", 0)
        row["cache_hit"] += e.get("cache_hit_tokens", 0)
        row["cache_miss"] += e.get("cache_miss_tokens", 0)
        row["cost"] += entry_cost(e)
        total["calls"] += 1
        total["prompt"] += e.get("prompt_tokens", 0)
        total["completion"] += e.get("completion_tokens", 0)
        total["cache_hit"] += e.get("cache_hit_tokens", 0)
        total["cache_miss"] += e.get("cache_miss_tokens", 0)
        total["cost"] += entry_cost(e)
    total["cost"] = round(total["cost"], 4)
    return {"by_key": by_key, "total": total}


def format_md(summary: dict, *, samples: int = 0, target_samples: int = 0,
              fingerprints: dict | None = None) -> str:
    """Markdown 表：分组件实付 +（可选）按样本数线性外推的生产规模估算。"""
    out = ["| 组件 | 模型 | 用途标签 | 调用 | 入 token | 出 token | 实付 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for (component, model, purpose), row in sorted(summary["by_key"].items()):
        out.append(f"| {component} | {model} | `{purpose}` | {row['calls']} | {row['prompt']:,} | "
                   f"{row['completion']:,} | ¥{row['cost']:.4f} |")
    t = summary["total"]
    out.append(f"| **合计** | — | — | **{t['calls']}** | **{t['prompt']:,}** | "
               f"**{t['completion']:,}** | **¥{t['cost']:.4f}** |")
    if samples and target_samples:
        unit = t["cost"] / samples
        out.append("")
        out.append(f"单位成本 ≈ **¥{unit:.5f}/样本**（{samples} 条实测）→ 外推 "
                   f"{target_samples:,} 条 ≈ **¥{unit * target_samples:.2f}**")
    if fingerprints:
        out.append("")
        out.append("指纹：" + " · ".join(f"`{k}`={v}" for k, v in fingerprints.items()))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="路线 A 工厂成本回填（读 usage JSONL）")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--out", type=Path, default=None, help="把 Markdown 写入文件")
    ap.add_argument("--samples", type=int, default=0, help="本次跑批实际出库样本数（用于单位成本）")
    ap.add_argument("--target-samples", type=int, default=0, help="外推目标样本数（默认 spec §10.2 的 2100）")
    ap.add_argument("--no-fingerprints", action="store_true", help="跳过端点指纹探测（离线）")
    args = ap.parse_args(argv)

    if not args.log.exists():
        print(f"[✗] usage 日志不存在：{args.log}（先跑一次出库）")
        return 1
    rows = load_usage(args.log)
    summary = summarize(rows)
    fps: dict = {}
    if not args.no_fingerprints:
        from game_agent.config import load_settings
        from game_agent.endpoint import fingerprint_for

        settings = load_settings()
        fps = {
            "endpoint": fingerprint_for(settings, "compress").get("model"),
            "base_url": settings.base_url,
            "budget_policy": _budget_policy(),
        }
    md = format_md(summary, samples=args.samples, target_samples=args.target_samples or 2100,
                   fingerprints=fps)
    print(f"usage 日志：{args.log}（{len(rows)} 条调用）\n")
    print(md)
    if args.out:
        args.out.write_text(md + "\n", encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


def _budget_policy() -> str:
    """预算策略指纹（与 `rubric_judge.budget_policy()` 同源：budgets.py 的 sha 前 12 位）。"""
    from scripts.rubric_judge import budget_policy

    return budget_policy()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

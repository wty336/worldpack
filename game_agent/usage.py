"""C2 usage 记账（P1 / improvement-roadmap §5 C2）：统一采集 LLM 调用用量。

- UsageTracker：每次 API 调用追加一条 JSONL（时间/模型/用途/token 明细）；
- 价格快照：DeepSeek 官方定价页（2026-09 抓取，峰谷分时定价）——
  空闲时段元/百万 tokens；高峰时段（工作日 9-12/14-18 北京时间）= 空闲 ×2。
  价格变动频繁，以 https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ 最新公告为准；
- 成本报告：token 为精确值；成本 = token × 单价（价格可经环境变量覆盖）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# 价格快照（元/百万 tokens，空闲时段）。高峰 = PEAK_FACTOR ×。
# 来源：DeepSeek 官方定价页 2026-09（缓存命中/未命中/输出三价）。
PRICES: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"cache_hit": 0.05, "cache_miss": 1.5, "output": 4.5},
    "deepseek-v4-pro": {"cache_hit": 0.15, "cache_miss": 4.5, "output": 13.5},
    "deepseek-v4-flash-vision-exp": {"cache_hit": 0.05, "cache_miss": 1.5, "output": 4.5},
}
PEAK_FACTOR = 2.0  # 高峰时段（北京工作日 9:00-12:00、14:00-18:00）价格翻倍


def _price_of(model: str, key: str) -> float:
    """读取模型单价；可用 env 覆盖（如 DEEPSEEK_PRICE_FLASH_OUTPUT=9.0）。"""
    env_key = f"DEEPSEEK_PRICE_{model.upper().replace('-', '_')}_{key.upper()}"
    raw = os.environ.get(env_key, "").strip()
    if raw:
        return float(raw)
    return PRICES.get(model, {}).get(key, 0.0)


def usage_fields(resp: Any) -> dict[str, int] | None:
    """从 API 响应提取用量字段。缺失时返回 None（旧 provider / 测试 fake 容错）。"""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    fields: dict[str, int] = {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    miss = getattr(usage, "prompt_cache_miss_tokens", None)
    if hit is not None:
        fields["cache_hit_tokens"] = int(hit or 0)
    if miss is not None:
        fields["cache_miss_tokens"] = int(miss or 0)
    return fields


class UsageTracker:
    """逐次调用追加 JSONL 落盘 + 汇总/成本报告。落盘失败静默（不影响游戏）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: list[dict] = []

    def record(
        self, model: str, purpose: str, usage: dict[str, int] | None = None, ts: str | None = None
    ) -> None:
        entry: dict[str, Any] = {
            "ts": ts or datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "purpose": purpose,
        }
        if usage:
            entry.update(usage)
        self.entries.append(entry)
        self._append(entry)

    def _append(self, entry: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 汇总与成本
    # ------------------------------------------------------------------

    def summarize(self) -> dict[str, dict[str, Any]]:
        """按 (model, purpose) 汇总：调用次数 + 各 token 维度累计。"""
        out: dict[str, dict[str, Any]] = {}
        for e in self.entries:
            key = f"{e['model']}@{e['purpose']}"
            row = out.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0,
                                       "cache_hit": 0, "cache_miss": 0})
            row["calls"] += 1
            row["prompt"] += e.get("prompt_tokens", 0)
            row["completion"] += e.get("completion_tokens", 0)
            row["cache_hit"] += e.get("cache_hit_tokens", 0)
            row["cache_miss"] += e.get("cache_miss_tokens", 0)
        return out

    def cost_report(self) -> str:
        """多行成本报告：token 精确值 + 空闲/高峰时段成本估算。"""
        lines = ["===== usage 成本报告 ====="]
        total = {"offpeak": 0.0, "peak": 0.0, "prompt": 0, "completion": 0}
        for key, row in sorted(self.summarize().items()):
            model, purpose = key.rsplit("@", 1)
            hit, miss = row["cache_hit"], row["cache_miss"]
            prompt_tok = hit + miss if (hit or miss) else row["prompt"]
            comp_tok = row["completion"]
            off = (
                hit * _price_of(model, "cache_hit")
                + miss * _price_of(model, "cache_miss")
                + comp_tok * _price_of(model, "output")
            ) / 1_000_000
            known = model in PRICES
            total["prompt"] += row["prompt"]
            total["completion"] += row["completion"]
            total["offpeak"] += off
            total["peak"] += off * PEAK_FACTOR
            lines.append(
                f"  {key:<40} 调用 {row['calls']:>4} 次 · "
                f"入 {row['prompt']:>9,}（命中 {hit:,} / 未命中 {miss:,}）· "
                f"出 {comp_tok:>8,}"
                + (f" · 约 ¥{off:.3f}（空闲）" if known else " · 价格未知")
            )
        lines.append(
            f"  合计：入 {total['prompt']:,} · 出 {total['completion']:,} · "
            f"成本约 ¥{total['offpeak']:.3f}（空闲时段）/ ¥{total['peak']:.3f}（高峰时段）"
        )
        lines.append("  注：价格快照 2026-09（峰谷分时），以 DeepSeek 最新公告为准。")
        return "\n".join(lines)

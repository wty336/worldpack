"""M2a 记忆回归验收：与基线同事实集/同 seed/同探针流程，输出 memory-regression-report.*。

用法：uv run python scripts/memory_regression.py [--seed 42] [--max-turns 120] [--resume]

对比对象：saves/baseline-report.md（M1.5 基线：120 回合保持率 50%，3 处虚假记忆）。
M2a 验收标准（plan-m2.md §2）：
  - 记忆保持率 ≥ 80%（基线 50%）；
  - 虚假记忆 0 条；
  - 保留集（两结局通关/注入/OOC）不退化。
"""

from __future__ import annotations

from baseline_probe import main as probe_main

if __name__ == "__main__":
    import sys

    argv = [a for a in sys.argv[1:] if not a.startswith("--out-prefix")]
    raise SystemExit(probe_main(["--out-prefix", "memory-regression", *argv]))

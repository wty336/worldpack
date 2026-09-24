"""B1（Track B）：LLM 调用 trace —— turn 回合 / API 调用 / 工具执行的 JSONL 轨迹。

与 usage 记账（C2）分工：
- usage：成本证据（token × 单价），按 (model, purpose) 汇总；
- trace：**过程证据**（时延 / verdict / 重试 / 熔断），按事件流回放"为什么这轮失败"。

事件三类（每条一行 JSON，按 seq 排序即完整时间线）：
- ``turn_begin`` / ``turn_end``：一个叙事回合的边界（iterations / outcome / plot_signal）；
- ``call``：一次 API 请求（purpose / model / latency_ms / finish_reason / usage / error）；
- ``tool``：一次工具执行（name / status: ok|rejected|bad_json|protocol_error|unknown / detail）。

纪律（继承 usage.py）：
- 落盘失败**静默**：trace 是观测层，不得反过来影响游戏；
- 跨进程追加走 usage 的同一把文件锁（实测撕裂行教训）；
- 默认关闭：``Settings.trace_path`` 为空时不建 recorder（零开销、零行为变化）。
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .usage import _WRITE_LOCK, _append_lock


class TraceRecorder:
    """逐事件追加 JSONL。seq 保证同进程内的全序；跨进程用 pid 区分。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._seq = itertools.count(1)

    def record(self, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "seq": next(self._seq),
            "pid": os.getpid(),
            "event": event,
        }
        entry.update(fields)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with _WRITE_LOCK, _append_lock(self.path):
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:  # noqa: BLE001
            pass  # 观测层：写不进就放弃，不影响游戏

# Track B 执行报告：训练兼容的 agent 增量（B1 trace / B2 qa_gate / B3 路由）

> 上游：会话路线规划「三条轨道 + 两个冻结窗口」（Track B = 训练兼容的 agent 增量，
> 零/低契约影响，不阻塞 Track A 训练主线）。
> 纪律：**引擎改动 = 新实验 = 全量回归**；所有改动默认关闭、纯增量、不碰消息契约。

---

## 0. 一句话结论

Track B 三件全部落地：**B1** LLM 调用 trace 事件流（默认关闭，JSONL 回放"为什么这轮失败"）、
**B2** 一键质量门 runner（L1 离线 / L2 真机 / L3 深度 + 报告卡）、
**B3** 侧信道专属模型路由补齐（extract/reflect/dedup 可从 env 指定本地小模型）。
全量回归 **565 → 590 passed**；L1 实测通过（check-worldpack 1.4s + pytest 29.9s）。

---

## 1. 为什么这三件"契约中立"

Track B 的选型判据：**不改变任何 LLM 调用的输入/输出契约**，因此：

- 不触发"换尺子重测"——冻结的 `eval-sets/`、五包语料基线、场景卡工厂的
  材料装配器（逐字复用 `status_text`）全部继续有效；
- 不需要重跑 flash / 14B 任何一侧的基线；
- 与 Phase 1 训练主线零冲突：**截至 2026-09-24 正式训练尚未完成**（数据已就绪
  `data/training/` 9 个 jsonl、100 步试跑已过，见 `reports/train-debug-20260919.md`），
  Track B 只做观测与调度，不参与训练数据与配方。

| 项 | 改动面 | 契约影响 |
| --- | --- | --- |
| B1 trace | `game_agent/trace.py`（新）+ `llm.py` 挂点 + `config.py` 一个字段 | 无（只读观测，落盘失败静默） |
| B2 qa_gate | `scripts/qa_gate.py`（新，纯 scripts 层） | 无（只调度既有门禁脚本） |
| B3 路由 | `config.py` 3 字段 + `llm.py` `from_settings` 映射 | 无（只加路由，不改消息；已列入 plan-local-14b §8.2） |

---

## 2. B1 · LLM 调用 trace（`game_agent/trace.py`）

### 2.1 设计

与 usage 记账（C2）分工：**usage = 成本证据，trace = 过程证据**。
三类事件，每条一行 JSON，按 `seq` 排序即完整时间线：

| 事件 | 关键字段 | 回答什么问题 |
| --- | --- | --- |
| `turn_begin` / `turn_end` | turn_seq / model / outcome(completed\|meltdown) / iterations / narration_chars / plot_signal | 这轮成没成、重试了几次、熔断没有 |
| `call` | purpose / model / latency_ms / finish_reason / usage / error | 哪类调用慢、谁截断、谁报错 |
| `tool` | name / status(ok\|rejected\|bad_json\|protocol_error\|unknown) / detail | 工具被拒/格式坏在哪一步 |

- 落盘失败**静默**（观测层不得影响游戏，继承 usage 纪律）；
- 跨进程追加复用 usage 的同一把文件锁（撕裂行教训）；
- **默认关闭**：`GAME_AGENT_TRACE` 为空时不建 recorder（零开销、零行为变化）。

### 2.2 挂点（`game_agent/llm.py`，均为纯增量）

- `complete_with_meta`：API 异常记 `error` 后**原样抛出**；正常记 latency/finish_reason/usage；
- `run_turn`：回合边界（含熔断时 `outcome=meltdown`）、每次 API 调用、每个工具执行的 status；
- `LLMClient.from_settings`：`settings.trace_path` 非空才接线。

### 2.3 演示（离线假客户端，1 轮拒绝 + 1 轮收尾）

```
turn_begin(turn_seq=1) → call(turn) → tool(change_stat, rejected, "数值越界…")
→ call(turn) → tool(submit_narration, ok) → turn_end(outcome=completed, iterations=2)
```

### 2.4 守卫

`tests/test_trace.py` **12 条**：recorder 全序/静默、端到端事件序列、
rejected/bad_json/unknown 三种 tool status、熔断留痕、complete 的 usage 与 error、
from_settings 开关与默认关闭、tracer=None 零开销。

---

## 3. B2 · 一键质量门 `scripts/qa_gate.py`

把 README「质量门四连」+ 深度检查收进一条命令、一张报告卡：

| 层 | 门禁 | 需要 API key |
| --- | --- | --- |
| L1（离线） | check-worldpack → 全量 pytest | 否 |
| L2（真机） | judge_sensitivity → worldpack_smoke | 是（缺 key 跳过并报数） |
| L3（深度） | longrun_probe（--turns 可调） | 是 |

- `--offline`：真机门禁改用 `--offline` 档（l2-judge 无法离线 → 不进计划，并打印提醒）；
- 报告：`reports/qa_gate_<ts>.json`（逐门 pass/skip/exit_code/耗时/stdout 尾部）；
- 退出码：全部已执行门禁通过 → 0；任一失败**或全部被跳过** → 1
  （"没测出来"≠"没问题"，与 judge 三态判定同纪律）；
- 子进程输出走**文件重定向**（非管道捕获），平台无关。

**实测（2026-09-24）**：`--pack world-packs/ancient_jianghu --levels l1` →
l1-check 1.4s ✓ · l1-pytest 29.9s ✓ · 报告 `reports/qa_gate_20260924-142427.json`。

**守卫**：`tests/test_qa_gate.py` **9 条**（计划构建/offline 语义/报告判过逻辑/CLI dry-run 冒烟）。

---

## 4. B3 · 侧信道路由补齐（config + llm）

- `Settings` 新增 `extract_model` / `reflect_model` / `dedup_model`
  （env：`DEEPSEEK_EXTRACT_MODEL` / `DEEPSEEK_REFLECT_MODEL` / `DEEPSEEK_DEDUP_MODEL`，
  空 = 回退主模型；`.env.example` 已同步文档）；
- `LLMClient.from_settings` 的 models 映射补齐五个侧信道 purpose；
- **这是 plan-local-14b §8.2「混合路由」的前置**：Gate 1 之后混跑本地 14B 侧信道
  只差"base_url 按 purpose 拆分 + Game 长局参数 env 化"（A7，本轮不做）。

**守卫**：`tests/test_usage.py` C1 段 +4（路由/回退/from_settings 接线/env 读取）。

---

## 5. 回归与留痕

- 全量：**565 → 590 passed**（+4 B3 / +12 B1 / +9 B2），1 warning（既有）；
- 引擎改动面：`config.py` / `llm.py`（挂点 + 路由）/ 新增 `trace.py`；
  `game_agent/` 其余零改动；`scripts/qa_gate.py` 为新增；
- 未碰：冻结评测集、语料、场景卡工厂、训练脚本与数据。

---

## 6. 下一步衔接

- **A7（Gate 1 后）**：purpose → (base_url, model) 完整路由 + Game 长局参数 env 化
  ——届时 trace 可直接观测"哪些调用落在本地、哪些落在云端"；
- **训练完成后的用法**（正式训练尚未完成，截至 2026-09-24）：训练跑完、adapter 落盘后，
  `GAME_AGENT_TRACE=reports/trace.jsonl` 打开，Gate 1 的三方对照（Qwen3-14B 基座 / +LoRA / flash）
  即可自动留下逐调用轨迹，失败回放不再靠猜；qa_gate 已把验收命令收敛为一条；
- **Track C（第二冻结窗口）**：语义检索 / 规划层 / 事实图 Judge 等契约变更类改进，
  仍按原纪律等 Gate 1 之后、Phase 2 数据采集之前打包进入。

---

## 7. 更正记录

- **2026-09-24**：初稿两处表述"与正在进行的 Phase 1 训练零冲突"与"Gate 1 三方对照（含 +LoRA）"
  暗示正式训练已在进行/已完成——**与事实不符**。核实本地仓库：`data/training/` 数据就绪、
  仅 100 步试跑报告（`train-debug-20260919.md`），**无 `runs/` 产物、无正式训练完成报告**。
  已改为"正式训练尚未完成（截至 2026-09-24）"，后续训练完成时更新本报告。

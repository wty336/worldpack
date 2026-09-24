# Runtime 平台化三期计划：记忆消解 / Run checkpoint+Replay / 观测与评估聚合

> 上游：`docs/Agent Runtime 工程化.md`（外部评审 8 条意见）与本会话的逐条对账结论。
> 结论：项目已站在"Long-Horizon Agent Runtime"定位上（六件 agent-first + Track B trace +
> 四连门禁），本期不做大重构，只补**边际价值最高的三件**。命名与门禁纪律继承
> `docs/plan-agent-first.md`（引擎改动 = 新实验 = 全量回归；评测集继续当尺子）。

---

## 0. 三件概览

| # | 件 | 解决什么 | 与现有资产的协同 | 引擎改动面 |
| --- | --- | --- | --- | --- |
| ① | **记忆时序冲突消解** | "Alice 恨 Bob → 原谅 Bob"不能 append 两条共存（design.md §8.2 明写的 v1 限制） | 与事实图 Judge 协同：被取代的旧事实不得再接地 confab 断言 | memory.py / state.py / factgraph.py |
| ② | **Run checkpoint + Replay** | "第 87 轮为什么这样"→ 在 turn N 重放、换 prompt/模型、diff | 存档中立格式 / trace / qa_gate 全部复用 | 新 runlog 模块 + 脚本（引擎主循环零改动） |
| ③ | **Trace 查看器 + 统一评估报告** | 还 Track B 欠账（trace_report 验收项未建）+ 指标聚合与 N 次跑对比 | trace.py / usage 账本 / reports/*.json | 纯 scripts 层，零引擎改动 |

执行顺序 ① → ② → ③（③ 随时可插队：零引擎风险）。

---

## 1. ① 记忆时序冲突消解

### 1.1 现状与缺口

- 写入路径：remember 提议 → 校验（长度/目标/importance）→ 去重（包含 + bigram 预筛 +
  LLM 语义）→ append。**无冲突检测**；
- 后果：旧事实与新事实并存注入 → 状态栏同时出现"沈清秋与你疏远"和"沈清秋与你亲近"，
  模型被两版都当真（与内轮自校正剥旧稿是同一个"版本污染"问题，但发生在记忆层）。

### 1.2 设计（写在写入路径上，读路径只做排除）

```
remember(fact) → 去重（现有）→ bigram 预筛出相关候选 → conflict 侧信道（temp=0，预算 500）
  → 判定三选一：无冲突 / 并存（不同维度的事实）/ 取代（新事实在时序上覆盖旧事实）
  → 取代时：旧条目打 superseded=True（保留在存储 = 溯源可查，不删除）
读取与消费三处排除 superseded：
  ① status_text / query_world 的 rank_facts 注入；
  ② factgraph.build_graph 的接地事实（被取代的旧事实不得再给 confab 断言接地——
     这正是"她已离开长安"之后再说"她住在长安"必判虚构的机制）；
  ③ 淘汰：满额时 superseded 条目优先淘汰（死权重最轻）。
```

- **判定放写入时**而非读取时：写入低频（remember 每次提议一次），读取每轮多次；
  写入时判定一次，读取时只是布尔过滤，零成本；
- **数据模型**：`MemoryEntry` 加 `superseded: bool = False`（缺省兼容旧档）；不加
  supersedes_by 指针——冲突判定输出即可支撑（v2 若要溯源再加）；
- **新侧信道** purpose="conflict"（回退主模型；预算常量入 budgets.py 单一真源）；
- **失败静默**：conflict 调用异常/空 → 视为"无冲突"（append 现状行为，绝不阻塞写入）。

### 1.3 验收

- 守卫（离线）：候选短名单生成、判定三态解析、superseded 的注入/接地/淘汰三处排除、
  存档回环（旧档缺字段回退 False）、factgraph 不接地被取代事实（含"取代后断言旧事实
  → 判虚构"的端到端用例）；
- 评测尺子：`eval-sets/conflict_cases.yaml`（30~40 对，标签 = 取代/并存/无冲突，
  构造 = 事实对 + 时序说明）+ `scripts/conflict_gate.py`（真机门禁，判准率门槛
  待第一批基线后定，参照 dedup 的 100% 护栏不适用——三类判定有真实歧义，先测基线）；
- 真机冒烟：写入路径改动 → ancient_jianghu 冒烟重跑。

### 1.4 待拍板

- 取代判定是否要求"新旧事实共享同一主体/属性维度"才触发（防"她原谅他"误取代
  "他欠你钱"）——conflict 提示词里显式要求，门禁语料含负例；
- superseded 条目是否永不注入（v1 决定：是，简单且与事实图一致；v2 可做"溯源性引用"）。

---

## 2. ② Run checkpoint + Replay

### 2.1 目标形态

```
Run #1024（run_id）
  turn 1..N：每回合落一条 runlog（玩家输入/状态快照/叙事/工具效果/判定/成本）
  ├── checkpoint：每回合全量状态快照（state JSON + history）
  ├── resume <turn>：从任意 checkpoint 继续交互
  ├── rollback <turn>：回滚到任意 checkpoint（state.copy 早已支持，包一层）
  └── replay <turn> [--prompt-patch f.yaml] [--model X]：重放该回合并 diff
      diff 维度：narration / choices / stat_changes / 判定 / tokens / latency
```

### 2.2 设计

- `game_agent/runlog.py`（新模块，引擎主循环**零改动**）：`RunRecorder` 挂 Game 的回调缝
  （on_text/`_llm_round` 之外？——不：**在 Game 外层包一层**，`record_turn(game, run_id)`
  由驱动脚本调用，回合结束后从 game.state/game.history 取快照落盘 JSONL）；
- 默认关闭：`GAME_AGENT_RUNLOG=<dir>` 非空才记录（开发/实验工具，不接受生产默认开销）；
- 成本口径：全量 checkpoint ≈ 每回合 state+history JSON（长局 10MB 级/局）——**接受**，
  它是实验工具不是生产存档；自动存档/崩溃存档机制不动；
- `scripts/replay.py`：
  - 读 runlog → 用 checkpoint(N-1) 重建 `GameState.from_dict` + history；
  - prompt 补丁 = 对系统提示词/工具 schema 做文本替换（yaml 补丁文件，替换前打印 diff
    确认），模型切换 = env 路由（`DEEPSEEK_MODEL`/`BASE_URL`，机制现成）；
  - 重放 turn N（同一玩家输入）→ 输出 diff 报告（JSON + 人读表格）；
- `scripts/resume.py`：checkpoint(N) → 继续交互（复用 play 的 REPL 但注入状态/history）。

### 2.3 验收

- 守卫（离线，FakeClient）：runlog 落盘可回放（同输入同 FakeClient 响应 → 结果一致）、
  checkpoint 重建后 state/history 与原件等价、rollback 回滚后 `state == checkpoint_state`、
  prompt 补丁仅影响系统消息（其余消息逐字不变）、diff 报告字段齐全；
- 真机演示：跑一局冒烟记录 runlog → `replay <turn>` 换模型/prompt → diff 报告落档
  （作为"Agent Experimentation Platform"的实证）。

### 2.4 待拍板

- runlog 与 trace 的关系：trace 是"过程事件流"、runlog 是"回合级状态线"——两者不合并
  （trace 无状态快照、runlog 无调用级时延），replay 报告里**引用 trace 的 seq 区间**关联；
- checkpoint 是否也存 stat_log/choice_log 全量（v1：是，直接随 state.to_dict 走）。

---

## 3. ③ Trace 查看器 + 统一评估报告

### 3.1 `scripts/trace_report.py`（还 Track B 欠账）

- 读 trace JSONL（+ 可选 usage JSONL）→ 聚合报告：
  - 总览：每回合一行（turn, iterations, latency, tokens, 工具状态, judge 判词, 结局）；
  - `--turn N`：展开该回合完整事件链（turn_begin → call → tool → turn_end），
    回答"第 87 轮为什么这样"；
  - 故障清单：meltdown/rejected/bad_json 事件 + 对应回合号；
  - 成本分用途表（复用 usage.summarize/cost_report 口径）。
- 纯离线、零引擎改动；守卫 = 合成 trace 样例 → 报告断言。

### 3.2 `scripts/eval_report.py`（统一评估报告卡）

- 聚合 `reports/` 既有产物（judge_sensitivity_* / injection_gate_* / qa_gate_* /
  usage-*.jsonl / trace）→ 一张 **Agent 报告卡**：
  拦截率（分家族）/ 误报率 / 注入泄露率 / 审计零偏差 / 通关结局 / 成本 / 时延 /
  Judge 未知率 / 卡壳触发数（从 trace/存档统计）；
- 判读纪律继承：样本量不足的指标打 ⚠（conclusive 口径复用 evalmeta）、
  未知不计分母、报告卡不通过不粉饰。

### 3.3 `scripts/run_matrix.py`（同任务 N 次跑对比）

- 驱动 worldpack_smoke 同包跑 N=5 次（seed 1..5）→ 指标表（通关/审计/禁表/结局/成本/时延）
  + 方差；
- 用途：换 prompt/模型/记忆策略后的 A/B（与 replay 的 diff 互补：replay 看单回合，
  run_matrix 看整局分布）；
- 预算：5 次冒烟 ≈ ¥0.5/包，超 ¥1.5 停（沿用工厂的成本门限风格）。

### 3.4 验收

- 守卫（离线）：trace 报告聚合逻辑、报告卡字段与判读口径、run_matrix 结果表结构
  （用假结果渲染，不真跑）；
- 真机：对既有 `saves/smoke-tb6` 的 usage 与既有报告跑一遍 trace_report + eval_report，
  产出第一张正式报告卡落档。

---

## 4. 纪律与风险

| 风险 | 对策 |
| --- | --- |
| ① 取代判定误伤（好事实被标 superseded） | 冲突语料含负例；判定提示词要求"同主体同维度 + 时序明确"；基线跑完再定门禁线 |
| ① 与训练数据契约冲突 | 记忆契约（fact 文本/注入格式）不变，只加字段与过滤——训练数据不受影响（训练暂停期，恢复时按新契约重渲染即可） |
| ② checkpoint 体积 | 实验工具定位，env 开关默认关；不并入生产存档路径 |
| ③ 报告卡"看起来全绿"自欺 | 判读口径复用 evalmeta（样本量/未知/CI），不达标显式 ✗ |
| 全程 | 每件落地 = 全量回归 + 该件真机验证；文档记录 prompt_version/模型/成本 |

---

## 5. 执行记录（滚动更新）

### 5.1 ① 记忆时序冲突消解 ✅（2026-09-24）

**落地**：

- `MemoryEntry.superseded: bool`（去 frozen 以支持写入时打标；存档回环兼容旧档）；
- `memory.py`：`CONFLICT_SYSTEM` + `parse_conflict`（三态）+ `judge_conflict`（纯函数，
  门禁复用）+ `_resolve_conflict`（写入时、bigram 短名单、失败静默不取代）；
- 三处排除：`rank_facts`（含常驻区）/ `factgraph.build_graph` 接地 / `_extract_facts`
  已有事实 + 满额淘汰**superseded 优先**；
- 尺子：`eval-sets/conflict_cases.yaml`（取代/并存/无冲突 各 8 条）+
  `scripts/conflict_gate.py`（基线期无硬门，3 轮多数票）。

**真机基线（flash，¥0.023，72 次判定）**：

| 轴 | 结果 | 判读 |
| --- | --- | --- |
| 行为轴（是否打 superseded） | **24/24 = 100%** | 取代 8/8 全抓；16 条非取代**零误杀**（误判全落在"并存→无冲突"安全向） |
| 标签轴（三态分类） | 19/24 = 79% | 并存 3/8 偏低——模型保守，两态合并为"不取代"，写路径无影响 |

结论：**行为相关精度 100%**；并存/无冲突的细分是纯判定质量问题，v2 可收紧
CONFLICT_SYSTEM（本期不追）。真机冒烟通过（exit 0，¥0.096）。

**守卫**：`tests/test_memory_conflict.py` **9 条** + `tests/test_conflict_gate.py` **3 条**。
全量回归：**653 → 665 passed**。

## 6. 与既有文档的关系

- 评审原文：`docs/Agent Runtime 工程化.md`（本计划是对其 8 条的取舍结论）；
- 被"不采纳/缓做"的评审条目：AgentRuntime 大重构（结构已在，只补循环图文档）、
  Tool Registry（等新工具需求再顺手做）、Context 预算控制器（32K 现状收益不显著）；
- 与训练恢复的衔接不变（`docs/plan-agent-first.md` §4）：恢复时按新契约重建数据。

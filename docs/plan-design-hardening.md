# 设计加固计划：证据面收口 / 校验闭环 / 平台化三步

> 背景：2026-09-24 对引擎全量代码的设计评审（会话记录）。结论：骨架不动——
> 真值原则、工具协议、分层压缩、不变量校验这些大决策都成立；本计划只处理
> **四个没收口的回路、两个表达力瓶颈、一个平台化短板**。
> 命名与门禁纪律继承 `docs/plan-agent-first.md`：引擎改动 = 全量回归；
> Judge 提示词改动 = 复跑 E1 灵敏度门禁。

---

## 0. 批次总览

| 批次 | 工作包 | 解决什么 | 改动面 | 状态 |
| --- | --- | --- | --- | --- |
| A | A1 事实图证据面 / A2 Judge 连贯性材料 / A3 长程反重复 / A4 卡壳重规划 / A5 熔断保守回合 | 判官"看不见"与"喊一嗓子就走"的一致性缺口 | factgraph / judge / game / cli / web | ✅ 已落地 |
| B | B1 factcheck 常开 / B2 反馈复查闭环 | 校验从开环变闭环、确定性层与 LLM 层分层 | game / judge | ✅ 已落地 |
| C | ToolRegistry + MCP 暴露 | 工具层硬编码 → 声明式注册表；接入生态 | llm / 新 registry / 新 mcp | 待排期 |
| D | 地点一等公民 | scene 自由字符串 → 声明式地点表 + 受校验的 change_scene | worldpack / llm / context / lore | 待排期 |
| E | 机制层表达力（counters / items） | flags 只有 bool → 计数器与持有物入真值 | worldpack / conditions / stats / state | 待排期 |
| F | 小项：token 校准闭环 / 复盘回写 | 预算估算用实测 usage 校准 | usage / compression | 待排期 |

执行顺序 A → B → C → D → E（F 随时可插队）。C/D/E 动 worldpack schema，
按守则同步更新 `docs/worldpack-manual.md` 与 `check-worldpack` 交叉校验。

---

## 1. 批次 A：证据面与一致性收口

### A1 事实图纳入压缩摘要与选择日志【设计洞，最优先】

**现状**：`factgraph.build_graph()` 的接地事实只取 player_facts、在场 NPC 记忆、
lore、NPC 身份、场景、数值（`factgraph.py:82`）。压缩摘要（`【剧情摘要】`消息）与
`choice_log` **不入图**。

**缺口（长局中后段 confab 误报路径）**：
早期剧情确立的事实 → 被 `_compress_history` 压进摘要 → 摘要不在图内 →
叙事引用该事实 → 全部锚点查表缺席 → 被判"虚构事实"。factcheck 抽取器明确把
"外部世界/第三方事件陈述"算断言（`FACTCHECK_SYSTEM`），而这类内容恰恰只活在
摘要与选择日志里。300 轮长局 Judge 零拦截的历史数据掩盖了这个洞——judge 是
抽样跑的，没抽到不等于不存在。

**设计**：
- `build_graph(pack, state, history=None)`：history 提供时，把当前摘要全文与
  `choice_log` 各条的选项文本追加进 facts（token 化照旧）；
- `compression.summary_text(history)`：复用 `locate_summary`，取出摘要正文；
- `JudgeSystem.check(..., history=None)` 透传；`game.py` 两个调用点
  （`_judge_turn` / `_critique_and_regenerate`）传 `self.history`。

**验收（守卫测试）**：
- 摘要内事实（"欠五十两，中秋前归还"只出现在摘要里）→ 引用它的断言**接地**，
  `check_graph` 返回 None；
- choice_log 选项文本同理接地；
- 真编造（图内外都没有）照旧拦截——既有用例不回退。

**风险与回退**：摘要文本入图会扩大 token 集 → 接地面变宽、缺席判定变保守
（漏报方向）。这正是期望的方向：证据面"该有的"必须有，宁可少拦不误拦。
回退 = 去掉 history 参数即可（调用方缺省 None 零影响）。

### A2 Judge 材料附上一轮叙事【design §10.2 第 4 条落地】

**现状**：`_judge_turn` / `_critique_and_regenerate` 给判官的材料 =
`status_text`（当前状态栏）。`design.md §10.2` 承诺的四类校验里，
"剧情连贯（与上一轮衔接是否自然、是否无视关键选择的结果）"**没有证据面**——
判官拿不到上一轮叙事，无从判连贯。

**设计**：
- `game._judge_materials()`：`status_text` + `<上一轮叙事>` 区块
  （取 `self.last_narration`，与反重复检测同源）；
- `JUDGE_SYSTEM` 的 ② 增补"或与上一轮叙事直接矛盾（前后不一致）"——
  不新增第 4 类检查（避免 E1 语料分类口径变化），只给 ② 补证据。

**验收**：离线断言判官调用消息含 `<上一轮叙事>` 区块；首轮（无上文）不附。
**门禁**：JUDGE_SYSTEM 变更 → 真机复跑 `scripts/judge_sensitivity.py`
（¥0.1~0.3），三类拦截率与误报率不回退才算过。

### A3 长程反重复（滑动窗口 n-gram）

**现状**：`_check_repetition` 只比较**相邻**两轮（difflib 相似度 0.6）。
隔十轮重复同一桥段的"长程复读机"（试玩反馈 #3/#4 的变体）检测不到。

**设计**：
- 维护 `self._narration_window`（最近 6 条整轮叙事，进程内、不落盘——
  与 `last_narration` 同生命周期，读档重置可接受）；
- 字符 6-gram 集合的**覆盖率**（当前叙事中被窗口内旧叙事覆盖的 gram 比例）
  ≥ 0.25 → 注入"与更早回合重复"的反重复提示；
- 相邻轮 difflib 命中时跳过长程检查（一轮最多一条提示）。

**验收**：构造与 3 轮前叙事大量重叠的叙事 → 提示注入；无关叙事 → 不注入；
相邻重复照旧走原路径。

### A4 卡壳时重规划（plan replan）

**现状**：计划只在节点进入时生成一次（`_ensure_plan`），此后不更新；
卡壳保护（30 轮推进提示）与计划层互不知情——计划可能早已偏离实际剧情，
提示还在按旧计划展示。

**设计**：`end_turn` 注入【推进提示】的回合，清空 `node_plan` 并强制重规划
（快照刷新到当前 flags、指针归零）。失败静默降级 = 无计划（与首生成同口径）。

**验收**：注入推进提示的回合后，`node_plan` 被新计划替换（fake 侧信道可测）；
无 goal 节点不触发。

### A5 熔断保守回合（design §10.3 落地）

**现状**：`LLMTurnError` 从 `_narrate` 一路抛出到 CLI/Web——CLI 崩溃存档退出
（`cli.py:234`），Web 报 error 事件。design §10.3 承诺的是"停止**本轮**生成，
输出保守文案 + 重新给选项"——会话不该死。

**设计**：`_llm_round` 捕获 `LLMTurnError`（来自 `_generate_turn`/自校正）→
返回保守 `TurnView`（中性引擎文案 + 沿用上轮选项兜底），历史追加熔断标记。
熔断时 `run_turn` 内部消息未同步进 history（同步只在成功返回后发生），
所以状态天然干净，无半轮污染。CLI/Web 的异常处理保留为外层保险。

**F1 纪律**：保守文案是引擎中性文案（"本轮生成失败，已跳过"），
不含世界包内容；世界包想自定义降级文案是后续可选项（world.yaml 可选字段）。

**验收**：假客户端三次协议失败 → `game.say()` 不抛异常，返回保守视图，
history 含熔断标记，后续回合照常。

---

## 2. 批次 B：校验闭环

### B1 factcheck 常开（确定性层每轮，LLM 层采样）

**现状**：缺席证据检查（`check_graph`：一次 extract 调用 + 纯代码查表）藏在
`JudgeSystem.check` 里，跟着 `judge_every`（默认 5）降频跑。**确定性、便宜的
检查被 LLM 判定的采样节奏绑架**。

**设计**：
- `Game(factcheck_every=...)`：>0 时每 N 回合独立跑一次缺席检查
  （不经过 LLM judge）；judge 采样轮跳过独立检查（避免同轮两次 extract）；
- 违规 → 与 `_judge_turn` 同格式的【校验反馈】注入 + 进入 B2 复查队列；
- CLI/Web 装配 `factcheck_every=1`（常开）；
- 分层口径成型：**确定性层（factgraph）每轮常开，LLM 层（judge）降频采样**。

**成本**：+1 次 extract 调用/回合（输入仅本轮叙事 + 提示词，输出 ≤500 token；
侧信道走专属档位/关思考）。qa_gate 真机冒烟复核成本增幅。

### B2 校验反馈复查闭环（fire-and-forget → 一次复查）

**现状**：`_judge_turn` 注入【校验反馈】后从不复查——模型是否真的修正了，
引擎既不知道也不跟进。对比关键节点内轮自校正（critique → 重生成 → 再判定）
是闭环，日常轮只做了"喊一嗓子"。

**设计**：
- 注入反馈时记 `self._pending_verdict`（进程内、不落盘——读档后退化为开环，
  与改前行为一致，无正确性影响）；
- 下一轮生成后，先跑一次**聚焦复查**（`judge.recheck_feedback`：只问
  "上轮反馈的问题是否已自然修正"，temp 0，purpose=judge）；
- 复查"已修正" → 清除队列；"仍有问题" → 注入一次升级反馈
  （【校验反馈·仍未修正】，要求正面处理），**不再连环**（至多一次升级）；
  复查未知/失败 → 清除队列不升级（与三态纪律同口径：未知 ≠ 通过，但也不误伤）。

**验收**：反馈注入 → 下一轮出现复查调用且携带原 verdict（fake 断言消息内容）；
复查通过无新消息；复查未通过出现升级反馈且不会再触发第三次复查。

---

## 3. 批次 C：ToolRegistry + MCP 暴露【待排期】

**现状**：四个工具写死在 `llm.build_tools()`（schema）+ `run_turn` 的
if/elif 分发（`llm.py:539-610`）。每加一个工具改引擎三处；世界包无法扩展
工具；"Runtime"定位下工具层不可编程。

**设计**：
```
ToolSpec = {name, schema, handler(args, state) -> str, purpose_tag, worldpack_scope?}
ToolRegistry：register / build_schemas / dispatch(name, args)
  - 引擎四件套注册为内置工具（行为不变，纯搬运）；
  - 世界包可注册自定义工具（schedule.yaml 申明式效果型工具先行：
    {id, label, 参数 schema 由效果推导, requires 门槛, effects}——
    修炼包 breakthrough、都市包 hack 这类"领域动作"不再硬编码）；
  - MCP server（stdio）把 registry 暴露出去：tools/list ← build_schemas，
    tools/call ← dispatch。外部 IDE/客户端可以直接玩这个游戏。
```
**守卫**：引擎四件套行为不变（全量协议测试原样通过）；世界包工具过
`check-worldpack` 交叉校验（效果引用合法性，复用现有校验器）；
MCP 层薄封装不碰状态（复用 query_world 的只读纪律测试模式）。
**验收**：新世界包仅靠 YAML 声明一个自定义工具并真机调用成功；
`scripts/qa_gate.py` 全绿。

---

## 4. 批次 D：地点一等公民【待排期】

**现状**：`state.scene` 是自由字符串；`ActionSpec.scene/present` 与节点
`on_enter.scene` 能写它，但世界包不声明地点表——LLM 叙事里的移动与引擎
真值各说各话，lore 触发跟着字符串匹配走，状态栏可能展示过期地点。

**设计**：
- `world.yaml` 可选声明 `locations: [{id, name, keys, present?}]`；
- `change_scene(location, reason)` 工具提议：LLM 只能提议**声明表内**的地点，
  引擎校验后写入（与 change_stat 同构：参数作 checklist、白名单、审计入
  stat_log 同款日志）；未声明地点的世界包行为不变（scene 仍由行动/节点写入）；
- lore 触发与 `<scene>` 卡改挂 location id（未声明时回退现行字符串匹配）；
- `check-worldpack` 交叉校验：行动/节点引用的 scene 必须在地点表内。
**守卫**：提议表外地点 → 拒绝；叙事宣称移动但真值未变 → 状态栏仍显示旧地点
（一致性由状态栏真值兜底）。

---

## 5. 批次 E：机制层表达力——counters / items【待排期】

**现状**：机制真值 = bool flags + 数值 + 好感。没有计数器（"送过 3 次花"）、
没有持有物（"是否还有听雨剑"）、没有关系阶段。记忆层很丰富而机制层很薄：
叙事说"你把剑当了"，引擎无感，后续矛盾只能靠 judge 兜。

**设计**：
- `counters: {name: {label, initial, min, max}}`：int 真值；effects 支持
  `{counters: {flower_gifts: +1}}`；条件 DSL 加 `{counter: {name: {gte: 3}}}`；
- `items: [{id, label}]`：集合真值（拥有/失去）；effects 支持
  `{items: {gain: [sword_listening_rain], lose: []}}`；条件 DSL 加
  `{items: {sword_listening_rain: true}}`；
- 加载期可达性校验扩展到 counters/items 路径（沿用 flag 可达性校验器）；
- 事实图把 counters/items 值入图（叙事引用"还有五十两欠款"可接地）。
**验收**：一个试点世界包用 counters 写"三次赠礼触发支线"、items 写
"当剑后不可再用剑法"（requires 门槛），全量门禁通过。

---

## 6. 批次 F：小项【随时插队】

- **token 校准闭环**：`est_tokens`（1 字 ≈ 1 token）偏粗；usage.jsonl 已有
  每调真实 prompt_tokens——按消息类型维护 EMA 校正因子，压缩触发线用校准值
  （观测进 trace，阈值行为不变，只修精度）；
- **README/复盘回写**：批次 A/B 落地后按惯例补 README 当前状态一行与本文件
  状态列；真机数据（E1 复跑结果、qa_gate 报告）回写本文件附录。

---

## 7. 不做清单（评审结论，防止过度投入）

- **每 NPC 独立 sub-agent**：成本涨、质量未必涨；单叙述者 + 角色卡是成熟做法；
- **embedding 重写检索**：≤24 条事实/≤20 条记忆的规模下 BM25 + 可插拔接缝
  是正确取舍（resume-entry Q4 口径不变）；
- **事件 DAG / 多相位事件**：现有世界包用不上，flag 机制够表达；
- **异步化 / 分布式**：单进程边界诚实标注即可（web 每会话锁已落地）。

---

## 8. 门禁与执行纪律

- 每批次：全量离线回归（`uv run pytest`）必须全绿再进下一批；
- A2 动了 JUDGE_SYSTEM → 真机复跑 `scripts/judge_sensitivity.py --pack world-packs/ancient_jianghu`
  （对抗拦截率三类 100%、正常误报 ≤8% 为过线口径，以复跑实测为准）；
- B1 改变每回合调用结构 → 真机冒烟复核成本增幅（对照 `docs/p1-report.md`
  单局 ¥2~4 基线）；
- 引擎改动守卫测试先行：每工作包的验收条目即测试清单，先写测试再写实现。

---

## 9. 落地记录（2026-09-25）

### 批次 A/B 引擎改动（全部完成）

| 文件 | 改动 |
| --- | --- |
| `compression.py` | +`summary_text()`（A1：取当前摘要正文） |
| `factgraph.py` | `build_graph(pack, state, history=None)`：history 提供时纳入摘要与 choice_log（A1） |
| `judge.py` | `check(..., history=None)` 透传；`JUDGE_SYSTEM` ② 补"与上一轮叙事直接矛盾"（A2）；+`recheck_feedback()` 三态复查（B2） |
| `game.py` | `_judge_materials()`（A2）；`_check_repetition` 双层化 + n-gram 窗口（A3）；`_maybe_replan`（A4）；`_meltdown_fallback` 保守回合（A5）；`_factcheck_turn` + `factcheck_every`（B1）；`_inject_feedback`/`_recheck_feedback` 复查闭环（B2）；`factcheck_every` 装配 CLI/Web =1 |
| `cli.py` / `web.py` | `factcheck_every=1`（确定性层每轮常开，judge 保持每 5 轮采样） |

### 离线回归

- 新增 `tests/test_design_hardening.py` 21 项守卫（A1×4 / A2×3 / A3×4 / A4×2 / A5×2 / B1×3 / B2×3）；
- 全量 `uv run pytest`：**698 passed**（677 → 698，零回退）。

### E1 灵敏度门禁（A2 提示词变更后真机复跑）

`reports/judge_sensitivity_20260925-001519.json`，语料 110 条 × 3 轮，主/Judge 均 deepseek-v4-flash：

| 类别 | 结果 | 与变更前基线对比 |
| --- | --- | --- |
| OOC 拦截 | 20/20 = 100% | 持平 |
| 设定矛盾拦截 | 22/22 = 100% | 持平 |
| 虚构事实拦截 | 20/20 = 100% | 持平 |
| 正常误报 | 3/48 = 6% | 持平 |

成本 ¥0.312（谷段）。**结论：A2 的证据面增补未引起判据漂移，门禁通过。**
附注：本次复跑中 factcheck 用途调用 330 次（判官内附带的图检查随语料 ×3 轮）——
事实图缺席检查已在门禁链路中生效。

### 真机冒烟（B1 调用结构变更复核）

`ancient_jianghu` seed=11 七日冒烟（saves/smoke-ancient_jianghu.txt）：

- 全程无熔断无协议重试；数值零偏差审计通过（12 条变更）、禁表扫描通过；
- `factcheck` 常开生效：4 次调用 / 1.8K 输入 / ¥0.002——**确定性层常开的成本增幅
  可忽略**（整局 ¥0.083，turn 缓存命中 96.7%）；
- 复查闭环、重规划（plan ×2）、conflict/dedup/extract/reflect 均按设计触发。

### 遗留与后续

- 批次 C（ToolRegistry + MCP）/ D（地点一等公民）/ E（counters/items）/ F（token 校准）
  按本文件 §0 顺序待排期；C 与 MCP 合并做；
- 已知口径：`purpose="factcheck"` / `"plan"` 未在 Settings 建独立模型档位
  （回退主模型）——沿用 factgraph 原状，若要分层路由在批次 F 一并补。

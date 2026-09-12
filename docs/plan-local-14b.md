# 本地 14B 对标 DeepSeek V4 Flash 的可行性实验设计（①→③）

> 一句话：不预设"能/不能"，把引擎的 6 个 LLM 调用点逐一对照；先零样本、后蒸馏，
> 用五轴指标 + 三道决策门收口，最终落到可玩的本地形态（离线/隐私动机）。
> 本文档 = 计划 + 执行复盘（滚动更新），复盘见 §10。

---

## 1. 背景与命题

### 1.1 动机

现游戏全程依赖 deepseek-v4-flash API（`config.py` 默认）。目标场景是**离线 / 隐私可玩**：
本地 GPU 起 14B 级模型，不经网络跑完整游戏。因此这不是"侧信道省 API 费"的课题，而是
**主回合生成也必须本地化**的课题——离线模式下 turn / judge / extract / compress / reflect / dedup
全部落在本地模型上。

### 1.2 命题拆分（避免用一个模糊的"效果"下结论）

- **P1**（判定轴）：本地 14B 经微调后，在 judge / extract / dedup / reflect / compress 上的
  输出是否与 flash 达成一致（判定一致率、拦截率、事实保全）？
- **P2**（协议轴）：本地 14B 经微调后，主回合能否以可接受的**协议一次通过率**产出合法回合
  （JSON 合法、枚举正确、3~5 choices、`submit_narration` 收尾、≤2 次协议重试）？
- **P3**（质量轴）：在通过 P2 的前提下，其叙事质量（文风、OOC、设定一致、长局连贯）是否
  接近 flash —— 用盲评偏好率衡量，不做"绝对达标"断言。
- **P4**（资源轴）：本地化的每 100 回合延迟 / 显存 / 摊销成本是否可玩（对照 flash API）。

三段式路线：**Phase 0 零样本对照（不训练）→ Phase 1 窄模块蒸馏 → Phase 2 主回合蒸馏试点 →**
**Phase 3 落地形态**。每阶段有独立报告与决策门，允许中途收敛为"混合/降级"结论。

---

## 2. 现状盘点（全部实测）

### 2.1 六个 LLM 调用点与真实 token 分布

来源：`saves/usage-*.jsonl` 全量解析（1025 次 turn 调用，混合压缩启用前后各时期 run）。

| purpose | 调用点 | 次数 | 输入中位 | 输入 p95 | 输入最大 | 输出中位 | 输出空间 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| turn | 主回合（game loop） | 1025 | **55,552** | 282,488 | 329,348 | 810 | 宽：中文创作 + 3 工具 JSON |
| aux | 杂项补全 | 51 | 7,804 | 9,846 | 9,850 | 157 | — |
| compress | 剧情摘要（game.py `_compress_history`） | 11 | 9,218 | 11,041 | 11,041 | 1,526 | 中：≤800 字结构化摘要 |
| extract | 事实提炼（game.py，每 2 回合） | 333 | 1,202 | 2,338 | 3,688 | 284 | 窄：`重要性|事实` |
| dedup | 语义去重（memory.py，bigram 预筛后） | 251 | 442 | 1,155 | 1,224 | 50 | 二值：重复/不重复 |
| judge | 语义校验（game.py，每 5 回合） | 34 | 866 | 985 | 1,006 | 500 | 窄：`通过`/问题描述 |

**读法**：
- 输入 token 的绝对大头在 **turn**（本表混合口径中位 5.5 万；**生产实测中位 2.93 万**，见下条）；
  其余调用全是"千 token 级"轻量调用；
- 上表是混合时代的汇总（含 M2b 压缩上线前的 300 轮无压缩 run，p95/最大即出自那批）。
  **当前生产配置**（cli.py：`compress_threshold=30000, keep_turns=6, extract_every=2,
  judge_every=5`；web 另加 `reflect_every=10`）下的分布**已于 2026-09-11 重测**（复盘 §2.2）：
  turn 输入中位 **29,267**（p95 26.8 万 / max 32.9 万仍出自压缩前那批）——**2.9 万是"32K 小窗可行"
  的关键实测证据，也是 §3 先验 1 被修正的依据**（usage 记账已内建，零成本）；
- turn 输出中位 810 token、上限 2048（`MAX_OUTPUT_TOKENS`）——对 14B 本地推理意味着
  TPOT×2048 的尾延迟预算要按最坏情况设计。

### 2.2 引擎已具备的接缝（实验不需要等改造）

1. **模型可路由**：`Settings.model_for(purpose)` 已支持 judge/compress 专属模型
   （`DEEPSEEK_JUDGE_MODEL` / `DEEPSEEK_COMPRESS_MODEL`，C1/P1）；extract/reflect/dedup
   目前回退主模型（Phase 3 若混合需小改造，见 §8.2）。
2. **端点可换**：`DEEPSEEK_BASE_URL` + `DEEPSEEK_MODEL` 指向任意 OpenAI 兼容端点
   （config.py 注释明确"可换任何兼容代理"）——本地 vLLM 起服务后改环境变量即可全量切换，
   **引擎零改动**跑 Phase 0。
3. **Game 长局参数可调**（game.py `Game.__init__`）：`compress_threshold / keep_turns /
   extract_every / judge_every / reflect_every`——"小窗 profile"（为 14B 收紧上下文）
   是纯参数调整，不需要架构改动；压缩机制（增量摘要 + 状态栏 + 近窗）本就是为小窗模型设计。
   注意：CLI/Web 目前把值写死在 `Game(...)` 调用里（`cli.py:79-80`：`extract_every=2,
   compress_threshold=30000, judge_every=5, reflect_every=10`；`keep_turns` 用 Game 缺省 6），
   运行期切换小窗档需走独立实验脚本或临时改常量（附录 D.5）。
4. **usage 记账**（C2）与 **audit** 内建，每次实验自动留痕与成本核算。

### 2.3 可复用评测资产（关键：多数门禁是现成的）

| 资产 | 位置 | 测量什么 | 离线? |
| --- | --- | --- | --- |
| Judge 对抗语料 + 期望判定 | 三包 `judge_corpus.yaml`（30/9/12 条） | OOC/设定矛盾/虚构拦截率 | 是（语料离线，跑判官需 API/本地） |
| P0 灵敏度基线报告 | `reports/judge_sensitivity_2026090*.json` + `judge-*.log` | flash 拦截 100% / 误报 0% | 是 |
| 灵敏度脚本 | `scripts/judge_sensitivity.py --pack <包>` | 分类拦截率 + 误报率（门禁退出码） | 需模型 |
| 协议合规离线统计 | `saves/*.json` 的 `history` 内含 engine `【引擎提示】`/`【协议错误】` 消息 | 可从既有轨迹直接数出各模型时代协议失败率 | 是 |
| 轨道事实检查 | `scripts/railed_fact_check.py`（轨道事实 3/3） | 长局事实保持 | 需模型 |
| 记忆回归 | `scripts/memory_regression.py`、`saves/memory-regression-report*.md` | 期望事实召回 | 需模型 |
| 禁表/OOC 冒烟 | `scripts/ooc_check.py`、`injection_test.py`、`condition_event_smoke.py`、`lore_smoke.py`、`reflect_smoke.py`、`dedup_test.py` | 各机制的专项真机门禁 | 需模型 |
| 通关冒烟 | `scripts/xianxia_smoke.py` / `urban_smoke.py` / `autoplay.py` | 整局可玩 + 审计零偏差 | 需模型 |
| 状态/审计单测 | `tests/`（217/224/233 全绿历史） | 引擎逻辑不随模型变（回归护栏） | 是 |

### 2.4 轨迹语料盘点（蒸馏与重放的数据源）

`saves/` 里含 `history` 完整消息轨迹的存档（`role=user/assistant/tool`，engine 元消息带
`name=engine` 打标；assistant 消息含 `content`+`tool_calls`，无 reasoning_content）：

| 文件 | 内容 | 规模量级 |
| --- | --- | --- |
| `memory-regression-checkpoint.json` | 300 轮长局（压缩前时代） | 最大，1.3 MB |
| `baseline-checkpoint.json` | 基线长局 | 1.0 MB |
| `condition-xianxia_wendao.json` / `condition-urban_neon.json` | 条件事件验收 run | 59/77 条消息/包 |
| `xianxia-smoke.json` / `urban-smoke.json` / `web-smoke.json` | 真机通关 run | 中 |
| `autosave-8f517e3573f8.json` | 存档 | 小 |
| 早期档（如 `playthrough-together.json`） | 无 history 字段（旧格式） | 不可作轨迹源 |

> 数据资产结论：**足够支撑 Phase 0 采样与 Phase 2 训练试点，但不够支撑大规模训练**——
> 单回合上下文输入 5 万级 token，纯靠这些轨迹可清洗出约 1000~2000 个干净回合对；
> 需要更多时用 flash 补标（补标成本很低，§6.2）。

---

## 3. 先验判断与关键风险（写在实验前，防止事后合理化）

1. **上下文是第一道门槛**：turn 输入中位 5.5 万 token。14B 原生 32K 窗的模型**直接不可用**
   （不是质量问题，是放不下）。候选必须 ≥64K 外推/原生窗（如 Qwen3-14B、Qwen2.5-14B 长窗变体，
   附录 A 确认清单）。压缩阈值 30000 字符 + `keep_turns=6` 是当前上限，小窗 profile 还有下调空间。
   > **实测修正（Phase 0 后补记，先验原文保留以备对账）**：本先验由 55.5K 混合口径推得，偏悲观——
   > 压缩生效后 turn 输入中位 **29K**（§2.1 重测），小窗 profile（`compress_threshold=20000 ·
   > keep_turns=4`）再压到 ≈**20-24K**，**Qwen2.5-14B 的 32K 窗实测够用**：小窗档两包通关、
   > ICR v2 的 60 个前缀最大 22K 全在窗内。结论以实测为准，不再要求 ≥64K 窗。
2. **蒸馏天花板**：微调 14B 逼近的是 teacher（flash）在**游戏内分布**上的输出；分布外
   （新世界包、新玩法模块）会回落。验收必须含"剔除包"泛化测试（建议 `urban_neon` 任一阶段
   从训练集剔除做泛化包）。
3. **质量轴没有离线 ground truth**：叙事好坏只能靠盲评（人类 + flash-referee）近似。
   盲评协议要防两类偏差：referee 对自族模型的偏袒（用交换位置 + 多次抽样）、人类样本量过小
   （每对 ≥3 人 × ≥20 对才有区分度）。
4. **本地推理成本不可忽略**：turn 输出 2048 上限 → 尾延迟预算 = TPOT × 2048；长窗 KV 是显存
   大头（5 万 token 输入的 KV，bs=1 也要 ~20-30 GB @bf16 视模型而定）；必须开 vLLM prefix
   caching（本引擎静态前缀稳定，命中友好，usage 里 `cache_hit` 中位 5.5 万已证明这一点）。
5. **数据污染风险**：轨迹里含协议失败重试段、`finish_reason=length` 截断段、被 `clean_narration`
   清洗的脏文本段——直接入训练集会学到坏行为。清洗规则见 §7.2，评测与训练同源要划保留集。
6. **DeepSeek 官方 R1-Distill-14B 不能作为"14B 可行"的直接证据**：那是数学/代码推理谱系；
   R1-Distill 不是中文创意写作 + 工具协议的合适起点，也未见有对应任务谱的官方 14B 蒸馏版。

---

## 4. 五轴指标总表（所有结论最终都落在这五轴上）

| 轴 | 指标 | 测量方法 | 数据来源 | flash 基线 | 目标线 |
| --- | --- | --- | --- | --- | --- |
| A 协议合规 | 协议一次通过率（iterations=1 占比）、熔断率、参数非法率、脏文本率 | TurnResult.iterations + history 内 engine 提示计数（可离线统计） | 既有轨迹（基线）/ 新 run | 待 Phase 0 实测 | 一次通过率 ≥ flash − 5pp；熔断 0 |
| B 判定一致 | judge 拦截率/误报率；与 flash 判定一致率；extract 事实一致率；dedup 二值一致率 | `judge_sensitivity.py --pack` 三包；ICR 双模型同输入对拍 | 三包 corpus + 期望判定 | 拦截 100%/误报 0%（P0） | 一致率 ≥98%（temp=0，多数票） |
| C 事实保全 | 轨道事实 3/3；期望事实召回；审计零偏差；跨包不泄漏 | `railed_fact_check.py`、`memory_regression.py`、audit | saves/基线 + 新 run | 3/3 | 3/3、零偏差 |
| D 叙事质量 | 盲评偏好率（14B vs flash，人类 + flash-referee）；禁表泄漏扫描 | 配对盲评协议（§7.4） | ICR 回合对采样 | 50%（平手） | ≥40% 视为"接近"，<40% 判不达标 |
| E 资源经济 | 单回合延迟、显存占用、每 100 回合摊销成本、上下文增长曲线 | vLLM 日志 + usage 记账 + GPU 实测 | 新 run | flash API 时延/成本 | 可玩（延迟无硬标准，记录备案） |

> A/B/C 是硬指标（可自动判），D 是软指标（需要评审），E 决定"可玩性"而非"质量"。
> 任何模型改动（prompt/温度/工具 schema）都算新实验，必须全套重测——继承仓库"修复即回归"纪律。

---

## 5. Phase 0 · 零样本对照（不训练，约 2~3 天，先做）

### 5.1 目的

花最小代价拿到"14B 裸模型按调用点的差距清单"，回答两个问题：
Q0a 差距是否小到根本不需要微调（judge/dedup/extract 很可能）；Q0b 主回合差距有多大，
是否值得进 Phase 2（微调主回合的投入最大，先用零样本决定去留）。

### 5.2 环境与接入

- 服务：vLLM（OpenAI server 兼容模式）。**验证门槛一张 24 GB 卡（如 4090）即够**：
  AWQ/GPTQ 4-bit + KV fp8 + 32K 小窗 profile 可跑 Phase 0 全部门禁（显存/速度预算与
  启动命令见附录 D）；候选先只上 **Qwen2.5-14B-Instruct(-AWQ)**——无 thinking 协议、
  工具调用稳定；Qwen3-14B（默认带 thinking，需显式关闭）与长窗变体留到多卡/云对照阶段
  （附录 A）。
- 接入：**不改引擎**。独立 env（如 `.env.local14b`，load_dotenv 读默认 `.env`——用一个包装
  环境变量覆盖方式执行：进程内 `DEEPSEEK_BASE_URL=http://<host>:8000/v1 DEEPSEEK_MODEL=<本地名>`）。
  所有门禁脚本与 `python -m game_agent play/web` 走原入口。
- 纪律：flash 对照 run 与 14B run 用**同一套命令与轮数**，分别落 `reports/` 与 usage 文件。

### 5.3 跑法矩阵

| 步骤 | 命令（示意） | 产出 |
| --- | --- | --- |
| 1. 判官门禁三包 | `scripts/judge_sensitivity.py --pack world-packs/{ancient_jianghu,xianxia_wendao,urban_neon}` | 三包拦截/误报 vs P0 基线 |
| 2. 专项冒烟 | ooc_check / injection_test / condition_event_smoke / lore_smoke / reflect_smoke / dedup_test | 各机制通过/失败清单 |
| 3. ICR 对拍采样 | 从 §2.4 轨迹每包取 ~30 个回合前缀，同一前缀分别喂两模型产一轮（脚本见 §7.4 的数据侧） | 逐对 A 轴自动统计 + D 轴样本池 |
| 4. 短长局 run | `autoplay.py` 或手玩 50~100 回合/包 | usage 分布重测（当前配置）+ C 轴抽查 |
| 5. 当前配置用量重测 | 解析新 usage jsonl | turn prompt 真实分布（决定模型窗位与 profile） |

### 5.4 决策门 0

- 若 **B 轴全部达标**（与 flash 一致率 ≥98%，或 corpus 拦截率不低于 P0 基线减 5pp）→ 窄模块
  （judge/extract/dedup/reflect/compress）本地化**不需要蒸馏或仅需小补强**，直接看 §8；
- 若 **A 轴达标且 D 轴 ≥40%**（极不可能，但别排除 14B 中文写作的惊喜）→ 直接跳 Phase 3；
- 否则（预期路径）→ Phase 1 先蒸馏窄模块，Phase 2 再决定主回合去留。

---

## 6. Phase 1 · 窄模块蒸馏（judge / extract / dedup / reflect / compress，5~8 天）

### 6.1 为什么窄模块值得先做

- 输入小（全部 <10K token）、输出空间窄、失败静默降级、有离线期望值——蒸馏性价比最高；
- 窄模块本地化后，即使主回合仍用 flash（在线模式），每回合也可少 1~2 次远程调用（judge 每 5 回合
  1 次、extract 每 2 回合 1 次、dedup 按需）——**离线场景下这些调用本来就必须本地**。

### 6.2 训练数据构造

| 模块 | 训练对构造 | 来源 | 规模预算 | 目标格式 |
| --- | --- | --- | --- | --- |
| judge | 材料 + 叙事 → 判定 | 三包 corpus 正反用例（全量，51 条）+ 由 flash 在真实回合前缀上补标 | 500~1500 | `通过` / `问题类型：描述` |
| extract | 回合窗（提炼素材+已有事实）→ `重要性|事实` 行 | 轨迹 history 切窗，flash 补标；保留集用 memory-regression 期望事实 | 600~1500 | 行式事实（parse_facts 同款） |
| dedup | 候选事实 + 既有事实 → 重复/不重复 | 轨迹内真实写入对 + 规则化近义改写合成 | 300~800 | 二值 |
| reflect | NPC 记忆列表 → 洞察行 | 需要 ≥8 条记忆的段（真机 run 触发；reflect_smoke 有现成材料） | 200~500 | `洞察|来源编号` |
| compress | 旧摘要 + 新增历史 → 合并摘要 | 存档内摘要前后对（compress 调用少，需 flash 补标） | 200~500 | ≤800 字结构化 Markdown |

- 所有补标调用 temp=0（判定类可复现，继承 E1 纪律）；usage 记账落盘，成本估算：5K 调用 ×
  平均 ~3K token 输入 ≈ 1500 万 token ≈ 数十元级（flash 空闲价 ¥1.5/M 未命中）。
- 清洗规则：删除失败/截断段；dedup 负例不要全用"明显重复"——按真实分布混入易混淆对。

### 6.3 训练与验收

- LoRA/QLoRA 起步（14B 单 LoRA 24G 可训；多卡可并行打五个模块或合并数据一次训练多任务，
  建议**合并单模型多任务**——共享底座、一次部署）；保留集与评测隔离（每包划 held-out）。
- 验收 = Phase 0 同一套门禁复跑 + 新指标"与 flash 判定一致率"（ICR 对拍，temp=0 多数票）。
- 决策门 1：B 轴达标 → 进入 §8 落地窄模块；不达标 → 检查数据质量（先怀疑清洗/标注，
  再怀疑容量），一轮回炉后仍不达标则砍掉该模块的本地化（flash 兜底），不阻塞主线。

---

## 7. Phase 2 · 主回合蒸馏试点（决策门 0/1 通过后，1~2 周）

### 7.1 命题再确认

主回合是"分布上逼近 flash"最难也最贵的一步。Phase 0 的 A 轴零样本数据是硬门槛：
**若裸 14B 连协议都学不会（一次通过率远低于 flash），先别训——协议遵从是 SFT 最容易
学会的东西，连它都学不会说明数据/模型接入有问题**，先排查再烧钱。

### 7.2 训练数据构造（回合级 SFT 对）

- **输入**：回合起点（玩家 user 消息）之前的完整 history 快照，含 engine 元消息（状态栏、
  摘要、【节点完成】等）与 tool 轨迹——即 `LLMClient.run_turn` 收到的原始 `messages`；
- **目标**：该回合最终成功的 assistant 工具调用序列 + `submit_narration`（含 narration 文本），
  **不含 reasoning_content、不含失败重试段**；
- **切分**：按回合边界把长轨迹切成窗口，窗口 = 压缩前配置的可见历史（近窗 + 摘要 + 状态栏），
  与生产输入分布对齐；
- **清洗黑名单**：含 `_protocol_fail`/`[协议错误]`/`[引擎拒绝]` 的段、`finish_reason=length`
  截断段、`clean_narration` 动过的脏文本、空 narration、choices 数不符段——先删后查重；
- **规模与成本**：§2.4 轨迹可清洗 ~1000~2000 对；不够则由 flash 在脚本化策略玩家
  （autoplay 同款）产生的新 run 上补标。**输入是 5 万 token 级，成本大头在这里**——1,000 对 ×
  平均 4 万输入 token ≈ 4,000 万 token ≈ ¥60（未命中价）量级，预算上限设 ¥300 前先算清楚。

### 7.3 训练与防过拟合

- LoRA 起步（效果不够再全参）；epoch 数、lr 走小验证集早停；**泛化包纪律**：建议 `urban_neon`
  全部轨迹不进训练集，作为"新包泛化"的最终试金石；训练与 ICR 评测的回合前缀不得重叠。
- 硬指标（A 轴）：协议一次通过率、熔断率、参数非法率 vs flash 基线；
- 软指标（D 轴）：配对盲评协议 ——
  1. 从 ICR 采样 20~30 对（同输入、两模型输出，匿名、随机左右序）；
  2. flash-referee（JudgeSystem 提示词 + 温度 0）对每对输出"哪边叙事质量更好"（它只判质量问题，
     风格偏好单独问）；
  3. ≥3 名人类读者盲评同一批；结论报 14B 偏好率与置信区间；
  4. 每对附禁表泄漏自动扫描结果（语料关键词命中即检出）。

### 7.4 决策门 2（三选一）

- **达标**：A 轴达标且 D 轴偏好率 ≥40%（与 flash 统计上无显著差）→ 进入 Phase 3 全本地；
- **质量未达**：A 轴达标但 D 轴 <40% → 记录差距清单，落地"本地优先 + 可选云端强化"双模式
  （§8.3），主回合默认本地、追求质量时切 flash（离线场景即接受该质量档）；
- **协议未达**：A 轴不达标 → 回炉（先查数据清洗，再查格式/温度/窗长），两轮回炉仍失败则
  判"14B 主回合不可行"，文档落结论与原因，不再追加投入。

---

## 8. Phase 3 · 落地形态（与决策门结论对应）

### 8.1 引擎零改动部分

- 纯配置：`DEEPSEEK_BASE_URL` 指向本地 vLLM + `DEEPSEEK_MODEL` 指本地模型即全本地；
- 小窗 profile（14B 档建议值，待 Phase 0 实测校准）：`compress_threshold` 30,000 → 18,000~24,000、
  `keep_turns` 6 → 4~6、lore 预算/近窗回合数同步下调；压缩机制不变（增量摘要本就是为小窗设计）。
  现状：这些值写死在 `cli.py:79-80`/`web.py:92`，实验期走独立脚本或临时改常量（附录 D.5），
  env 化并入 §8.2 小改造；
- CLI/Web 需补一个"本地模式"的启动文档/参数（如 `--local` 包装 env），F1 纪律：引擎层不放内容。

### 8.2 引擎小改造（仅混合路由需要，全本地不需要）

extract/reflect/dedup 目前回退主模型且与 turn 同端点。若做"judge/compress/extract 本地、
turn 云端"的混合省钱档，需要给 `Settings`/`LLMClient` 加 purpose → (base_url, model) 的路由
（现 `models` 只映射模型名、单 client 单端点）。改造点很小（config + llm 两文件），
改造后跑全量测试回归（217+ 用例）——纯增量，不碰协议与状态。
   顺带把 Game 长局参数 env 化（现写死 `cli.py:79-80`/`web.py:92`），本地模式即纯配置切换。

### 8.3 双模式（若决策门 2 = 质量未达）

- 本地模式（默认）：全本地 14B，质量档 = 实验实测值；
- 云端强化模式：`DEEPSEEK_BASE_URL` 切回官方 + `DEEPSEEK_MODEL=deepseek-v4-flash`（现配置），
  一键切换 = 环境变量，不改代码；
- 质量兜底：Judge 侧信道本地化后仍需保留 corpus 门禁（CI 化），防"模型换小后禁表泄漏率上升"
  无人发现。

---

## 9. 测量纪律（继承 P0/M2a/M2b/worldpack-QA 全套）

1. **三类失败区分**：API/服务偶发错误（复测确认）、数据/用例设计不当（改语料，不改判据）、
   模型能力不足（先怀疑前者再下结论）；
2. **材料纪律**：ICR 与 corpus 用例的判定材料必须能在生产同款 `build_materials`/真实 history
   内可见，测不到 = 材料问题不是模型问题；
3. **可复现**：判定类调用 temp=0；对抗用例 rounds=3 多数票（抗偶发）；语料与脚本不随模型改；
4. **计数优先**：小样本报 x/y，百分比只作汇总；
5. **留痕**：每次 run 记录 model/时间/成本；报告落 `reports/`（沿用 `judge_sensitivity_<ts>.json`
   与 usage JSONL 命名惯例）；对照 run 必须同命令同轮数；
6. **防污染**：训练/评测同源必须划保留集；一个世界包作泛化剔除包；
7. **回归**：任何引擎改动（即使只改配置）过全量离线测试（tests/ 217+），对照结论附审计。

---

## 10. 执行顺序与报告（滚动复盘区）

**顺序**：Phase 0（§5）→ 决策门 0 → Phase 1（§6）→ 决策门 1 → Phase 2（§7）→ 决策门 2 →
Phase 3（§8）。Phase 0 与仓库并行事项（如 worldpack-QA 计划 ④长局腐化验证）无冲突，
且 worldpack-QA 的长局产物可直接作为 Phase 2 的数据与基线补充。

**滚动复盘**：
- 2026-09-08 · 阶段 0-2 执行复盘（资产找回 / L2 门禁 / 离线统计 / vLLM 环境踩坑 /
  14B 零样本协议遵从第一信号 / 实验支撑脚本）→ `docs/local14b-p012-retro.md`。
- 2026-09-08~09-11 · 阶段 3-4 执行复盘（flash 对照重跑：小窗 smoke 三包全过 + dedup
  基线退化 40-50%；14B 矩阵：judge 两包过闸 / dedup 100% 完胜 flash / xianxia+urban
  小窗通关 / 间歇性协议熔断两类失败模式 / railed 0/3 暴露事实提取层失败 /
  长局零样本不可测）→ 同上文档 §5-§6。
- 2026-09-11 · 阶段 5-6 执行复盘与决策门 0（ICR v1/v2：污染输入毒化 vLLM + 
  **reasoning_content 缺失对 flash 的系统性 bias**——14B 一次通过 36-42% 跨形态稳定可靠、
  flash 重放数字不可靠；**判定：走预期路径进 Phase 1 窄模块蒸馏**，主回合留 Phase 2）
  → `reports/local14b-p0-20260911.md`（五轴初值）+ 同上复盘文档 §7。
- 2026-09-11 · Phase 1 前奏补测（compress：14B 保全率 0.23-0.67 < flash → 纳入训练；
  reflect：flash 空响应**根因实锤 = 思考模式吃光 200 预算**（2000 预算下能力正常），
  同根因解释 dedup 40-50% 退化（50 预算）——flash 补标一律放大 max_tokens；
  训练数据改分层构造：YAML 程序合成为主干、轨迹限量作锚、flash 只补标合成前缀）
  → 同上复盘文档 §8、§10。
- 2026-09-11 · 引擎侧落地：**侧信道预算统一**（新增 `game_agent/budgets.py` 单一真源：
  `MIN_CALL_TOKENS=500` 规则 + `EMPTY_RETRY_TOKENS=2000`；`complete_with_empty_retry`
  统一空响应升级重试并推广到 dedup/reflect/extract，judge 改走同一实现；compress / turn
  仅取常量、不纳入重试）→ `docs/local14b-p012-retro.md` §10.4 +
  `tests/test_sidechannel_budget.py`（5 例）+ 全量 **262 passed**。
- 2026-09-11 · **flash 对照线重测**（预算修复后）：dedup **40-50% → 10/10 = 100%**
  （历史取证：456 次调用 61.2% 顶满 50 预算）→「14B 完胜」改为**持平**；reflect 从 6 连空
  恢复到产出 2 条洞察；railed 轨道事实 flash 3/3（extract 零顶满，旧 34.8% 顶满）；
  judge 经"偏差方向"论证无需重测（空响应只会压低拦截率，100% 是保守下界）。
  残留问题处置：**截断升级重试 + judge 三态判定已修**（`d79345e`：`complete_with_meta`
  透出 `finish_reason`、判定 None=未知不再当通过、未知不进门禁分母；真机复验 reflect 洞察
  完整、矛盾率 0/2）；reflect 基础预算暂维持（现有 length 重试兜底）；
  **compress 补标预算已修**（`7573ac9`：`COMPRESS_MAX_TOKENS=4000` + 纳入升级重试 +
  **截断摘要不得采纳**；真机复验 5/5 无截断）→ `reports/sidechannel-budget-retest-20260911.md`。
- 2026-09-12 · **Phase 1 数据与评测计划立项** → `docs/plan-phase1-data.md`（训什么/训多少、
  三源配比、五模块输入与标签契约、轴覆盖矩阵、评测集冻结与三方对照协议）。
  相对原方案的两处修正：① **先造尺子再造数据**（现评测每类仅 6 条，1 条 = 16.7pp，
  分辨不出训练效果——"flash dedup 40-50%"的错误对照线就是坏尺子产物）；
  ② **dedup 不进训练集**（基座与 flash 实测均 100%），只作冻结回归护栏。

| 阶段 | 时间盒 | 报告 |
| --- | --- | --- |
| Phase 0 零样本对照 | 2~3 天 | `reports/local14b-p0-<ts>.md`（差距清单表 + 五轴初值） |
| Phase 1 窄模块蒸馏 | 5~8 天 | `reports/local14b-p1-<ts>.md`（数据/训练/验收） |
| Phase 2 主回合试点 | 1~2 周 | `reports/local14b-p2-<ts>.md`（含盲评明细） |
| Phase 3 落地 | 滚动 | `reports/local14b-p3-<ts>.md`（配置/改造/成本） |

---

## 附录 A：候选模型选型清单（Phase 0 前逐项确认）

| 项 | 确认内容 | 为什么关键 |
| --- | --- | --- |
| 上下文上限 | 原生/外推窗 ≥64K（推荐 128K 档），用真实 5 万 token 前缀实测不丢尾 | turn 输入中位 5.5 万 token；**Phase 0 实测小窗档压到 ≈20-24K → 32K 窗已够用**（Qwen2.5-14B 据此入选，见 §3 先验 1 修正） |
| 长窗质量 | 长输入下协议与叙事不塌（部分模型长窗外推后质量骤降） | 蒸馏天花板前提 |
| 中文创作 | 三包文风样例抽查（古风/仙侠/冷硬都市） | D 轴主力 |
| 工具协议 | OpenAI 兼容 + 工具调用稳定；思考模式建议关闭（thinking 输出不入引擎协议，关掉最稳） | A 轴 |
| vLLM 支持与许可 | 权重许可（商业/研究）、vLLM 量化与 prefix cache | E 轴与合规 |
| DeepSeek R1-Distill-Qwen-14B | 仅作对照参考：推理谱系 ≠ 创作谱系，不建议作为起点 | 防误用 |

## 附录 B：待补测量点

1. 当前生产配置（threshold 30K）下 turn prompt 分布重测（Phase 0 步骤 5）；
2. 既有轨迹的 A 轴离线基线统计（history 内 engine 协议提示计数，纯离线，可立即做）；
3. flash 对照 run 的每回合延迟采样（为 E 轴提供 API 侧基准）。

## 附录 C：相关文档

- 架构与协议：`docs/design.md`（§3 核心循环、§4 上下文、§10 约束/验证/纠正、§14.2 模型分层）
- 质量门与验收历史：`docs/p0-report.md`（E1 灵敏度）、`docs/m2b-postmortem.md`（300 轮长局）、
  `docs/world3-report.md`（跨包验证）、`docs/plan-worldpack-qa.md`（并行中的质量门补齐）
- 代码接缝：`game_agent/config.py`、`game_agent/llm.py`、`game_agent/game.py`、`scripts/judge_sensitivity.py`

---

## 附录 D：单卡 4090（24 GB）验证部署速查

### D.1 一句话结论

验证用途（Phase 0 门禁、ICR 对拍、窄模块小样）**一张 4090 够用**：AWQ/GPTQ 4-bit 量化 +
KV fp8 + 32K 小窗 profile；decode 吞吐满足单用户回合游戏。**不够的事**交给多卡/云：
128K 长窗对照、300 轮长局、正式训练。

### D.2 显存细账（量级估算，以 vLLM 启动日志/`nvidia-smi` 实测为准）

| 项 | 数值 | 说明 |
| --- | --- | --- |
| 14B 权重 bf16 | ~28 GB | 单卡必量化 |
| AWQ/GPTQ 4-bit 权重 | ~9~10 GB | 推荐档 |
| FP8 权重 | ~15 GB | 可行，留给 KV 的余量小 |
| KV cache | ~80 KB（fp8）/ 160 KB（fp16）每 token（14B GQA 档量级） | 长上下文大头 |
| 24 GB 卡可达上下文 | 32K 轻松 / 64K 可 / **128K 不推荐** | `gpu-memory-utilization≈0.92` + KV fp8 时 |

启动后看 vLLM 日志的 `GPU KV cache size: ~X GiB` 确认分配；OOM 就降 `--max-model-len`。

### D.3 速度预算（4090，量级）

- **decode** 50~90 tok/s：单用户够；回合输出中位 810 tok ≈ 10~18 s/回合，引擎流式输出
  （SSE / `on_text`）边生成边显示，观感可接受；
- **prefill（TTFT）是瓶颈**：3 万 token 输入 ≈ 3~8 s，5 万+ ≈ 5~15 s；
- 缓解三件套：① `--enable-prefix-caching`（本引擎静态前缀稳定，usage 中 `cache_hit` 中位
  5.5 万佐证——后续回合只对增量 prefill，TTFT 显著下降）；② 小窗 profile（D.5）；
  ③ 流式 UI；
- 判定类调用（judge/extract/dedup，输入 <10K token）TTFT 亚秒~2 s 级，**完全无压力**。

### D.4 启动与接入（不改引擎代码）

```bash
# ① 4090 验证档（模型首次运行会自动从 HuggingFace 下载）
vllm serve Qwen/Qwen2.5-14B-Instruct-AWQ \
  --max-model-len 32768 --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.92 --enable-prefix-caching \
  --served-model-name local-14b

# ② 新终端：独立 env，不动 .env 原件。
#    API key 用占位符即可（引擎只检查非空，本地服务不校验）
$env:DEEPSEEK_API_KEY = "sk-local"
$env:DEEPSEEK_BASE_URL = "http://127.0.0.1:8000/v1"
$env:DEEPSEEK_MODEL = "local-14b"
uv run python -m game_agent play world-packs/ancient_jianghu
```

冒烟顺序：`curl http://127.0.0.1:8000/v1/models` → 手玩 3~5 回合看协议（history 里无
`[引擎提示]`/`[协议错误]`、无熔断）→ usage jsonl 确认 `model=local-14b` → §5.3 矩阵全量。

### D.5 小窗 profile 的现实与改法

现状：`cli.py:79-80` / `web.py:92` 写死 `extract_every=2, compress_threshold=30000,
judge_every=5, reflect_every=10`，`keep_turns` 用 Game 缺省 6（§2.2 注记）。4090 32K 窗
建议值（Phase 0 步骤 5 校准前）：

| 参数 | 现生产值 | 4090 验证建议 | 效果 |
| --- | --- | --- | --- |
| `compress_threshold` | 30,000 | 18,000~22,000 | 历史更早回落；回合输入估 ≈25~32K（字符≈token，以 usage 实测为准） |
| `keep_turns` | 6（缺省） | 4 | 近窗更小 |
| `extract_every` / `judge_every` / `reflect_every` | 2 / 5 / 10 | 不变 | 侧信道密度不动（控制变量） |

改法三选：
1. **独立实验脚本直连 `Game(...)` 构造**（不碰引擎文件，推荐）；
2. 临时改 cli.py/web.py 常量，跑完还原——改前先确认工作区干净（`git status`），跑完用
   `git checkout -- cli.py web.py` 精确还原，避免把实验常量混进提交；
3. env 化（正式方案，并入 §8.2 小改造清单）。

任何引擎改动后过 `tests/` 全量回归。

### D.6 模型选型与协议注意（Phase 0 只测一个，减少变量）

- **首选 Qwen2.5-14B-Instruct(-AWQ)**：无 thinking 协议、工具调用稳定、vLLM 直接支持；
- Qwen3-14B：默认 chat template 带 thinking，引擎 SDK 调用未传 `chat_template_kwargs` →
  需给 `llm.py` 的 `create()` 加 `extra_body` 或换无思考模板；验证期不碰，多卡对照再评估；
- 长窗变体（如 Qwen2.5-14B 长窗/1M 档）：先确认 vLLM 加载与 rope 上限；4090 上不必追 128K；
- R1-Distill 系：推理谱系，不作创作起点（附录 A）。

### D.7 分工建议

| 工作 | 单卡 4090（本地） | 多卡/云 GPU |
| --- | --- | --- |
| Phase 0：判定类门禁 + 32K 小窗主回合 + ICR 对拍 | ✅ | — |
| QLoRA 短序列试验（样本 ≤8~16K token） | ✅ | — |
| 128K 长窗对照、300 轮长局（C/E 轴） | ❌ | ✅ |
| Phase 1/2 正式训练与大规模补标 | 备选 | ✅ |

# 本地 14B 实验 · 阶段 0-5 执行复盘 + Phase 1 前奏补测（2026-09-08 ~ 09-11）

> 对应 `docs/plan-local-14b.md` Phase 0 的全段执行：阶段 0（L2 门禁 + 离线统计）、
> 阶段 1（vLLM 环境与接入冒烟）、阶段 2（实验支撑脚本）、阶段 3（flash 侧对照重跑）、
> 阶段 4（14B 侧门禁矩阵）、阶段 5（ICR 对拍 v1/v2）、阶段 6（决策门 0 报告），
> 以及 Phase 1 前奏：compress/reflect 零样本补测（§8）。
> 决策门 0 正式结论见 `reports/local14b-p0-20260911.md`。

---

## 1. 结论速览

| 条目 | 结果 |
| --- | --- |
| 数据资产 | ✅ `git pull`（b720aca→a9064f5）找回 saves/reports 全部历史产物（4038 条 usage、三包轨迹、judge 基线） |
| L2 门禁 | ✅ **254 passed**（a9064f5 含 judge.py 空响应升级重试改动） |
| flash A 轴离线基线 | ✅ 生产配置轨迹零协议失败（smoke 三包 + longrun + baseline 242 回合） |
| 小窗 profile 校准 | ✅ `compress_threshold=20000 · keep_turns=4`（usage turn 中位 29K 实测支撑） |
| vLLM 环境 | ✅ venv 方案（Docker 拉取失败回退）· vLLM 0.11.1 + torch 2.9.0+cu128 · Qwen2.5-14B-Instruct-AWQ 9.4GB |
| 接入冒烟 | ✅ 协议闭环跑通（工具解析 / change_stat / 叙事质量成立）· usage 落盘 `model=local-14b` |
| **A 轴第一信号** | ⚠️ **14B 零样本协议遵从差**：单回合常态 `iterations=2`；`--days 1` 冒烟偶发熔断（诊断见 §3.4） |
| 实验支撑脚本 | ✅ `worldpack_smoke.py` 参数化（零行为变化）+ `icr_sampling.py` 新增（dry-run/双模型小样全验） |
| 阶段 3：flash 小窗 smoke 三包 | ✅ 全过：结局（问道长生/自由落体）· 审计零偏差 ×3 · 禁表零泄漏 ×3 · 压缩正常触发 · 成本 **≈¥1**（cache 命中 87-96%） |
| 阶段 3：flash 专项冒烟 | ✅ lore / reflect（0 洞察 0 矛盾，样本弱）/ injection / start / condition ×2 全过；⚠️ **dedup_test 拦截率 40-50% < 80%**（根因 = 思考模式吃光 50 预算，§5.3 实锤） |
| 阶段 3：ICR 采样源 | ⚠️ 压缩后轨迹边界仅 12/8/3 → 采样源改全量轨迹池混合（§5.4） |
| 阶段 4：B 轴 judge | ✅ xianxia/urban 门禁过（误报 0/12 ×3 全零）；ancient 不过（setting 4/6，总拦截 15/18）——差距 −5.6 / −11.1 / −16.7pp vs flash 100% |
| 阶段 4：B 轴 dedup | 🎉 **14B 拦截 10/10 = 100%，完胜 flash 实测 40-50%** |
| 阶段 4：A/C 轴 smoke | ✅ xianxia「问道长生」+ urban「自由落体」**通关**（审计零偏差 + 禁表零泄漏）；ancient 熔断 ×2 run（间歇性，§6.3） |
| 阶段 4：C 轴 | ⚠️ railed **0/3 保持**——根因在事实提取层（轨道事实未入桶），非召回失败（§6.5）；长局回合 0 熔断 → **长局在零样本协议不稳下不可测**，留给 Phase 1 |
| 阶段 4：专项冒烟 | lore/reflect/injection ✅；start ✗ ×2、condition×2 ✗ 各 2 次（熔断） |
| 熔断根因升级 | vLLM 日志定位：hermes `<tool_call>` 块 JSON 损坏 → 解析器丢弃 → 引擎判「无工具调用」×3 → 熔断（与纯文本不调工具并列的两类失败模式） |
| 阶段 5：ICR v1（83 前缀无状态栏） | ⚠️ 反直觉结果 + 两个方法论修正（§7）：flash 一次通过仅 13.4%（**无状态栏是形态 artifact**）；7 个 local api_error = **污染输入毒化 vLLM**（历史坏 JSON tool_calls → 模板解析 400）→ v2 重跑 |
| 阶段 5：ICR v2 | ✅ 60 前缀带状态栏 + 污染剔除：flash 8.6% / 14B 36.4% 一次通过——**发现 reasoning_content 缺失是 ICR 对 flash 的系统性 bias**（§7.3）；14B 数字跨形态稳定可靠 |
| 决策门 0 | **→ Phase 1（窄模块蒸馏）**：B 轴 dedup 达标 judge 差 6-17pp；A 轴生产形态不达标；C 轴提取层缺口；报告 `reports/local14b-p0-20260911.md` |
| Phase 1 前奏：compress 补测 | ⚠️ 14B 会写摘要但保全率 0.23-0.67 < flash 0.47-0.89（长输入最弱）→ **纳入训练**；flash 有偶发空响应（重跑恢复） |
| Phase 1 前奏：reflect 补测 | ⚠️ flash 6 连空响应**根因实锤 = 思考模式吃光 200 预算**（2000 预算下能力正常）；14B 1/3 合法洞察 → 引擎修预算后重测，训练最低优先级 |
| E 轴初值 | prefill ~600 tok/s（**低于附录 D.3 预估**）· generation 35-56 tok/s · prefix hit 87% · 显存 21.8/24GB |

---

## 2. 阶段 0：L2 门禁 + 离线统计

### 2.1 数据资产找回（执行计划的三个修正事实）

探索 agent 首轮盘点时 `saves/`、`reports/` 均不存在——原因是本地 HEAD 停在 `b720aca`，
远程已有 `a9064f5`（"chore: track saves/reports"，113 文件）。`git pull --ff-only` 后找回：

- **usage 全量 4038 条**（turn 1404 / judge 1378 / extract 480 / dedup 349 / compress 43 / reflect 4 / aux 51 / recall 10 + import_*）；
- 三包 smoke / longrun / 300 轮 memory-regression / condition 事件等全部轨迹存档；
- 三包 judge 基线报告全绿（对抗类全拦截、normal 12/12 零误报，2026-09-08 13:38-13:41）。
  另见 `star_ring` 基线失败（ooc 4/6=0.667，09-08 20:22）——不在 Phase 0 范围，仅备注未触碰。

**纪律修正**：saves/reports 现被 git track 且是 Phase 2 数据源——所有新 run 一律 `--out-prefix`
区分输出，不覆盖历史档；执行前先整目录归档（`cp -r saves /tmp/saves.archive-flash`，3.8M）。

### 2.2 六调用点 token 分布重测（附录 B）

解析全部 usage jsonl（历史全量，flash 时代；当前生产配置 = `extract_every=2,
compress_threshold=30000, judge_every=5, keep_turns=6`）：

| purpose | n | 输入中位 | 输入 p95 | 输入 max | 输出中位 |
| --- | --- | --- | --- | --- | --- |
| turn | 1404 | **29,267** | 267,624 | 329,348 | 809 |
| compress | 43 | 12,213 | 16,739 | 18,244 | 1,999 |
| extract | 480 | 1,077 | 2,271 | 3,688 | 273 |
| dedup | 349 | 374 | 1,143 | 1,224 | 50 |
| judge | 1378 | 551 | 767 | 1,406 | 183 |
| reflect | 4 | 490 | 560 | 560 | 200 |
| aux | 51 | 7,804 | 9,846 | 9,850 | 157 |

与文档 §2.1 的差异：turn 输入中位 29K（文档 55.5K 来自压缩前 300 轮时代混入；p95 26.7 万
即出自那批）——**生产配置压缩生效后 turn 输入中位 ≈29K 真实 token**，是 32K 小窗可行性
的关键实测证据。

### 2.3 flash 生产档 A 轴离线基线（从既有轨迹直数）

| 轨迹 | 回合 | [引擎提示] | [协议错误] | [引擎拒绝] | 截断段 |
| --- | --- | --- | --- | --- | --- |
| smoke-ancient_jianghu | 6 | 0 | 0 | 0 | 0 |
| smoke-xianxia_wendao | 7 | 0 | 0 | 0 | 0 |
| smoke-urban_neon | 10 | 0 | 0 | 0 | 0 |
| longrun-xianxia_wendao | 35 | 0 | 0 | 0 | 0 |
| baseline-checkpoint | 242 | 0 | 0 | 1 | 0 |
| memory-regression-checkpoint | 121 | 5 | 11 | 0 | 2 |

**生产配置基线 = 零协议失败**。memory-regression 的 5 提示/11 错误是**压缩前时代**
（M2a，compress_threshold=0）产物，不计入生产基线——恰好是"压缩上线改善协议"的又一佐证。

### 2.4 小窗 profile 校准与采样源发现

- **校准**：compress_threshold=30000 → **20000**（D.5 建议区间 18-22K 中值），keep_turns=6 → **4**。
  依据：usage turn 中位 29K 真实 token ≈ 阈值 30000 字符估的产物；阈值降到 20000 字符估
  后 turn 输入估 ≈20-24K，落在 32K 窗内。正式值待阶段 3/4 新 run 的 usage 复测。
- **采样源不足**：历史 smoke 轨迹仅 6/7/10 个回合边界（前缀中位 1.5-3.9K token），
  远不够 ICR 每包 30 前缀。**修正：ICR 采样源改用阶段 3/4 新跑的小窗 smoke 轨迹**
  （~30-40 边界/包）+ 长档（longrun 35 边界 / memory-regression 121 边界）补充。

---

## 3. 阶段 1：vLLM 环境搭建（踩坑地图）

### 3.1 部署方式：Docker → venv 回退

- **现象**：`docker pull vllm/vllm-openai:v0.11.1` 两次在 339MB / 3.2GB 层报
  `short read: expected N bytes but got 0: unexpected EOF`（exit 0 被 tail 管道掩盖，教训见 §7）。
- **根因**：docker hub 大层下载在该网络下不稳定（小层能过）。
- **解法**：按计划风险回退条款切 venv 方案：`uv venv --python 3.12` + `uv pip install vllm`
  （PyPI 可达，dry-run 先行验证）。

### 3.2 CUDA 版本不匹配（本阶段最隐蔽的一坑）

- **现象**：vLLM 0.28.0（PyPI 默认最新）启动报
  `RuntimeError: The NVIDIA driver on your system is too old (found version 12080)`。
- **根因**：0.28.0 的 PyPI 默认 wheel 为 CUDA 13.0 编译（torch cu130），要求驱动 ≥580；
  本机驱动 570.153.02 = CUDA 12.8。pip 解析成功 ≠ 运行时能跑（dry-run 只验证依赖解析）。
- **解法**：`uv pip install vllm==0.11.1`（默认 wheel 为 cu12.8 时代，解析出
  nvidia-cublas-cu12==12.8.4.1 系 + torch 2.9.0+cu128）——正好是原计划选的 Docker 镜像版本。
- **预防**：装 GPU 推理栈前先对齐三件套版本：驱动 CUDA 上限 ↔ vLLM wheel CUDA ↔ torch CUDA；
  dry-run 后再加一条 `python -c "import torch; torch.zeros(1).cuda()"` 真机探针。

### 3.3 启动参数三连坑

| 现象 | 根因 | 解法 |
| --- | --- | --- |
| `FileNotFoundError: 'ninja'`（EngineCore 子进程） | 绝对路径调 `vllm serve`，venv bin 不在 PATH，子进程找不到 ninja | 启动前 `export PATH=<venv>/bin:$PATH` |
| `CUDA out of memory ... warming up sampler with 256 dummy requests` | `gpu-memory-utilization 0.92` 对 32K 窗 + fp8 KV 过于激进，warmup 阶段 22GB 已占满 | 降为 **0.85** + `--max-num-seqs 64` |
| 400 `"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser` | vLLM 工具调用需显式开启解析器 | `--enable-auto-tool-choice --tool-call-parser hermes`（Qwen2.5 推荐解析器） |
| （运维坑）新服务启动失败且 curl 仍通 | 旧服务占着 GPU 与 8000 端口，新服务 EngineCore 显存分配失败，curl 打到旧进程 | 起新服务前先停旧进程（TaskStop）+ 确认 `nvidia-smi` 归零 |

### 3.4 接入冒烟与 A 轴第一信号

冒烟顺序（D.4）：`/v1/models` → local-14b ✓（启动 ~60s，KV cache 110,272 tokens）→
`smoke_llm.py` 协议闭环 ✓（首回合叙事/choices/plot_signal 全合法，`iterations=2`）→
`worldpack_smoke.py --days 1` **协议熔断**（连续 3 次未产出合法协议输出）。

**诊断**（`/tmp/diag_turn.py`：拦截 `chat.completions.create` 逐次打印原始返回）定位失败模式：

1. **纯文本不调工具**（最常见）：`finish_reason=stop`，把叙事和「选择：1…」列表全写进正文，
   不调 `submit_narration`——引擎判失败注入 `[引擎提示]`；
2. 重试后大多能纠正（产出合法 `change_stat` + `submit_narration`）→ 每回合常态 `iterations=2`；
3. 偶发连续 3 次失败 → 熔断（`--days 1` 冒烟命中一次，复现 run 则全过——温度 1.0 下的方差）；
4. 首轮输出逼近 2048 token 上限（正文写「选择」列表是主因，与 M3 发现的
   `finish_reason=length` 截断模式同源）。

**结论**：接入层完全正确（工具解析、数值契约、侧信道 extract 全通）；熔断是**模型零样本
协议遵从能力**现象，不是环境问题。这直接回答了 P2 命题的零样本部分，也是 ICR A 轴的
核心测量对象。对照 flash 生产轨迹零协议失败——差距已可预期。

### 3.5 E 轴初值（vLLM 日志 + nvidia-smi）

- 显存：21.8/24GB（0.85 利用率满负荷）；KV cache 110K tokens 池。
- 吞吐：**prefill ~600 tok/s**（低于附录 D.3 预估 3-8s/3万token——chunked prefill 2048
  批大小限制下，长 prompt 实测留给 ICR 阶段）；generation 35-56 tok/s（D.3 预估区间内）。
- **prefix cache hit 87%**——与文档预判一致（静态前缀稳定，验证了 `--enable-prefix-caching`
  的必要性；flash 侧重放同轨迹时服务端缓存命中同样高达 92%）。
- usage 记账：`model=local-14b` 条目正常落盘；`cache_hit_tokens` 字段为 null（vLLM 不返回，
  符合文档预期）；本地模型成本显示"价格未知"（PRICES 快照无此模型，不影响记账）。

---

## 4. 阶段 2：实验支撑脚本

### 4.1 `scripts/worldpack_smoke.py` 参数化（零行为变化）

新增三个可选参数（默认值 = 现生产值）：`--compress-threshold 30000` / `--keep-turns 6` /
`--out-prefix smoke`（落盘名与 usage 文件名同步前缀）。改动点：Game 构造（原 152-155 行）
与落盘（原 234-238 行）。`tests/test_worldpack_smoke.py` 4 例离线回归通过，全量 254 passed。

### 4.2 `scripts/icr_sampling.py` 新增（~490 行）

按 `plan-local-14b.md` §5.3/§7.4 设计实现，要点：

- **回合边界**：user 无 name（真实玩家输入/选择）或 engine+`【事件】` 前缀——比
  `compression.find_turn_cut` 严格（后者把回合中段 `[引擎提示]` 误判为起点，采样语义会错）；
- **双 client 直构**：flash 用 `dotenv_values(".env")` 读文件（免疫 shell 残留 env 污染——
  `load_dotenv` 不覆盖已导出变量，flash/14B 两侧必须显式隔离）；local 显式构造
  `timeout=300s/max_retries=2`（不走 `make_client` 的 180s/5 重试，本地慢请求与偶发 500 不适用）；
- **回放环境**：`MemorySystem(pack, llm=None)` 关 dedup 侧信道（保 A 轴纯净）；
  `apply_change`/`remember` 回调复刻 `game.py:269-287`；样本间 `state.copy()` 隔离；
- **A 轴信号**（全部来自 `TurnResult`）：iterations / 熔断 / `[协议错误]`+`[引擎拒绝]` 分类 /
  脏文本（`clean_narration(raw)!=raw`）/ 截断（`[引擎提示]` 含 finish_reason=length）；
  E 轴每样本 wall 时间；单样本隔离（一次熔断/API 错误不中断整轮）；
- **截断**：`--truncate-tokens N` 复用 compression 三件套（find_turn_cut/locate_summary/
  ensure_pairing），配对不成立则放弃截断并告警——**两模型永远吃同一份输入**；
- **输出**：`reports/icr-<ts>.json`（含 reveal 映射 + A 轴汇总 + usage 汇总）、
  `saves/icr-pool-<ts>.json`（匿名随机左右序盲评池 + `_forbidden_tokens` 禁表扫描）、
  `saves/usage-icr.jsonl`（共享 tracker 按 model 字段分账）。

验证记录：

| 步骤 | 结果 |
| --- | --- |
| `--dry-run` 三包推断 | 边界 6/10/7、采样 4/8/5（min-turn=2 后）、离线基线正确 |
| `--dry-run` 长档截断 | longrun 35 边界截断 1 个配对成立；memory-regression 121 边界（旧格式档）5/5 截断 |
| 旧档兼容 | `GameState.from_dict` 对 M2a 时代存档抛 KeyError（缺 `pack_name` 字段）→ 兜底改为 `(ValueError, KeyError)` 捕获 + `from_pack` 回退 |
| flash 小样（2 前缀） | ok=2/2 · **92% cache 命中** · ¥0.019（重放同轨迹时 DeepSeek 服务端缓存红利巨大，全量成本将低于预估） |
| 双模型小样（2 前缀） | local ok=1/2 + 熔断 1（隔离正常，flash 结果完整落盘）；盲评池只收双方成功对（1 对） |
| 全量回归 | **254 passed**；验证产物已清理（reports/icr-*.json ×2、icr-pool ×2、usage-icr.jsonl） |

### 4.3 已知偏差（写进报告 meta，不在 v1 修复）

- 输入默认**不带状态栏**（`--with-status` 关闭）：存档只有终局 state，早期前缀配终局
  状态栏会向模型泄漏"未来事实"。两模型同输入，配对有效性不受影响；Phase 3 落地前
  需"逐前缀重建 state"（stat_log 回放，audit.py 已验证可行）再默认开启。
- `apply_change`/`remember` 也作用在终局 state 的副本上——契约校验（范围/上限）语义不变。

---

## 5. 阶段 3：flash 侧对照重跑（2026-09-08 晚）

### 5.1 小窗 smoke 三包（compress_threshold=20000 · keep_turns=4，`--out-prefix smoke-flash`）

| 包 | 结局 | 审计 | 禁表 | compress 触发 |
| --- | --- | --- | --- | --- |
| 江湖旧梦 | 日常冒烟无结局（profile 目标即如此，与历史一致） | 零偏差（15 条变更） | 零泄漏 | 0 次 |
| 问道长生 | ✅ 问道长生 | 零偏差（29 条） | 零泄漏 | 1 次 |
| 霓虹深处 | ✅ 自由落体 | 零偏差（26 条） | 零泄漏 | 2 次 |

结论：**flash 在小窗档生产可玩性确认**，压缩机制在 20000 阈值下正常触发。
成本 ≈¥0.52（cache 命中率 87-96%——静态前缀稳定的红利兑现，全量预算大幅下修）。

### 5.2 专项冒烟

| 项 | 结果 | 备注 |
| --- | --- | --- |
| lore_smoke | ✅ | — |
| reflect_smoke | ✅ 0/0 矛盾 | **样本弱**：14 回合 NPC 记忆不足 8 条（REFLECT_MIN_MEMORIES 门控），未产生洞察——对照时须同命令同轮数 |
| dedup_test | ✗ **拦截率 5/10 → 重跑 4/10（要求 ≥80%）** | 见 §5.3 |
| injection_test | ✅ | — |
| start_smoke | ✅（F1 + baseline_probe 探针） | — |
| condition_event_smoke ×2 | ✅ 洗剑池夜话（好感 37 第 3 天）/ 诊所夜话（好感 36 第 3 天）+ 审计零偏差 | — |

### 5.3 ⚠️ dedup 基线退化（根因已实锤：思考模式吃光预算，非能力退化）

- **现象**：dedup_test 拦截率 5/10（第一次）→ 4/10（重跑），两次**失败组不同**。
- **已排除的变量**：PAIRS 固定 10 组（自 P1 未改）；dedup 调用 `temperature=0.0` 显式传参；
  memory.py dedup 逻辑 P1 后未动。
- **根因（2026-09-11 实锤）**：v4-flash 换代后带思考模式，触发与否不稳定；
  引擎 dedup 预算 `DEDUP_MAX_TOKENS=50`——思考触发时吃光全部预算（completion=50 顶满、
  content 空），引擎把空输出判为「不重复」→ 放行。usage 近 30 次调用呈双峰：
  **成功调用 completion=2（"重复"两字），失败调用 completion=50（预算顶满）**。
  裸调复现：组10 在 50 预算下思考 48 token 勉强挤下（输出"重复"），稍长即空。
- **同一根因的兄弟问题**：judge 500 预算（a9064f5 已加空响应升级重试 2000 修复）、
  reflect 200 预算（§8.2 实锤）、extract 400 预算（存疑，smoke 中未见异常）。
- **对实验的影响**：flash dedup 对照线 40-50% 是**预算 bug 拉低的值**，不是能力基线；
  14B 的 100% 仍然有效（无思考模式）。B 轴 dedup 结论不受影响（14B 已达标）；
  但「flash 补标」环节必须放大 max_tokens 再采标。
- **对产品的影响**：建议引擎侧立项——把 judge 的空响应升级重试推广到
  dedup/reflect/extract（或按模块调大预算），P1 门禁即可恢复。

### 5.4 ICR 采样源修正（阶段 5 决策）

小窗 smoke 轨迹经压缩后存档只剩「摘要 + 近窗 4 回合」：边界 12/8/3（xianxia/urban
通关太快 + 压缩吃掉了早期回合），前缀 token 中位 8.9K。历史短档 6/7/10 同样不足。
**采样源改为全量轨迹池混合**：

| 源 | 边界 | 用途 |
| --- | --- | --- |
| smoke-flash 三包（阶段 3 新跑） | 12/8/3 | 小窗生产形态 |
| smoke 三包（历史） | 6/7/10 | 生产档形态 |
| longrun-xianxia_wendao（历史） | 35 | 长局压缩后形态 |
| memory-regression-checkpoint（历史） | 121 | 压缩前长局形态（探针包） |
| baseline-checkpoint（历史） | 242 | 压缩前长局形态（探针包） |

配额规则：每包最多 30、不足全取（计数优先纪律，报告 x/y）。预计总采样 ~90 前缀
（xianxia 30 + ancient 18 + urban 13 + baseline_probe 30）。

### 5.5 阶段 3 产出

- `saves/smoke-flash-<pack>.{json,txt}` + `usage-smoke-flash-<pack>.jsonl` ×3
- `saves/condition-<pack>.{json,txt}` ×2（flash 重跑覆盖历史档；历史版在 /tmp 归档）
- `reports/usage-{lore,reflect,dedup}-smoke.jsonl` / `usage-judge-sensitivity.jsonl` 追加
- 阶段 3 总成本 ≈ **¥1**（vs 预估 ¥10-15——cache 红利 + 千 token 级轻量调用的现实）

---

## 6. 阶段 4：14B 侧门禁矩阵（2026-09-11）

全链一次跑完 + 失败项各重试一次（重试链在途）。所有命令 env 覆盖
`DEEPSEEK_BASE_URL=http://127.0.0.1:8000/v1 DEEPSEEK_MODEL=local-14b`，小窗档
`--compress-threshold 20000 --keep-turns 4`，与阶段 3 flash 侧同命令。

### 6.1 B 轴 · judge 门禁三包（14B 当判官，rounds=3 多数票）

| 包 | 拦截率 | 误报率 | 门禁 | vs flash 基线（100%/0%） |
| --- | --- | --- | --- | --- |
| 霓虹深处 | 17/18 = 94.4% | 0/12 = 0% | ✅ | −5.6pp |
| 问道长生 | 16/18 = 88.9% | 0/12 = 0% | ✅ | −11.1pp |
| 江湖旧梦 | 15/18 = 83.3%（setting 类 4/6=67%） | 0/12 = 0% | ❌ | −16.7pp |

读数：**误报控制完美**（0/12 ×3，与 flash 一致）；拦截率 83-94% 弱于 flash 但整体高于
脚本 80% 总闸。按文档 §5.4 判据（≥ flash−5pp）三包均不达标——但对抗语料每类仅 6 条，
1 条即 16.7pp，小样本下 −5pp 判据过严；最终结论与 ICR 判定一致率合并裁决（阶段 6）。

### 6.2 B 轴 · dedup：14B 完胜 flash（本阶段最大正面信号）

- flash 实测拦截率 40-50%（§5.3，且 temp=0 下不稳定）；
- **14B 拦截 10/10 = 100%**，判定稳定（与 flash 的"不稳定 40-50%"形成鲜明对比）。
- 解读：dedup 是"语义等价二值判定"窄任务，输入 <1K token，恰好落在 14B 能力带内；
  flash 的退化可能与服务端模型行为漂移有关（§5.3）。**窄模块本地化的第一个实锤论据**。

### 6.3 A/C 轴 · 小窗 smoke 三包

| 包 | 结果 |
| --- | --- |
| 问道长生 | ✅ **通关**（审计零偏差 17 条变更 + 禁表零泄漏） |
| 霓虹深处 | ✅ **通关**（审计零偏差 20 条 + 禁表零泄漏；世界观合法元素 终端5/霓虹6/数据18/信用点42；古风残留 1 处「姑娘」，疑似子串误报） |
| 江湖旧梦 | ✗ 熔断（2 次 run 均熔断） |

**14B 零样本在小窗档可以通关两个世界包**——P3 质量轴的强正面信号（叙事、结局达成、
数值纪律、世界观边界都成立）。ancient 两连熔断与阶段 1 的"复现通过"合起来说明：
协议失败是**间歇性**的（温度 1.0 方差），单次 run 的熔断不能判"该包不可玩"，
A 轴的精确测量必须靠 ICR 全量统计（阶段 5）。

### 6.4 熔断根因升级（vLLM 服务日志定位）

```
hermes_tool_parser.py  json.decoder.JSONDecodeError: Extra data: line 3 column 1
```

Qwen2.5 的原生工具调用是 hermes 风格文本格式（`<tool_call>` 块，vLLM `hermes` 解析器
负责提取）。14B 输出中该块 JSON **多出内容/损坏**时，解析器静默丢弃工具调用 →
引擎看到「无工具调用」→ `[引擎提示]` 重试 ×3 仍坏 → 熔断。**两类失败模式并存**：
① 纯文本不调工具（阶段 1 诊断，finish_reason=stop）；② hermes 块 JSON 损坏（本节）。
两者都指向同一个能力缺口：**零样本 14B 的协议格式遵从不稳定**——恰好是 Phase 2 蒸馏
要解决的核心问题（文档 §7.1：协议遵从是 SFT 最容易学会的东西）。

### 6.5 C 轴与专项冒烟

| 项 | 结果 |
| --- | --- |
| railed_fact_check（轨道事实） | ✗ 重试 run 跑完 → 检查 **0/3 保持**。**根因在提取/记忆写入环节**：检查时 14B 诚实回答「不知道」，因为玩家事实桶里根本没有轨道事实（剑名/师承/家乡），桶中只有 3 条关系进展类记忆——remember/extract 侧信道捕捉偏好偏关系、漏身份事实。非召回失败，是**事实提取层失败**（flash 基线 3/3 对照成立）。 |
| baseline_probe 长局 120 回合 | ✗ **回合 0 熔断** → 零样本协议不稳下长局不可测，判定留给 Phase 1 蒸馏后重测 |
| lore_smoke | ✅ |
| reflect_smoke | ✅ 洞察矛盾率 0/0（样本弱：14 回合记忆不足 8 条门控，未产生洞察） |
| injection_test | ✅（数值契约/无泄露/结局未受口头控制） |
| start_smoke | ✗ 熔断 ×2 run（F1 冒烟失败） |
| condition_event_smoke ×2 | ✗ 熔断 ×2 run 各 2 次 |

### 6.6 阶段 4 小结

- **能过的都过了，且质量轴正面**：judge 两包、dedup 满分、两包通关——14B 零样本的
  "上限"相当高；
- **卡点在协议稳定性**：间歇性熔断（两类失败模式）使单次 run 不可复现，A 轴需统计口径；
- **C 轴新缺口**：railed 0/3 暴露**事实提取层失败**（轨道事实未入桶，非召回失败）——
  与协议不稳定并列的第二能力缺口，Phase 1 窄模块蒸馏的 extract 模块需要重点补；
- **长局在零样本下不可测**——留给 Phase 1（窄模块蒸馏后协议稳定性预期大幅改善，
  文档 §7.1）。
- 成本：本地推理零 API 成本；总时长 ~5 小时（含重试）。

---

## 7. 阶段 5：ICR 对拍（v1 教训 → v2 重跑）

### 7.1 v1（83 前缀，无状态栏形态）结果与方法论修正

| A 轴 | flash | local-14b | 读法 |
| --- | --- | --- | --- |
| ok | 82/83 | 66/83 | — |
| 一次通过率 | **13.4%**（11/82） | **42.4%**（28/66） | ⚠️ 反直觉：14B 反而更高 |
| 熔断 | 1 | 10 | 14B 熔断率 12%，仍是硬伤 |
| 参数非法 | 10（全是坏 JSON 工具参数） | 4 | — |
| 脏文本 / 截断 | 4 / 3 | 3 / 2 | 相当 |
| 平均迭代 | 1.988 | 1.667 | 14B 更快收敛 |
| 均时 | 15.44s | 13.3s | 本地不快于云端（短前缀下） |
| api_error | 0 | 7 | 全部是污染输入（§7.2） |

**修正 1 —— 形态 artifact**：v1 默认不带状态栏（防"未来事实泄漏"）。但 flash 的生产输入
**总是**带 `<agent_status>` 状态栏——无状态栏重放把 flash 拉出了生产分布（一次通过率
13.4% vs 生产轨迹零协议失败）。结论：**A 轴（协议）测量必须用生产形态（--with-status）**；
状态泄漏对协议轴无影响（协议遵从不依赖状态值正确性），D 轴盲评池保持无状态栏形态
（避免未来事实影响叙事质量评估）。

**修正 2 —— 输入污染毒化本地端点**：7 个 local api_error 全部是
`400 BadRequest: Unterminated string starting at: line 1 column N`。定位（vLLM
serving_chat.py 预处理层 + 逐样本前缀扫描）：**失败的 7 个前缀恰好都含 flash 时代的
`[协议错误]` tool 消息**（坏 JSON 工具调用残迹）。vLLM 的 Qwen 模板对历史 tool 消息
做严格 JSON 解析 → 400；DeepSeek 官方端点宽容 → flash 侧不受影响。这是文档 §7.2
清洗黑名单的**重放侧实证**：协议失败段不仅不能入训练集，连重放输入都会毒化本地推理栈。

### 7.2 v2 设计（进行中）

- 采样阶段**剔除污染前缀**（含 `[协议错误]`/`[引擎拒绝]` tool 消息的回合起点），
  报告剔除数：smoke-flash-ancient 2、**memory-regression 110/121**（300 轮长局后半程
  几乎全被 flash 时代协议错误污染，与离线基线 5 提示/11 错误一致）、其余源 0。
- 输入**带状态栏**（`--with-status`，生产形态）。
- 采样池同步剔除 `baseline-checkpoint.json`：A-2 修复前旧格式（状态栏快照是无 name 的
  user 消息，109K 前缀且 find_turn_cut 失效），边界语义不成立。
- v2 规模：60 前缀（ancient 12 + xianxia 30 + urban 9 + baseline_probe 9），max 22K
  token 全在 32K 窗内；truncate_prefix 加了边界规则兜底切点。
- 成本：v1 flash ¥1.006（cache 命中 84.5%）+ v2 ¥0.602（cache 命中 96%）≈ **Phase 0
  ICR 全期 ≈¥1.6**。v1 报告 `reports/icr-20260911-153158.json` 保留为"无状态栏形态"
  数据，65 对盲评池继续有效；v2 报告 `reports/icr-20260911-161425.json`（42 对池）。

### 7.3 v2 结果与 reasoning_content bias（方法论级发现）

| A 轴（60 前缀） | flash | local-14b |
| --- | --- | --- |
| ok | 58/60 | 44/60 |
| 一次通过率 | **8.6%**（v1 13.4%） | **36.4%**（v1 42.4%） |
| 熔断 | 2 | 16 |
| 参数非法 / 脏文本 / 截断 | 5 / 1 / 4 | 5 / 2 / 2 |
| 均时 | 12.01s | 13.43s |

**"带状态栏更差"推翻了形态 artifact 假设，真正的 bias 是 `reasoning_content` 缺失**：
DeepSeek V4 携带 tools 时要求回传思考内容（生产每回合实时往返都带）；存档不保留该字段，
ICR 重放输入缺它 → flash 协议遵从崩溃（8.6-13.4% vs 生产零协议失败）。Qwen AWQ 无
thinking 输出、不依赖该字段 → **14B 的 ICR 数字（36-42%）跨两形态稳定、可靠**。
推论：① 生产形态 A 轴以 smoke 对照为准（阶段 3/4）；② 文档 §7.4 的 ICR 对拍设计在
teacher 带思维链时对 teacher 不公平，Phase 2 盲评需注明产物形态差异；
③ 重放输入的三要素（reasoning_content / 状态栏同步 / 协议错误残迹）要全清单对照。

### 7.4 决策门 0 判定

按文档 §5.4：B 轴未全部达标（judge 差 6-17pp、dedup 达标）、A 轴生产形态不达标 →
**预期路径：Phase 1 窄模块蒸馏（judge/extract/dedup/reflect/compress），Phase 2 再决定
主回合去留**。完整五轴初值与对账见 `reports/local14b-p0-20260911.md`。

---

## 8. Phase 1 前奏：compress / reflect 零样本补测（2026-09-11 晚）

工具：`scripts/phase1_probe.py`（样本从存档构造生产同款输入，双模型同输入对照；
产出 `reports/phase1-probe-20260911-{165644,165812}.json`）。目的：Phase 0 没测到的
两个窄模块先零样本摸底，决定训不训、数据怎么收。

### 8.1 compress：会写但记不全 → 纳入训练

5 个样本（2 增量合并 + 3 首压，含 20K 长输入），指标 = 长度合规（≤800 目标）+
关键实体保全率（数字/专名/约定类关键词，规则抽取）：

| 模型 | 结构合格 | 保全率范围 | 观察 |
| --- | --- | --- | --- |
| flash | 5/5（首轮有 1 个偶发空响应，重跑恢复） | 0.47~0.89 | 800 字目标是软约束（输出 818-1180 字居多） |
| 14B | 4/5 | **0.23~0.67** | 长输入（20K）最弱（0.27）；1 个失败样本在脏输入（A-2 前状态栏快照档）上复述提示词——样本源已换干净档 |

判定：**要训**，差距在保全率（关键数字/承诺/名字），重点补长输入。
数据：轨迹压缩前后对 + flash 补标（§6.2 预算 200-500 对）。

### 8.2 reflect：flash 空响应的根因实锤（预算被思考吃光，能力未退化）

3 个样本（沈清秋记忆桶切片 12/8/10 条，真实长局记忆）：

| 模型 | 结果 |
| --- | --- |
| flash | **连续 6 次空响应**（两轮 × 3 样本） |
| 14B | 1/3 产出**切题且格式合法**的洞察；2/3 诚实输出「无」（不编造） |

**根因（裸调对照实验实锤，与 §5.3 dedup 同源）**：引擎 reflect 预算
`REFLECT_MAX_TOKENS=200`。v4-flash 思考模式把 200 token 全吃光：
`finish_reason=length` + `content=''` + `reasoning_content` 完整思考文本；
**放宽到 2000 预算后 flash 正常产出两条高质量洞察**（格式与来源编号全对）。
即 flash 的反思能力没有退化——是预算参数与新一代模型行为不匹配。

判定修正：**材料可用、flash 对照可用（修预算后）**；reflect 处置仍维持
训练最低优先级（14B 零样本已能产出合法洞察）；引擎侧建议把空响应升级重试
推广到 reflect（同 §5.3 的产品立项建议）。

### 8.3 五个窄模块最终处置清单（补测后）

| 模块 | 处置 |
| --- | --- |
| extract 提炼事实 | **必训**（railed 0/3 实锤，§6.5） |
| judge 审查员 | **小补强**（漏抓 6-17pp，§6.1） |
| compress 摘要 | **要训**（保全率 0.23-0.67，§8.1） |
| dedup 判重复 | **不训**（100% 满分，留回归测试集，§6.2） |
| reflect 反思 | **待定/最低优先级**（§8.2） |

---

## 9. 数据与资产状态

- **资产备份的权威 = git**（a9064f5 已 track saves/reports 全部历史档，`git checkout --
  <path>` 可还原任何被覆盖的历史档）。`/tmp/saves.archive-flash` 快照曾作为额外保险，
  但**随会话重启被清空**（教训见踩坑清单 #10）——阶段 3 的 flash condition 重跑版已用
  `saves/condition-<pack>.flash0908.{json,txt}` 后缀副本保护；阶段 4 的 14B condition
  重跑覆盖了 saves 内的 flash 版，原版仍在前述后缀副本与 git 中。
- 工作区改动：**截至 2026-09-11 已全部入库**（阶段 3/4 产物 → `6899fae`；Phase 1 前奏补测 →
  `0e01b77`；星环之下导入链产物 → `10a9f94`）——本节曾列出的 `scripts/worldpack_smoke.py`、
  `scripts/icr_sampling.py`、`scripts/diag_turn.py`、`saves/smoke-*.{json,txt,jsonl}`、
  `saves/condition-*.flash0908.*`、`saves/local14b-baseline-report.*`、`saves/railed-fact-check.json`、
  `reports/usage-*.jsonl` 等现均在 git 内（`git checkout -- <path>` 可还原任何被覆盖的历史档）；
  工作区当前干净（`git status` 零改动）。
- vLLM 服务：后台运行中（Qwen2.5-14B-Instruct-AWQ，32K 窗，prefix caching，端口 8000；
  会话重启会杀死后台任务，复跑前需 `curl /v1/models` + `nvidia-smi` 双确认）。
- flash API key 已验证可用（最小调用 87+8 token）。
- 诊断脚本 `scripts/diag_turn.py`（create 拦截器，支持 --action act/say/pick0）已入仓。

---

## 10. 对后续阶段（Phase 1）的修正与预判

1. **训练数据分层构造**（对"轨迹数据题材太窄"顾虑的对策，§8.3 处置清单配套）：
   - **主干 = 世界包 YAML 程序合成**（extract/judge 可批量生成任意题材训练对，
     零 API 成本、标准答案无噪音；5 个包的 YAML 即 5 种题材）；
   - **轨迹数据限量当"生产锚"**（20-30% 混比，跨包混合）；
   - **flash 补标只用于合成前缀**（compress/reflect 需要真实叙事流的模块）；
   - 合成对先让 flash 考一遍验证标准答案可达（flash 答不出的合成题没有训练价值）。
2. **训练顺序**：extract（必训）→ judge（补强）→ compress（要训）→ dedup（不训，
   留回归集）→ reflect（最低优先级，材料重造后定）。
3. **泛化验证加档**：文档 §7.3 的 urban_neon 剔除包之外，再加一个训练集未见过的
   临时中性小包（init-worldpack 生成）作最终试金石。
4. **flash 小预算侧信道的预算 bug**（思考模式吃光预算，§5.3/§8.2 实锤）：
   - 所有"以 flash 为标准答案"的补标环节一律放大 max_tokens（≥2000）再采标；
   - 建议引擎侧立项：空响应升级重试从 judge 推广到 dedup/reflect/extract；
     ✅ **2026-09-11 已落地**：`game_agent/budgets.py` 成为预算单一真源
     （`MIN_CALL_TOKENS=500` 规则 + `EMPTY_RETRY_TOKENS=2000`），升级重试由
     `complete_with_empty_retry` 统一实现并推广到 dedup/reflect/extract（judge 改走同一实现）；
     顺带修掉死常量 `REFLECT_MAX_TOKENS`（定义在 memory.py、调用点却用字面量 200，改常量不生效）
     与 dedup/reflect/extract 预算低于规则下限的问题（50/200/400 → 500）。回归：
     `tests/test_sidechannel_budget.py` 5 例 + 全量 **262 passed**。注意：离线假客户端恒返回空，
     故离线档侧信道调用计数会翻倍（longrun 报告 `extract` 15 → 30），属预期行为。
   - 修复后 flash 对照线需重测（dedup 大概率恢复 80%+、reflect 恢复产出）——**待跑**。
5. **测量纪律沿用**：flash 命令不带任何 `DEEPSEEK_*` shell 前缀；14B 命令显式带
   `DEEPSEEK_BASE_URL=http://127.0.0.1:8000/v1 DEEPSEEK_MODEL=local-14b DEEPSEEK_API_KEY=sk-local`。

---

## 11. 踩坑清单（现象 → 根因 → 预防）

| # | 现象 | 根因 | 预防 |
| --- | --- | --- | --- |
| 1 | `docker pull` 报错但命令 exit 0 | `\| tail -N` 管道掩盖真实退出码（tail 的 exit 0 成为管道末级） | 后台任务关键下载用 `set -o pipefail` 或事后校验产物落盘（模型下载也靠 du 复核） |
| 2 | `uvx --from huggingface_hub huggingface-cli download` 跑成了 `hf --help`，"下载成功"但 76 字节 symlink | uvx 解析到的入口不是预期命令；symlink 是快照层，真实 blob 在 `blobs/` | 模型下载后必须 `du -sh` 验证 blobs 总体积（9.4G 才可信） |
| 3 | vLLM 启动 "driver too old (found 12080)" | PyPI 默认 wheel 的 CUDA 版本高于驱动支持 | 装前对齐驱动 CUDA ↔ vLLM/torch wheel CUDA；装后 `torch.zeros(1).cuda()` 真机探针 |
| 4 | EngineCore 找不到 ninja | 绝对路径启动服务，子进程继承的 PATH 缺 venv bin | 起服务前 `export PATH=<venv>/bin:$PATH` |
| 5 | sampler warmup OOM | 0.92 显存利用对 32K 窗过激 | 4090 + 32K + fp8 KV 档用 0.85 + `--max-num-seqs 64` |
| 6 | 重启服务失败但 curl 仍通 | 旧进程占 GPU 与端口，新进程死于显存不足 | 起新前 TaskStop + `nvidia-smi` 归零 + 端口释放双确认 |
| 7 | 工具调用 400 | vLLM 默认不开 auto tool choice | `--enable-auto-tool-choice --tool-call-parser hermes` |
| 8 | ICR 旧档 KeyError | M2a 时代存档缺 `pack_name` 字段，兜底只捕 ValueError | 兼容兜底捕获 `(ValueError, KeyError)`；dry-run 先用真实存档跑通再花钱 |
| 9 | 14B 熔断难复现 | 温度 1.0 方差 + 失败模式间歇性 | 诊断走 create 拦截器逐次打原始返回；下结论前区分「模型能力」与「接入问题」（三类失败区分纪律） |
| 10 | `/tmp` 归档随会话重启消失 | 会话重启清理 /tmp；跨会话资产放 /tmp 无持久性 | 跨会话备份以 git track 为权威；临时快照放仓库外永久目录（如 ~/）；复盘文档如实更新而非依赖已消失的路径 |
| 11 | flash 门禁重跑结论行被 `tail` 截断 | 后台任务输出每段只保留 tail N 行，判定行在管道中被丢弃 | 结论类输出落盘到产物文件（txt/json），核验以产物为准；关键 run 不经过 tail 或显式 grep 判定行 |
| 12 | 14B 间歇性熔断难定位 | 坏 JSON 在 vLLM 解析器层被静默丢弃，引擎侧只见「无工具调用」 | 熔断诊断三件套：diag_turn 拦截器（模型原始输出）+ vLLM 服务日志（解析器报错）+ 引擎 history（重试提示）；三者对照才能区分「模型没调工具」与「调了但被解析器丢弃」 |
| 13 | ICR 重放时本地端点报 400 `Unterminated string`，flash 端点却不报 | 历史轨迹里的 flash 坏 JSON tool_calls（协议失败段残迹）——vLLM 模板严格解析历史 tool 消息，DeepSeek 端点宽容 | 重放/训练输入都要按文档 §7.2 清洗黑名单过滤（含 `[协议错误]` 段的前缀）；「同输入对照」若一方端点报 400，先怀疑输入污染而非模型能力 |
| 14 | 无状态栏重放把 flash 拉出生产分布（一次通过率 13.4% vs 生产零协议失败） | ICR 默认关状态栏防「未来事实泄漏」，但生产输入恒带状态栏 | A 轴（协议）测量用生产形态（--with-status，泄漏对协议轴无影响）；D 轴（质量）盲评池保持无状态栏防泄漏——两轴分开选择输入形态 |
| 15 | flash 小预算侧信道（dedup 50/reflect 200）空响应"退化" | v4-flash 换代后带思考模式，触发不稳定；思考吃光 max_tokens → content 空 → 静默降级路径误判（dedup 空=不重复→放行） | 怀疑"模型退化"前先裸调看 `finish_reason`/`reasoning_content`/`usage.completion_tokens` 三件套——预算顶满+content 空 = 预算 bug 不是能力 bug；补标调用一律放大 max_tokens |

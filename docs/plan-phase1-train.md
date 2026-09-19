# Phase 1 · 侧信道 LoRA 训练执行手册（⑤ 训练 / ⑥ 评测交接）

> **上游**：`docs/plan-phase1-data.md` §5（训练配方：双 LoRA、混比 4:3:2）、
> §6.2/§6.3（指标判据与回归护栏）、§6.4（报告 schema）；`docs/plan-local-14b.md` 附录 D（部署）。
> **本文档定位**：把"数据已就绪"推进到"**训出可评测的 LoRA 并自检通过**"。
> 判据本身不在这里复述 —— 一切"达标/不达标"以 plan-phase1-data §6 为准，本文只写**怎么跑、看什么、怎么算过**。
> **本文档不含任何凭据**：远程执行走 `scripts/remote_run.py`，主机/用户/密码全在环境变量里。

---

## 0. 一句话现状（2026-09-18 实测）

| 项 | 状态 |
| --- | --- |
| 数据 | ✅ `data/training/` 9 个 jsonl —— 训练 2466 / 留出 136 / 验证（dev 层）2663，`dataset_sha256 b61b2feadd4a826b` |
| 机器 | ✅ 2× A800-SXM4-80GB（**NVLink 未启用**，拓扑 PHB）、32 核、472 GB 内存、磁盘可用 124 GB |
| 环境 | ✅ `/home/ubuntu/venv`：torch 2.6.0+cu124 / transformers 5.17.0 / peft 0.20.0 / accelerate 1.15.0 / bitsandbytes 0.50.2 / datasets 5.0.1 / trl 1.13.0；`cuda=True 卡数=2 bf16=True` |
| 仓库 | ✅ `/home/ubuntu/game_agent` 已是 git 仓库（跟踪 `origin/main`，公开仓库 ⇒ 免凭据 `git pull`） |
| 权重 | ⏳ **Qwen3-14B 下载中**（bf16，29.55 GB / 18 个文件） |
| 训练脚本 | ❌ **未写**（本轮要交付的第一件东西，见 §4） |

---

## 1. 决策 38：底座换成 `Qwen/Qwen3-14B`（bf16）

**决定**：Phase 1 侧信道 LoRA 的底座由 `Qwen/Qwen2.5-14B-Instruct` 改为 **`Qwen/Qwen3-14B`（bf16）**。

**为什么可以换**：`plan-local-14b` 原文就写着 *"Qwen3-14B（默认带 thinking，需显式关闭）与长窗变体
**留到多卡/云对照阶段**"* —— 现在正是多卡阶段（2× A800）。且实测 Qwen3-14B
`max_position_embeddings = 40960`（比 32K 更宽），而我们的最长样本 14,805 字符（≈10K token），**零截断**。

**为什么是 bf16 而不是 AWQ**：AWQ 是**离线量化好的推理格式**，QLoRA 的标准链路
（peft + bitsandbytes）是加载 bf16/fp16 权重时**现场**量化，不认 AWQ 权重
（[peft#2745](https://github.com/huggingface/peft/issues/2745) 至今是未实现的 feature request）。
AWQ + LoRA 是**推理侧**组合（vLLM 支持在量化底座上挂 adapter）。
⇒ 训练下 `Qwen/Qwen3-14B`（29.55 GB）；AWQ（9.99 GB）**可选**，只为 ⑥ 评测更快更省显存
—— 80 GB 卡上并非必需。

**代价（必须一起认）**：

1. **thinking 必须显式关，且要在训练/服务两侧保持一致**（机制见 §2）——这是本轮最大的踩坑面；
2. **基线要重测**：现有 14B 基线是在 `Qwen2.5-14B-Instruct-AWQ` 上测的
   （`reports/local14b-p0-20260911.md`、`reports/extract-task10-report.md`），
   换底座 = 换基线（纪律："换尺子不换两侧 = 假对比"）；
3. `plan-local-14b` §3 选 Qwen2.5 的理由之一"**无 thinking 协议**"作废，§2 的处置随之改写。

**回退**：若 §2 的关 thinking 在 30 分钟内无法验证成功，**立即回退 Qwen2.5-14B-Instruct**
（下载 29.55 GB，旧基线可直接复用）——不在底座上硬耗。

---

## 2. 关键工程点：关 thinking（先做这个，再碰训练）

### 2.1 机制差异（这是坑的根源）

| 链路 | 关思考的机制 |
| --- | --- |
| 云端 DeepSeek（**现在的生产路径**） | `extra_body = {"thinking": {"type": "disabled"}}`（`game_agent/llm.py:333`） |
| **本地 Qwen3 + vLLM** | `chat_template_kwargs = {"enable_thinking": false}`（vLLM 透传给 tokenizer 的 chat template） |

`{"thinking": ...}` 对 Qwen3 是**无效字段**（vLLM 会忽略），所以"引擎已经在关思考了"是**错觉** ——
这正是 `plan-local-14b` 记下的坑：*"引擎 SDK 调用未传 chat_template_kwargs"*。

### 2.2 怎么做（两侧一致）

**服务侧（一劳永逸）**：起 vLLM 时加默认 kwargs，**所有**调用方（含引擎）都自动是关思考的：

```bash
vllm serve /home/ubuntu/models/Qwen3-14B \
  --served-model-name local-14b --max-model-len 32768 \
  --gpu-memory-utilization 0.90 --enable-prefix-caching \
  --default-chat-template-kwargs '{"enable_thinking": false}'
```

> ⚠️ `--default-chat-template-kwargs` 这个旗标**要先在那台机器上 `vllm serve --help | grep -i chat` 确认**
> （vllm 0.29.0 应支持，但"应该"不算数）。不支持就走引擎侧：
> `game_agent/llm.py` 的 `complete_with_meta` 在本地模型时追加
> `extra_body["chat_template_kwargs"] = {"enable_thinking": False}`。

**训练侧（分布必须一致）**：渲染样本时用**同一个 tokenizer 与同一个 kwarg**：

```python
text = tokenizer.apply_chat_template(
    row["messages"], tokenize=False, add_generation_prompt=False,
    enable_thinking=False,          # ← 与服务的渲染**逐字对齐**，否则训练/推理分布不一致
)
```

⚠️ Qwen3 在 `enable_thinking=False` 时，模板会在 assistant 前缀里塞一个**空的 think 块**；
因此**损失只算答案段**（`train_on_responses_only` 或等价做法），答案段里**不得出现 ` thinking`**。

### 2.3 守卫（三条，缺一不可）

1. **渲染守卫**：数据准备时断言 —— 渲染串含预期的 assistant 前缀，且**答案段无 ` thinking`**；
   随机 20 条人工可读的样本落盘供抽查。
2. **服务守卫**：起服务后 `curl` 一次，断言返回**不以 ` thinking` 开头**。
3. **端到端守卫**（最硬的一条）：走**引擎真实路径**（`DEEPSEEK_BASE_URL` 指向本地 vLLM）
   跑 3 条 judge + 3 条 extract，断言 `parse_verdict` / `parse_facts` **能正确吃下**输出
   （判官不得返回 `None`）。**解析失败率必须为 0**（plan-phase1-data §6.3 第 4 条）。

---

## 3. 训练配方（对齐 plan-phase1-data §5）

| 项 | 取值 | 依据 |
| --- | --- | --- |
| 底座 | `Qwen/Qwen3-14B`（bf16，本地路径 `/home/ubuntu/models/Qwen3-14B`） | 决策 38 |
| 适配器 | **本轮只训"侧信道组"一个 LoRA**（extract/judge/compress 合并多任务）；主回合 LoRA 属 Phase 2 | §5 方案 A |
| 混比 | `extract : judge : compress = 4 : 3 : 2`（**采样权重**，见 `data/training/manifest.json`） | §5 |
| 序列长度 | **16384**（覆盖 100% 样本：最长 14,805 字符；compress p95 12,655） | 实测 |
| packing | 开（≈334 个 packed 序列/epoch） | 提吞吐 |
| LoRA | r=16, alpha=32, dropout=0.05, target = 全部线性层（q/k/v/o + gate/up/down） | 常规起点 |
| 精度 | bf16（实测 `bf16=True`），**不用 QLoRA**（80 GB 卡上没必要，还慢） | §2 显存账 |
| 梯度检查点 | 开 | 长序列省显存 |
| 优化器 / lr | AdamW / **1e-4**，cosine，warmup 3% | LoRA 常规 |
| 有效批 | 目标 8~16 个 packed 序列（全局） | 见下 |
| epoch | **3 / 5 / 8 三档对照**（每档 ≈1~2.5 h，实测后定） | §5 早停纪律 |
| 早停依据 | **留出 136 条 + dev 2663 条**（轴值孪生，决策 19）；dev 不再降即停 | 决策 19 |
| 保存 | **只存 adapter**（几十~几百 MB），每 epoch + 最终各一份 | 便宜，便于对照 |
| 随机种子 | 固定并记录 | §5 可复现 |

**可复现字段（写进训练报告，§5 要求）**：`seed` / `epochs` / `lr` / `dataset_sha256`
（= `b61b2feadd4a826b`）/ 三条 prompt 指纹（`EXTRACT_SYSTEM` / `JUDGE_SYSTEM` / `COMPRESS_SYSTEM`，
在 `manifest.json` 的 `prompts` 里）/ 底座 `model_root` / `transformers`+`peft`+`trl` 版本号。

---

## 4. 待写的训练脚本（本轮第一件交付物）

新建 `scripts/train_sidechannel.py`（在那台机器上跑，本机不跑）。设计要求：

1. **数据**：读 `data/training/{module}.train.jsonl`，按 4:3:2 **加权采样**（不删数据、不改文件）；
   `dev` 作验证集；`holdout` 另作同分布留出。
2. **渲染**：`apply_chat_template(..., enable_thinking=False)` + **只对答案段算损失**（§2.2）。
3. **配置**：超参全部走 CLI/常量，**每条都写进训练报告**；默认值即 §3 表。
4. **产物**：`runs/<训练名>/` 下 —— `adapter/`（LoRA 权重）、`config.json`（全部超参）、
   `train_log.jsonl`（每步 loss/lr/**tok·s⁻¹**/显存峰值）、`holdout_metrics.json`、`README.md`（人读小结）。
5. **训练名含关键超参**（如 `qwen3-14b-lora-r16-ep5-4-3-2`），避免多组对照互相覆盖。
6. **不许静默降级**：OOM 就报错退出并打印当前配置（不许偷偷减 batch/截断序列）——
   "训练跑完了"不等于"按配置训练完了"。

---

## 5. Step 1：100 步试跑（**先做这个**，约 10 分钟）

**目的**：把估算法换成实测法 —— 量三个数，然后重算 ETA。

```bash
# 在本机（凭据走环境变量）：
uv run --quiet --with paramiko python scripts/remote_run.py \
    --cwd /home/ubuntu/game_agent --script scripts/train_sidechannel_smoke.sh
```

**要记录的三个数**（写进 `train_log.jsonl` 与报告）：

| 指标 | 用途 |
| --- | --- |
| **实测 tok/s** | 替换"1.5~2.5k tok/s/卡"这条假设 |
| **显存峰值** | 确认 16K 序列 + 有效批 8~16 放得下；定最终批大小 |
| **实测 token/epoch** | 替换"5.46 M 字符/epoch"的字符折算 |

**验收判据**：① 100 步无 OOM、无 NaN；② loss 明显下降；③ 输出**无 thinking 痕迹**（§2.3 守卫 1）；
④ 用实测 tok/s 重算 ETA 并**更新本手册 §3 的 epoch 档位**（估算 3/5/8 epoch ≈ 1~2.5 h/档）。

---

## 6. Step 2：正式训练

- 先跑 **5 epoch 一档**（居中的那档），看 dev 曲线决定是否加练到 8 或退回 3；
- 关掉交互、`nohup` 落日志（本机断线不影响）；每 epoch 存 adapter；
- 训练结束后立刻跑 §7 自检**再**决定要不要开下一档 —— 三档并行跑等于三倍电费买同一份信息。

---

## 7. Step 3：训练后自检（不通过就不进 ⑥）

| # | 检查 | 判据 |
| --- | --- | --- |
| 1 | **契约合规** | 三模块各抽 50 条，`parse_verdict` / `parse_facts` **失败率 0**；judge 不得返回"未知" |
| 2 | **thinking 未泄漏** | 生成串不含 ` thinking`（含空 think 块以外的任何 think 内容） |
| 3 | **holdout 指标** | 每模块 holdout loss/准确率对比**基座**（同一条尺子，换底座后必须重测） |
| 4 | **dev 泛化** | dev 层（2663 条，轴值孪生）不劣于 holdout 太多 —— 差距大就是窄化信号 |
| 5 | **回归护栏** | `worldpack_smoke` 两包通关（计划 §6.3 第 1 条：这是**部署配置检查**） |

---

## 8. Step 4：交接给 ⑥ 评测（本轮不做，先记清要重测什么）

换底座之后，**所有 14B 历史数字都不能直接复用**。⑥ 开跑前必须：

1. **重测基座基线**：同命令、同 seed、同 budget_policy，在 **Qwen3-14B（未训）** 上重跑
   `eval-sets/` 全套（原基线在 `reports/local14b-p0-20260911.md`，是 Qwen2.5-14B-AWQ）；
2. **三方对照**：基座 Qwen3-14B / 基座+LoRA / flash（flash 侧若语料未动可不重测，动过则两侧都重测）；
3. **分家族切片**（禁止只报家族平均分）：confab 的 **T1（纯缺席）/T2（语气冲突）分开报**、
   ooc、setting、**normal 误报率 ≤0.10**；
4. **报告 schema**（§6.4）里 `endpoint.model_root` 记 `Qwen/Qwen3-14B`、`max_model_len` 记实际窗口；
5. **数据守卫三连**：`eval_quota_check --gate` / `axis_coverage_check --gate` / `near_dup_check --gate`。

---

## 9. 风险与回退

| 风险 | 症状 | 处置 |
| --- | --- | --- |
| **thinking 关不掉** | 输出以 ` thinking` 开头、解析器返回"未知" | §2.2 换服务侧旗标 → 仍不行则引擎侧加 kwargs → 30 分钟内搞不定就**回退 Qwen2.5**（决策 38 回退条款） |
| **小数据过拟合** | holdout 早降、dev 反升 | 减 epoch；judge 材料复用率高（1018 种材料 / 1093 条缺陷样本，最多 1 份材料 13 条共用）⇒ 材料级记忆风险，看 dev 曲线 |
| **显存不足** | 16K + 有效批 16 OOM | 降有效批（**不许截断序列**）；仍不行开 QLoRA（4-bit，慢 1.5 倍） |
| **NVLink 缺失** | — | 只用 **DDP**（LoRA 梯度同步量小，无影响）；**不要**上张量并行 / ZeRO-3 |
| **LoRA 摸不到门禁** | judge T1 confab 仍低于零样本基线 | 先看是不是数据/提示词问题；确属容量不足才考虑**全参微调**（bf16+Adam ≈112 GB ⇒ 两张卡 ZeRO-2） |
| **判"此路不通"** | 契约合规修不好，或 T1 切片**低于零样本基线** | 停训，回头查数据（`reports/confab-triage-20260918.md` 的 172 条可疑档正好是候选） |

---

## 10. 命令速查

```bash
# 本机：远程执行（凭据在环境变量里，仓库不留凭据）
$env:DSH_SSH_HOST='...'; $env:DSH_SSH_USER='ubuntu'; $env:DSH_SSH_PW='...'
uv run --quiet --with paramiko python scripts/remote_run.py --script <本地脚本>

# 服务器侧常用
bash scripts/gpu_box_setup.sh      # 置备（幂等）
bash scripts/gpu_box_check.sh      # 起飞前检查：git / 数据 / 卡 / 包 / 权重
python -m scripts.scenario_factory.export_training   # 重建训练集（零成本；manifest 可核对）
```

**文件清单**：`data/training/*.jsonl`（9）+ `manifest.json`（回执：切分/权重/摘要/指纹）；
`docs/plan-phase1-data.md` §5/§6（判据真源）；`reports/confab-triage-20260918.md`（可疑档 172 条）。

---

## 附：进度清单

- [ ] 权重下载完成并核验（18 个文件 / 29.55 GB / `config.json` 的 `max_position_embeddings=40960`）
- [ ] §2.2 关 thinking 在**服务侧**验证通过（curl 返回不以 ` thinking` 开头）
- [ ] §2.3 端到端守卫通过（引擎真实路径，解析失败率 0）
- [ ] §4 训练脚本 `scripts/train_sidechannel.py` + smoke 脚本写完
- [ ] §5 100 步试跑：tok/s、显存峰值、token/epoch 三个数落档 + ETA 重算
- [ ] §6 5 epoch 正式训练完成，adapter 与 `train_log.jsonl` 落盘
- [ ] §7 自检五项全过（**契约合规 0 失败**是硬门）
- [ ] §8 交接 ⑥：Qwen3-14B 基座基线重测清单确认

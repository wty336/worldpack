# 训练环境排障记录（2026-09-19）

> 定位：从「§5 100 步试跑 OOM」到「5 epoch 正式训练起跑」的完整排障过程。
> **结论已固化进 `scripts/train_sidechannel.py`**（代码 + 守卫 + 注释），本文档是证据链与决策记录。
> 判据真源不变：`docs/plan-phase1-data.md` §5/§6。配方**一处未动**（底座 bf16 Qwen3-14B、
> r=16、4:3:2、seq 16384、有效批 4）——所有改动都是让环境兑现配方，不是改配方。

---

## 0. 一句话结论

- 16K bf16 LoRA 在 2×A800（80GB）上**完全装得下**：实测峰值 **45.6 GB**（余量 ~34 GB）、
  **2963 tok/s**（双卡合计）、**21.8 s/步**、75 步/epoch、5 epoch ≈ **2 小时 17 分**。
- 四次 OOM 与 LoRA 容量无关（可训练参数仅 64M）：是 **trl 1.13 + transformers 5.17**
  的四个环境级坑叠加，每个都有源码级证据与实测数字。

## 1. 四次 OOM 的完整因果链（按发现顺序）

### 坑 1：`warmup_ratio` 被 trl 1.13 移除（守卫赢，不是真 OOM）

试跑第一次退出（exit 2），脚本自己的 API 守卫响亮报错：

```
[✗] SFTConfig 不接受这些参数（trl 版本 API 不同）：SFTConfig.__init__() got an unexpected keyword argument 'warmup_ratio'
```

**处置**：按 `warmup_ratio × 总步数` 折算 `warmup_steps`（0.03 ⇒ 3% 语义不变），换算显式打印
（`[warmup] ... 折算 warmup_steps=3（3% × 100 步）`）。旧版 trl 两参都有，本地/服务器双端兼容。

### 坑 2：`packing` 默认策略 bfd 强制 padding-free，需要 Flash Attention（第一次真 OOM，exit 3）

trl 1.13 的 bfd 打包策略会**自动开启 padding-free**（把 batch 打平成单序列），官方告警原文：

```
[RANK 0] Padding-free training is enabled, but the attention implementation is not set to a
supported Flash Attention variant... Using other implementations may lead to unexpected behavior.
```

本机没有 flash-attn（torch 2.6+cu124 无预编译轮子，`pip install flash-attn` 源码编译失败——
缺 CUDA 工具链）。SDPA 在 padding-free 平铺的 16K 序列上回退慢路径，双卡 80GB 顶满。

**处置**：`packing_strategy="wrapped"`（旧 ConstantLengthDataset 语义：整池拼接切片）。
wrapped **不触发 padding-free**；SDPA 对普通 causal 16K 在 A800（sm_80）上正常走高效内核
（探针实测：合成张量 16384×40 头 GQA，默认派发峰值 0.7GB；强制 math 后端 OOM——反证高效内核在用）。
损失掩码由 `labels`（-100）携带，**不依赖序列边界**，wrapped 无损失。
且 4.84M/16384 ≈ 295 packs 与手册 §3.1 的步数估算口径一致。

### 坑 3：trl 1.13 的 TRLTrainer 不执行梯度检查点启用（第二次 OOM 的元凶之一）

显式打印证据（修复前）：

```
[检查点] is_gradient_checkpointing=False，0 个模块标志位开（期望 40 层 + 顶层 = 41）
```

`SFTConfig(gradient_checkpointing=True)` 构造后字段为 True（实测），但 **trl 1.13 的
TRLTrainer 里没有任何 `gradient_checkpointing_enable` 调用点**（grep 整个 trl/trainer 确认；
transformers Trainer 里的启用块在 trl 1.13 的继承链上不执行）。40 层激活全存 ≈ **108 GB**，
必然 OOM。

**处置**：trainer 建好后显式 `trainer.model.gradient_checkpointing_enable()`（经 peft 的
`__getattr__` 转发到 base model，探针验证 41 个标志位全开），并加**守卫**：开启后 0 个标志位
直接报错退出（不许静默降级）。trl 自己在 peft+检查点下会补 `enable_input_require_grads()`，
无需额外处理。

### 坑 4：**模型以 fp32 加载**（最大的坑；80.7GB 恒定峰值的来源）

修复前每次 OOM 的峰值**恒为 80.7 GB**——与检查点开不开、liger 开不开无关。这只有一种解释：
基线本身太重。铁证在 trl 源码 `sft_trainer.py` 的 docstring（837-839 行）：

```
If `dtype` is not specified in `args.model_init_kwargs`, it defaults to `float32`. This differs
from `PreTrainedModel.from_pretrained`, where (since Transformers v5) the dtype is inferred...
```

脚本没传 `model_init_kwargs` ⇒ 权重按 **fp32 加载 = 56 GB**（bf16 的 2 倍）。
`bf16=True` 只管训练 autocast，**不管加载**。加载路径：`model_init_kwargs = dict(args.model_init_kwargs or {})`
→ `create_model_from_path(model, **kwargs)`，无 dtype 关键字。

**处置**：`model_init_kwargs={"dtype": torch.bfloat16}`（transformers 5.17 的新参数名；
`torch_dtype` 已废弃）。修复后脚本自带打印验证：

```
[模型] 参数 dtype=torch.bfloat16（期望 bfloat16——fp32 会吃 56 GB 权重，16K 必 OOM）
```

修复后峰值 45.6GB，与探针预测 45.5GB 一致（差 0.1GB）。

## 2. 显存旋钮实测（16K bf16 LoRA fwd+bwd+step，单卡探针）

| 配置 | 显存峰值 | 单步耗时 | 结论 |
| --- | --- | --- | --- |
| 基线（全层检查点，reentrant） | 71.7 GB | 13.5s | 贴 80GB 天花板，trainer 开销一加就炸 |
| +`offload=True`（检查点边界张量进 472G 内存） | 65.2 GB | 14.0s | 省 6.5GB，代价 +0.5s/步 |
| **+liger（融合线性交叉熵）** | **45.5 GB** | 13.7s | **省 26.2GB 且不减速**——16K 的 logits 是 16384×151936（bf16 5GB + fp32 损失物化 10GB），liger 分块算交叉熵，两份都不落显存 |
| +offload + liger | 69.1 GB | 12.9s | **两者叠加互相干扰**，勿混用 |

liger-kernel 0.8.3 有 `apply_liger_kernel_to_qwen3`（`monkey_patch.py:1819`），transformers
5.17 的 Trainer 原生支持 `use_liger_kernel`（经 `transformers.integrations.liger`，自动 unwrap
peft 后打补丁），trl 1.13 的 `compute_loss` 是 liger 感知的（训练时传 `skip_logits`）。
rope 后端的 cutedsl/ascend 告警是良性的（回退默认 triton 后端）。

> 决策记录：用户明确「优先想办法降显存保住 **bf16 LoRA**，QLoRA 只是最后手段」——
> 最终零 QLoRA、零截断、零 seq_len 让步，纯靠根因修复 + 融合内核兑现配方。

## 3. 顺带修掉的配方接线缺陷：4:3:2 混比从未生效

排查中发现 `MixedSampler`（4:3:2 加权抽样 + DDP 交错分片）**只写了类和 13 条测试，从未传进
trainer**——训练吃的是自然混比（judge token 占 40% vs 配方 22%）。经用户确认后接线：
`build_weighted_pool()` 复用 MixedSampler 的单 epoch 抽样（最大余数法 + 小模块重复采样），
重建训练行池后再交给 packing。实测加权池：

```
[混比] 4:3:2 加权池（一个 epoch）：{'judge': 822, 'compress': 548, 'extract': 1096} 行 ≈ 4,841,103 token
```

4.84M token/epoch 与手册 §3.1 的 4.83M 估算吻合（差 0.2%）。逐 epoch 换序由 Trainer 采样器
负责（torch RandomSampler 按 seed+epoch 重播种，可复现）。新增 2 条守卫测试（配比精确值 +
确定性），共 **15 条全绿**。

## 4. 实测基准（30 步 smoke，2026-09-19 17:42-17:53）

| 指标 | 实测 | 手册估算 | 备注 |
| --- | --- | --- | --- |
| 显存峰值 | **45.6 GB**（分配）/ 59.2 GB（预留） | — | 余量 ~34GB |
| tok/s | **2963**（双卡合计，~1480/卡） | 2.5~4k/卡 | 偏下限：16K 长序列 + 检查点重算 |
| 每步耗时 | **21.8s**（中位；最大 67s 为首步 triton 编译） | — | 每卡 1 × 累积 2 = 4 packs/步 |
| 步数/epoch | **75**（375 步 = 5 epoch） | 74 | wrapped 打包切出 ~300 块 |
| loss 30 步 | 1.487 → 1.128 | 明显下降 ✓ | token 准确率 0.68→0.73 |
| ETA 5 epoch | **2 小时 17 分** | 1.7~2.7 h | 正式训练实测进度条 |

验收判据对照（手册 §5）：① 无 OOM/NaN ✓ ② loss 明显下降 ✓ ③ 无 thinking 痕迹（render
守卫全过）✓ ④ 实测值落档 ✓。

已知小噪音：DDP 报 `find_unused_parameters=True` 告警（accelerate 默认，每步多一次图遍历）。
不影响正确性，可在回顾时关掉。

## 5. 脚本改动清单（`scripts/train_sidechannel.py` + smoke + tests）

| 改动 | 理由 | 类型 |
| --- | --- | --- |
| `warmup_ratio` → 折算 `warmup_steps` | 坑 1 | API 适配 |
| `packing_strategy="wrapped"` | 坑 2 | 环境适配 |
| `model_init_kwargs={"dtype": torch.bfloat16}` | 坑 4 | 环境适配 |
| 显式 `gradient_checkpointing_enable()` + 0 标志位守卫 | 坑 3 | 环境适配 + 守卫 |
| `build_weighted_pool` 接线 4:3:2 | 混比缺陷 | 配方兑现 |
| 新旗标 `--liger` / `--grad-offload` / `--qlora`（显式、进 config.json、有 banner 打印） | 显存旋钮 | 显式开关 |
| OOM 处理器：打印峰值分配/预留 + `MEM_TRACE=1` 内存指纹 | 排障 | 诊断 |
| 模型 dtype 打印 + 检查点标志位打印 | 坑 3/4 的回归哨兵 | 诊断 |
| 守卫测试 13 → **15** 条（加权池配比精确值 + 确定性） | 混比接线 | 守卫 |

## 6. 对 ⑥ 评测的遗留影响

**无配方级影响**——底座、混比、r/alpha、lr、seq_len、有效批全部与手册一致，训练侧唯一
不同是损失由 liger 融合内核计算（数值等价，分块归约）。liger 只在训练脚本用，服务侧
（vLLM + adapter）不涉及。若⑥ 的基座基线（Qwen3-14B 未训）要用训练同款渲染，记得
`enable_thinking=False` 与服务侧一致（手册 §2 不变）。

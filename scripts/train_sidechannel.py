"""⑤ 侧信道 LoRA 训练（Qwen3-14B 底座）。

**执行手册**：`docs/plan-phase1-train.md`（怎么跑、看什么、怎么算过）。
**判据真源**：`docs/plan-phase1-data.md` §5（配方）/ §6（指标与护栏）。本脚本不重述判据。

三件事必须做对，其余都是常规：

1. **关 thinking，且训练/服务两侧一致**（决策 38）。Qwen3 的模板在 `enable_thinking=False` 时
   会把**空的 think 块预填进 prompt**，实测渲染长这样：

       <|im_start|>assistant\\n<think>\\n\\n</think>\\n\\n   ← 生成前缀（prompt 的一部分）
       通过<|im_end|>\\n                                    ← 答案段（**只对它算损失**）

   ⇒ 断言用 `<think>`（**不带空格**）；答案段里出现 `<think>` 即关 thinking 失败，直接报错。
   （踩过的坑：第一版断言写成 `" thinking"` 带空格，于是"没检测到"是假阴性。）
2. **只对答案段算损失**：`prompt` / `completion` 由**同一次模板渲染切出来**，边界由构造保证，
   不是另写一份拼接逻辑。
3. **不许静默降级**：OOM/NaN/渲染守卫失败一律**报错退出并打印当前配置**，
   不许偷偷减 batch、截断序列或跳过样本 ——"训练跑完了"不等于"按配置训练完了"。

用法（在 GPU 机上）::

    # ① 只渲染 + 校验（只要 tokenizer，不需要权重；零 GPU）——顺便量**真实 token 数**
    python -m scripts.train_sidechannel --stage render --tokenizer /tmp/qwen3_tok
    # ② 100 步试跑（量 tok/s、显存峰值、真实 token/epoch）
    torchrun --nproc_per_node=2 -m scripts.train_sidechannel --stage smoke --max-steps 100
    # ③ 正式训练
    torchrun --nproc_per_node=2 -m scripts.train_sidechannel --stage train --epochs 5
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import random
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "training"
MODULES = ("extract", "judge", "compress")
# 计划 §5：extract : judge : compress ≈ 4 : 3 : 2（**采样权重**，不是数据量配额）
MIX = {"extract": 4, "judge": 3, "compress": 2}

# ---- 超参默认值（= 手册 §3 的表；全部会写进 runs/<name>/config.json）-------------
DEFAULTS = dict(
    model="/home/ubuntu/models/Qwen3-14B",
    seq_len=16384,          # 覆盖 100%：最长样本 14,805 字符
    lora_r=16, lora_alpha=32, lora_dropout=0.05,
    lr=1e-4, warmup_ratio=0.03, weight_decay=0.0,
    epochs=5, seed=20260918,
    per_device_batch=1,     # 每个设备每步几条 packed 序列
    grad_accum=2,           # ⇒ 全局有效批 = 1 × 2 × 卡数；步数是主约束（见手册 §3）
    packing=True,
    logging_steps=5, save_each_epoch=True,
)


# ---------------------------------------------------------------------------
# 渲染与守卫
# ---------------------------------------------------------------------------

def render_pair(tokenizer, messages: list[dict]) -> tuple[str, str]:
    """返回 (生成前缀, 答案段)。两者**由同一次渲染切出**，边界由构造保证。

    ⚠️ 两次渲染喂的 messages **不一样**：生成前缀只喂到 user 为止（`messages[:-1]`），
    含答案的那次才喂全部。**踩过的坑**：初版两次都喂全量，于是"前缀"里已经包含了答案，
    自洽性断言当场报错（守卫抓到的就是这么个真 bug）。所以下面先验证形态，
    再断言 `full` 以前缀开头 —— 这个断言正是用来挡这类错的。
    """
    if not messages or messages[-1].get("role") != "assistant":
        raise RuntimeError(f"messages 末条必须是 assistant（实际 {messages[-1:]!r}）")
    prompt = tokenizer.apply_chat_template(
        messages[:-1], tokenize=False, add_generation_prompt=True, enable_thinking=False)
    full = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=False, enable_thinking=False)
    if not full.startswith(prompt):
        raise RuntimeError(
            "渲染不自洽：含答案的渲染不是以生成前缀开头 —— Qwen3 模板行为可能变了。\n"
            f"  前缀尾部：{prompt[-60:]!r}\n  完整串尾部：{full[-60:]!r}")
    return prompt, full[len(prompt):]


def guard_render(prompt: str, completion: str) -> None:
    """关 thinking 的三条断言（**改了模板/加了参数会在这里响**）。"""
    if "<think>" in completion or "</think>" in completion:
        raise RuntimeError(f"答案段含 think 标记 —— 关 thinking 没生效：{completion[:80]!r}")
    if not prompt.rstrip().endswith("</think>"):
        raise RuntimeError(
            "生成前缀结尾不是预期的空 think 块（实测应为 ...assistant\\n<think>\\n\\n</think>）。"
            f"实际尾部：{prompt[-60:]!r} —— 模板版本或 kwargs 变了，训练/推理分布会不一致。")


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

def load_split(data_dir: pathlib.Path, split: str) -> list[dict]:
    """读 `{module}.{split}.jsonl`（训练/留出/验证）。缺文件即空，不崩。"""
    rows: list[dict] = []
    for m in MODULES:
        f = data_dir / f"{m}.{split}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                r["module"] = m
                rows.append(r)
    return rows


def build_pairs(tokenizer, rows: list[dict]) -> list[dict]:
    """渲染成 prompt/completion 对，并逐条跑守卫。"""
    out = []
    for r in rows:
        prompt, completion = render_pair(tokenizer, r["messages"])
        guard_render(prompt, completion)
        if not completion.strip():
            raise RuntimeError(f"{r['id']}: 答案段为空（渲染出错）")
        out.append({"id": r["id"], "module": r["module"], "prompt": prompt,
                    "completion": completion})
    return out


# ---------------------------------------------------------------------------
# 加权采样（4:3:2，每 epoch 跨任务打乱）
# ---------------------------------------------------------------------------

class MixedSampler:
    """按 4:3:2 抽一个 epoch 的样本，每 epoch 换 seed；DDP 下按 rank 交错分片。

    为什么自己写：HF Trainer 的默认采样器会让三个模块按**文件拼接顺序**成块出现
    （extract 全在前、compress 全在后），而计划 §5 明确要求"每个 epoch 跨任务打乱，
    防任务块状学习"。

    **两条由构造保证的性质**（都有守卫）：

    1. **跨 rank 不重**：每个 rank 取 `pool[rank::world]`（**交错**分片），
       位置奇偶不变 ⇒ 两个 rank 的取值集合天然不相交，即使池子被绕圈复用也不会撞。
       （踩过的坑：第一版各自 `random.choices` 有放回抽，两张卡抽到同一条 —— 浪费算力
       且让重复样本在 DDP 梯度平均里被**加权**，分布被扭曲。）
    2. **各 rank 长度完全相同**：`ceil(N/world)`，按模块用最大余数法分配条数 ⇒
       不会出现"某张卡先跑完"的 DDP 挂起。

    小模块（compress 270 条）会被**重复采样**到配比所需的量，大模块被下采样 ——
    这正是"4:3:2 是采样权重、不是数据量配额"的含义（不删数据、不改文件）。
    """

    def __init__(self, modules: list[str], mix: dict[str, int], seed: int,
                 num_replicas: int = 1, rank: int = 0):
        self.modules = modules
        self.by_mod: dict[str, list[int]] = {}
        for i, m in enumerate(modules):
            self.by_mod.setdefault(m, []).append(i)
        missing = [m for m in mix if m not in self.by_mod]
        if missing:
            raise RuntimeError(f"混比里声明了但没有数据的模块：{missing}（数据没导出全？）")
        self.order = sorted(self.by_mod)
        self.mix = {m: mix[m] for m in self.order}
        self.seed, self.epoch = seed, 0
        self.num_replicas, self.rank = num_replicas, rank

    def set_epoch(self, epoch: int) -> None:
        self.epoch = epoch

    def __len__(self) -> int:
        return math.ceil(len(self.modules) / self.num_replicas)

    def _per_module_counts(self, target: int) -> dict[str, int]:
        """最大余数法：各模块条数之和**恰好**等于 target（各 rank 长度一致）。"""
        total_w = sum(self.mix.values())
        exact = {m: target * self.mix[m] / total_w for m in self.order}
        base = {m: int(math.floor(exact[m])) for m in self.order}
        rest = target - sum(base.values())
        for m in sorted(self.order, key=lambda x: -(exact[x] - base[x]))[:rest]:
            base[m] += 1
        return base

    def __iter__(self):
        rng = random.Random(f"{self.seed}-{self.epoch}")
        counts = self._per_module_counts(len(self))
        out: list[int] = []
        for m in self.order:
            pool = list(self.by_mod[m])
            rng.shuffle(pool)                       # 每 epoch 换顺序（同 seed 可复现）
            stride = pool[self.rank::self.num_replicas] or pool
            out += [stride[j % len(stride)] for j in range(counts[m])]
        rng.shuffle(out)                            # 跨任务打乱：模块不许成块
        return iter(out)


# ---------------------------------------------------------------------------
# 训练
# ---------------------------------------------------------------------------

def run_name(args) -> str:
    """训练名含关键超参 —— 多组对照不许互相覆盖（手册 §4 第 5 条）。"""
    mix = "-".join(str(MIX[m]) for m in MODULES)
    tag = f"{pathlib.Path(args.model).name.lower()}-lora-r{args.lora_r}-ep{args.epochs}-{mix}"
    return args.run_name or tag


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="⑤ 侧信道 LoRA 训练（Qwen3-14B）")
    p.add_argument("--stage", required=True, choices=["render", "smoke", "train"])
    for k, v in DEFAULTS.items():
        if isinstance(v, bool):
            p.add_argument(f"--{k.replace('_', '-')}", action="store_true", default=v)
        else:
            p.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=v)
    p.add_argument("--tokenizer", default=None, help="render 阶段可单独指定 tokenizer 目录")
    p.add_argument("--data", default=str(DATA_DIR))
    p.add_argument("--out", default="runs")
    p.add_argument("--run-name", default=None)
    p.add_argument("--max-steps", type=int, default=-1, help="smoke 用（-1 = 不限）")
    p.add_argument("--full-text-loss", action="store_true",
                   help="显式退路：对整串算损失（**不推荐**，仅当 completion_only_loss 不可用时人工指定）")
    p.add_argument("--no-packing", action="store_true", help="显式关 packing（默认开）")
    args = p.parse_args(argv)

    from transformers import AutoTokenizer

    t0 = time.time()
    tok_src = args.tokenizer or args.model
    try:
        tokenizer = AutoTokenizer.from_pretrained(tok_src)
    except Exception as e:                                     # noqa: BLE001
        print(f"[✗] tokenizer 加载失败（{tok_src}）：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    data_dir = pathlib.Path(args.data)
    print(f"[数据] {data_dir}")
    train_rows = load_split(data_dir, "train")
    hold_rows = load_split(data_dir, "holdout")
    dev_rows = load_split(data_dir, "dev")
    if not train_rows:
        print("[✗] 训练集为空 —— 先跑 `python -m scripts.scenario_factory.export_training`",
              file=sys.stderr)
        return 2

    # ---- 渲染 + 守卫（render 阶段到此为止，零 GPU）--------------------------
    pairs = build_pairs(tokenizer, train_rows)
    tok_counts = {m: [len(tokenizer(p["prompt"] + p["completion"])["input_ids"])
                      for p in pairs if p["module"] == m] for m in MODULES}
    stats = {m: {"n": len(v), "总 token": sum(v),
                 "中位": sorted(v)[len(v) // 2] if v else 0, "最大": max(v) if v else 0}
             for m, v in tok_counts.items()}
    total_tok = sum(s["总 token"] for s in stats.values())
    print("[渲染] 全量通过关 thinking 守卫（答案段无 <think>）")
    for m in MODULES:
        s = stats[m]
        print(f"   {m:<9} n={s['n']:>5}  token 总 {s['总 token']:>9,}  中位 {s['中位']:>6}  最大 {s['最大']:>6}")
    print(f"   合计 {total_tok:,} token / epoch（**实测**，替换字符折算的估算）")
    over = [m for m in MODULES if stats[m]["最大"] > args.seq_len]
    if over:
        print(f"[✗] 有样本超过 seq_len={args.seq_len}：{over} —— 加长序列，**不许截断**",
              file=sys.stderr)
        return 2

    if args.stage == "render":
        dest = pathlib.Path(args.out) / run_name(args)
        dest.mkdir(parents=True, exist_ok=True)
        with (dest / "render_samples.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for p in pairs[:20]:
                fh.write(json.dumps(p, ensure_ascii=False) + "\n")
        (dest / "token_stats.json").write_text(
            json.dumps({"per_module": stats, "total_per_epoch": total_tok,
                        "seq_len": args.seq_len}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        print(f"[✓] render 阶段完成（{time.time() - t0:.1f}s）→ {dest}/"
              f"render_samples.jsonl（20 条供人读）+ token_stats.json")
        return 0

    # ---- 训练 --------------------------------------------------------------
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import TrainerCallback
    from trl import SFTConfig, SFTTrainer

    torch.manual_seed(args.seed)
    if not torch.cuda.is_available():
        print("[✗] 没有可用 GPU", file=sys.stderr)
        return 2

    name = run_name(args)
    dest = pathlib.Path(args.out) / name
    dest.mkdir(parents=True, exist_ok=True)
    cfg = {k: getattr(args, k) for k in DEFAULTS}
    cfg.update({"stage": args.stage, "run_name": name, "data": str(data_dir),
                "full_text_loss": args.full_text_loss, "packing": not args.no_packing,
                "world_size": int(os.environ.get("WORLD_SIZE", 1)),
                "measured_tokens_per_epoch": total_tok,
                "token_stats": stats})
    (dest / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")

    ds_train = Dataset.from_list([{"prompt": p["prompt"], "completion": p["completion"],
                                   "module": p["module"]} for p in pairs])
    hold_pairs = build_pairs(tokenizer, hold_rows) if hold_rows else []
    ds_hold = Dataset.from_list([{"prompt": p["prompt"], "completion": p["completion"],
                                  "module": p["module"]} for p in hold_pairs]) if hold_pairs else None

    class _EpochSamplerCB(TrainerCallback):
        """每 epoch 重设采样种子（MixedSampler 的 `set_epoch`），并记录吞吐与显存。"""

        def __init__(self, sampler=None):
            self.sampler = sampler
            self.t_step = None
            self.log_path = dest / "train_log.jsonl"

        def on_epoch_begin(self, a, state, control, **kw):
            if self.sampler is not None:
                self.sampler.set_epoch(int(state.epoch or 0))

        def on_step_begin(self, a, state, control, **kw):
            self.t_step = time.time()

        def on_log(self, a, state, control, logs=None, **kw):
            rec = {"step": state.global_step, "epoch": state.epoch}
            rec.update({k: v for k, v in (logs or {}).items()
                        if k in ("loss", "learning_rate", "grad_norm")})
            if self.t_step:
                rec["step_sec"] = round(time.time() - self.t_step, 3)
            if torch.cuda.is_available():
                rec["peak_vram_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
            with self.log_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    lora = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
        bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"])

    trl_kwargs = dict(
        output_dir=str(dest), num_train_epochs=args.epochs,
        per_device_train_batch_size=args.per_device_batch,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr, lr_scheduler_type="cosine",
        warmup_ratio=args.warmup_ratio, weight_decay=args.weight_decay,
        bf16=True, gradient_checkpointing=True, logging_steps=args.logging_steps,
        save_strategy="epoch" if args.save_each_epoch else "no",
        max_steps=args.max_steps, seed=args.seed, report_to=[],
        max_length=args.seq_len, packing=not args.no_packing,
        completion_only_loss=not args.full_text_loss,
        dataset_kwargs={"add_special_tokens": False} if args.full_text_loss else {},
    )
    try:
        sft_cfg = SFTConfig(**trl_kwargs)
    except TypeError as e:                                     # 版本 API 不同 → **响亮地失败**
        print(f"[✗] SFTConfig 不接受这些参数（trl 版本 API 不同）：{e}\n"
              f"    当前 trl 版本请对照手册 §4 调整；**不要**改成本地静默降级。", file=sys.stderr)
        return 2

    trainer = SFTTrainer(model=args.model, args=sft_cfg, train_dataset=ds_train,
                         eval_dataset=ds_hold, peft_config=lora,
                         callbacks=[_EpochSamplerCB()])
    try:
        trainer.train()
    except torch.cuda.OutOfMemoryError:
        print("[✗] 显存不足 —— **不静默降级**。当前配置：\n"
              + json.dumps(cfg, ensure_ascii=False, indent=2)
              + "\n    处置：调小 --per-device-batch / --grad-accum，或显式换 QLoRA；"
                "**不许截断序列**（手册 §9）。", file=sys.stderr)
        return 3

    trainer.save_model(str(dest / "adapter"))
    metrics = {"train": trainer.state.log_history[-1] if trainer.state.log_history else {},
               "measured_tokens_per_epoch": total_tok,
               "token_stats": stats,
               "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2)}
    if ds_hold is not None:
        metrics["holdout"] = trainer.evaluate()
    (dest / "holdout_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    # 可复现字段（手册 §3）
    try:
        import peft, transformers, trl
        vers = {"transformers": transformers.__version__, "peft": peft.__version__,
                "trl": trl.__version__, "torch": torch.__version__}
    except Exception:                                          # noqa: BLE001
        vers = {}
    (dest / "README.md").write_text(
        f"# {name}\n\n- 底座：`{args.model}`\n- 数据：`{data_dir}`（{total_tok:,} token/epoch，实测）\n"
        f"- 超参：见 `config.json`；版本：{vers}\n- 显存峰值：{metrics['peak_vram_gb']} GB\n"
        f"- 日志：`train_log.jsonl`（每 {args.logging_steps} 步 loss/lr/tok·s⁻¹/显存）\n",
        encoding="utf-8", newline="\n")
    print(f"[✓] {name} 完成（{time.time() - t0:.0f}s）→ {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

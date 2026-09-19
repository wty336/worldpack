#!/usr/bin/env bash
# 100 步试跑（`docs/plan-phase1-train.md` §5）：把估算换成实测。
#
# 量三个数：① 实测 tok/s ② 显存峰值 ③ 真实 token/epoch
# 验收：无 OOM / 无 NaN / 输出无 thinking 痕迹 / 用实测值重算 ETA。
#
# 用法（在 GPU 机上，代码目录内）：
#   bash scripts/train_sidechannel_smoke.sh
# 环境变量可覆盖：MODEL / STEPS / PER_DEVICE / GRAD_ACCUM / NPROC
set -u
cd "$(dirname "$0")/.." || exit 1
PY=/home/ubuntu/venv/bin/python
MODEL="${MODEL:-/home/ubuntu/models/Qwen3-14B}"
STEPS="${STEPS:-100}"
NPROC="${NPROC:-2}"
PER_DEVICE="${PER_DEVICE:-1}"
GRAD_ACCUM="${GRAD_ACCUM:-2}"
OUT="${OUT:-runs}"

echo "=== 前置检查：权重齐了吗 ==="
if [ ! -f "$MODEL/config.json" ]; then
  echo "✗ 缺 $MODEL/config.json —— 模型还没下完？"; exit 2
fi
shards=$(ls "$MODEL"/model-*.safetensors 2>/dev/null | wc -l)
incomplete=$(ls "$MODEL"/*.incomplete 2>/dev/null | wc -l)
echo "分片 $shards / 未完成 $incomplete"
if [ "$incomplete" -ne 0 ]; then
  echo "✗ 还有 .incomplete 文件 —— **等下载完再跑**（不要拿半个模型试）"; exit 2
fi
if ! "$PY" -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('$MODEL')" 2>/dev/null; then
  echo "✗ tokenizer 加载失败"; exit 2
fi

echo
echo "=== ① render 阶段（零 GPU）：渲染守卫 + **实测 token 数** ==="
"$PY" -m scripts.train_sidechannel --stage render --model "$MODEL" --out "$OUT"

echo
echo "=== ② smoke 阶段：${STEPS} 步 × ${NPROC} 卡（每卡批 ${PER_DEVICE} × 累积 ${GRAD_ACCUM}）==="
EXTRA=""
[ "${QLORA:-}" = "1" ] && EXTRA="$EXTRA --qlora" && echo "（QLORA=1：显式 4-bit）"
[ "${LIGER:-}" = "1" ] && EXTRA="$EXTRA --liger" && echo "（LIGER=1：融合损失，16K 实测 −26 GB）"
[ "${OFFLOAD:-}" = "1" ] && EXTRA="$EXTRA --grad-offload" && echo "（OFFLOAD=1：检查点激活进内存）"
CUDA_VISIBLE_DEVICES=0,1 "$PY" -m torch.distributed.run --nproc_per_node="$NPROC" \
  -m scripts.train_sidechannel --stage smoke --model "$MODEL" --out "$OUT" \
  --max-steps "$STEPS" --per-device-batch "$PER_DEVICE" --grad-accum "$GRAD_ACCUM" $EXTRA

echo
echo "=== ③ 试跑小结（要把这三行抄进手册/训练报告）==="
RUN=$(ls -1t "$OUT" | head -1)
LOG="$OUT/$RUN/train_log.jsonl"
"$PY" - "$LOG" <<'PY'
import json, pathlib, sys
recs = [json.loads(x) for x in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if x.strip()]
if not recs:
    print("（train_log.jsonl 为空 —— 训练没跑到 log 步？）"); raise SystemExit
last = recs[-1]
secs = [r["step_sec"] for r in recs if r.get("step_sec")]
peak = max((r.get("peak_vram_gb") or 0) for r in recs)
loss0 = next((r["loss"] for r in recs if "loss" in r), None)
print(f"步数        {last['step']}")
print(f"最后 loss   {last.get('loss')}   （首个 {loss0}）")
print(f"每步耗时    中位 {sorted(secs)[len(secs)//2]:.2f}s / 最大 {max(secs):.2f}s" if secs else "无 step_sec")
print(f"显存峰值    {peak} GB")
print(f"日志        {sys.argv[1]}")
PY
echo
echo "下一步：用上面的「每步耗时」× 每 epoch 步数 重算 ETA，据此定 --epochs（手册 §5 验收判据④）"

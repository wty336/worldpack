#!/usr/bin/env bash
# GPU 机**起飞前检查**：训练/评测开跑前先跑这一条，确认机器处于预期状态。
#
# 为什么要有它：⑤ 训练、⑥ 评测都要在这台机器上跑，而"环境对不对"如果靠记忆，
# 就会在跑到一半时才发现（缺包 / 数据没同步 / 权重没下 / 卡被别的进程占着）。
# 一次 10 秒的检查换掉一次半小时的失败重跑。
#
# 用法：
#   uv run --quiet --with paramiko python scripts/remote_run.py --script scripts/gpu_box_check.sh
set -u
REPO="${GAME_AGENT_DIR:-/home/ubuntu/game_agent}"
VENV="${GAME_AGENT_VENV:-/home/ubuntu/venv}"
cd "$REPO" || { echo "✗ 目录不存在：$REPO"; exit 1; }

echo "=== git ==="
if [ -d .git ]; then
  echo "HEAD $(git rev-parse --short HEAD)  分支 $(git branch --show-current)  "
  echo "落后远端：$(git rev-list --count HEAD..origin/main 2>/dev/null || echo '?') 个提交"
  echo "本地改动/未跟踪：$(git status --porcelain | wc -l) 条"
else
  echo "✗ 不是 git 仓库（跑 scripts/gpu_box_setup.sh）"
fi

echo
echo "=== 训练数据 ==="
n=$(ls data/training/*.jsonl 2>/dev/null | wc -l)
echo "jsonl 文件数：$n（应为 9）"
if [ "$n" -eq 9 ]; then
  python3 -c "import json;d=json.load(open('data/training/manifest.json'));\
print('dataset_sha256', d['dataset_sha256'][:16], '（本地应为 b61b2feadd4a826b）')"
else
  echo "→ 缺数据：cd $REPO && $VENV/bin/python -m scripts.scenario_factory.export_training"
fi

echo
echo "=== GPU ==="
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader
busy=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader | wc -l)
echo "占用中的进程数：$busy（0 = 两张卡都空）"

echo
echo "=== 环境 ==="
"$VENV/bin/python" - <<'PY'
import importlib
for m in ("torch", "transformers", "peft", "accelerate", "bitsandbytes", "datasets", "trl"):
    try:
        mod = importlib.import_module(m)
        print(f"  {m:<14} {getattr(mod, '__version__', '?')}")
    except ImportError:
        print(f"  {m:<14} **缺**")
import torch
print(f"  cuda={torch.cuda.is_available()} 卡数={torch.cuda.device_count()} "
      f"bf16={torch.cuda.is_bf16_supported()}")
PY

echo
echo "=== 权重 ==="
ls -d /home/ubuntu/models/*/ 2>/dev/null | sed 's#.*/models/##' | tr -d '/' | tr '\n' ' '
echo
echo "（计划口径的基座 Qwen2.5-14B-Instruct 若不在上面，⑤ 前需先下载）"

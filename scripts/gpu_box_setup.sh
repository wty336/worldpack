#!/usr/bin/env bash
# GPU 机置备：把代码目录变成真正的 git 仓库 + 装训练侧缺的包。
#
# 背景（2026-09-18 实测）：`/home/ubuntu/game_agent` 最初是**文件拷贝**（无 `.git`），
# 于是没法 `git pull` 同步。本脚本做两件事：
#   ① `git init` + 指向 origin + `git fetch` + `reset --hard origin/main`
#      —— 工作区与远端该提交**逐字节一致**；**未跟踪文件（派生数据）不受影响**；
#   ② 装 `datasets` / `trl`（装前已用 `pip install --dry-run` 确认**不动**
#      torch / transformers / peft / accelerate，只加叶子包）。
#
# **凭据不在这里**：SSH 密码走环境变量 `DSH_SSH_PW`（见 `scripts/remote_run.py`）；
# 仓库是**公开**的，故服务器端 `git pull` 匿名即可，无需 token。
#
# 用法：
#   uv run --quiet --with paramiko python scripts/remote_run.py --script scripts/gpu_box_setup.sh
set -u
REPO="${GAME_AGENT_DIR:-/home/ubuntu/game_agent}"
URL="${GAME_AGENT_REMOTE:-https://github.com/wty336/worldpack.git}"
VENV="${GAME_AGENT_VENV:-/home/ubuntu/venv}"

cd "$REPO" || { echo "目录不存在：$REPO"; exit 1; }

echo "=== ① git 仓库化 ==="
if [ -d .git ]; then
  echo "已有 .git，跳过 init"
else
  git init -b main >/dev/null && echo "git init 完成"
fi
git remote remove origin 2>/dev/null || true
git remote add origin "$URL"
echo "remote: $(git remote get-url origin)"
git fetch origin main 2>&1 | tail -2
# reset --hard 只覆盖**已跟踪**路径；若目标是"本地有改动要保留"，别用本脚本
git reset --hard origin/main 2>&1 | tail -1
git branch --set-upstream-to=origin/main main 2>&1 | tail -1
git config user.name "wty336"
git config user.email "wtyu336@163.com"
git log --oneline -1
echo "未跟踪/改动条数：$(git status --porcelain | wc -l)"
echo "派生数据仍在：$(ls data/training/*.jsonl 2>/dev/null | wc -l) 个 jsonl"

echo
echo "=== ② 装 datasets / trl ==="
"$VENV/bin/pip" install -q datasets trl 2>&1 | tail -3
"$VENV/bin/python" - <<'PY'
import torch, transformers, peft, datasets, trl
print(f"datasets {datasets.__version__} | trl {trl.__version__}")
print(f"torch {torch.__version__} | transformers {transformers.__version__} | peft {peft.__version__}")
print(f"cuda={torch.cuda.is_available()} 卡数={torch.cuda.device_count()}")
PY
"$VENV/bin/pip" check 2>&1 | tail -2

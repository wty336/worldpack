"""在 GPU 服务器上跑命令 —— 主机/用户/密码**全部走环境变量，仓库里不留任何凭据**。

**为什么需要它**：⑤ 训练 / ⑥ 评测都要在那台机器上跑（本机起不了 vLLM、训不了 LoRA），
而每次手写一段 paramiko 探针既啰嗦、又容易顺手把密码写进文件（我这次就连写了四个探针脚本）。
这个工具把"远程执行"收敛成一条命令：

    $env:DSH_SSH_HOST='1.2.3.4'; $env:DSH_SSH_USER='ubuntu'; $env:DSH_SSH_PW='...'
    uv run --quiet --with paramiko python scripts/remote_run.py -- "nvidia-smi"
    uv run --quiet --with paramiko python scripts/remote_run.py --cwd /home/ubuntu/game_agent -- \\
        "/home/ubuntu/venv/bin/python -m scripts.scenario_factory.export_training"

**两条纪律**：

1. 脚本里**没有**任何主机/用户/密码的默认值 —— 有默认值就会诱人把密码写进代码；
   三个环境变量缺任一即报错退出（连"试一试"的机会都不给）。
2. 远端 stdout / stderr **分开原样透传**，并把退出码带回来 —— 远端报错必须看得见，
   不能让"命令跑完了"冒充"命令成功了"（"空 = 未知 ≠ 通过"在这儿的用法）。
"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="远程执行命令（凭据走环境变量）")
    p.add_argument("command", help="要执行的 shell 命令（一整条字符串）")
    p.add_argument("--cwd", default=None, help="先 cd 到该目录")
    p.add_argument("--timeout", type=float, default=1800.0, help="秒（默认 30 分钟）")
    args = p.parse_args(argv)

    host = os.environ.get("DSH_SSH_HOST")
    user = os.environ.get("DSH_SSH_USER")
    pw = os.environ.get("DSH_SSH_PW")
    missing = [k for k, v in (("DSH_SSH_HOST", host), ("DSH_SSH_USER", user),
                              ("DSH_SSH_PW", pw)) if not v]
    if missing:
        print(f"[✗] 缺环境变量：{'、'.join(missing)}（凭据不入库，故无默认值）", file=sys.stderr)
        return 2

    try:
        import paramiko
    except ImportError:
        print("[✗] 缺 paramiko：用 `uv run --with paramiko python scripts/remote_run.py ...`",
              file=sys.stderr)
        return 2

    cmd = f"cd {args.cwd} && {args.command}" if args.cwd else args.command
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=user, password=pw, timeout=30, banner_timeout=30)
    try:
        _, out, err = cli.exec_command(cmd, timeout=args.timeout)
        text = out.read().decode("utf-8", "replace")
        errt = err.read().decode("utf-8", "replace")
        code = out.channel.recv_exit_status()
    finally:
        cli.close()
    if text:
        print(text, end="" if text.endswith("\n") else "\n")
    if errt:
        print("--- stderr ---", file=sys.stderr)
        print(errt, end="" if errt.endswith("\n") else "\n", file=sys.stderr)
    print(f"[远端退出码] {code}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

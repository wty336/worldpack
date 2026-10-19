"""分发真机冒烟：**打 wheel → 检查内容 → 装到干净目录 → 在空 cwd 里跑起来**。

用法::

    python scripts/dist_probe.py            # 全流程（约 10 秒，离线可跑）
    python scripts/dist_probe.py --keep     # 保留临时目录便于排查

## 为什么要有它

`tests/test_distribution.py` 守的是**配置与逻辑**（`[project.scripts]` 指的函数存在、
`force-include` 声明了、`resolve_pack_root()` 的三级回退正确）。但它证明不了
"打包产物真的能用"——wheel 里少一个 `world-packs/`、`entry_points.txt` 没生成、
包内路径在安装后被 rewrite 掉，这些都只有**真打一次包再装一遍**才看得见。

而"装完不能用"恰恰是分发最贵的失败：发现时玩家已经拿到包了。

## 它验的四件事

1. wheel 与 sdist 都能构建出来；
2. wheel 里有**全部**世界包（`<pkg>/world-packs/*/world.yaml` 数量 = 仓库里的数量）；
3. wheel 的 `entry_points.txt` 声明了 `game-agent = game_agent.cli:main`；
4. 装到临时目录（`--no-deps`，用当前解释器的依赖）后，在**空 cwd** 里跑
   `check-worldpack` 能加载出真实的一张卡——这一步同时验证第 2 条与
   `resolve_pack_root()` 的包内回退。

## 它不验什么

不验"另一个干净 Python 环境里 `pip install` 的依赖解析"（那需要网络）。第 4 步借用
当前解释器已装的依赖，因此覆盖的是**本项目自己的打包内容**，而不是依赖树。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = "game_agent"


def _run(cmd: list[str], *, cwd: Path = REPO, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="分发真机冒烟（构建 → 检查 → 安装 → 跑起来）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args(argv)

    failures: list[str] = []
    dist = REPO / "dist"
    repo_packs = sorted(p.name for p in (REPO / "world-packs").iterdir()
                        if (p / "world.yaml").is_file())

    # ---- 1. 构建 ----
    print("[1/4] 构建 wheel 与 sdist …")
    r = _run(["uv", "build"])
    if r.returncode != 0:
        # `uv` 不在 PATH 时退一步：只报告，不猜
        print(f"  [✗] 构建失败（退出码 {r.returncode}）\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
        return 1
    wheel = max(dist.glob("*.whl"), key=lambda p: p.stat().st_mtime, default=None)
    sdist = max(dist.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, default=None)
    if wheel is None or sdist is None:
        print(f"  [✗] dist/ 里没有 wheel 或 sdist：{list(dist.glob('*'))}")
        return 1
    print(f"  [✓] {wheel.name} · {sdist.name}")

    # ---- 2/3. 检查内容 ----
    print("[2/4] 检查 wheel 内容 …")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        packs = sorted(
            Path(n).parent.name for n in names
            if n.startswith(f"{PKG}/world-packs/") and n.endswith("/world.yaml")
        )
        if packs != repo_packs:
            failures.append(f"wheel 里的卡与仓库不一致：缺 {sorted(set(repo_packs) - set(packs))}，"
                            f"多 {sorted(set(packs) - set(repo_packs))}")
        else:
            print(f"  [✓] {len(packs)} 张卡都进了 wheel")
        ep = next((n for n in names if n.endswith("entry_points.txt")), None)
        if ep is None:
            failures.append("wheel 里没有 entry_points.txt（console script 不会生成）")
        else:
            text = zf.read(ep).decode("utf-8")
            if "game-agent = game_agent.cli:main" not in text:
                failures.append(f"entry_points.txt 内容不对：{text!r}")
            else:
                print("  [✓] entry_points.txt 声明了 game-agent = game_agent.cli:main")

    # ---- 4. 装到临时目录并在空 cwd 里跑 ----
    print("[3/4] 安装到临时目录（--no-deps）…")
    tmp = Path(tempfile.mkdtemp(prefix="ga-dist-"))
    site, cwd = tmp / "site", tmp / "cwd"
    cwd.mkdir(parents=True)
    r = _run(["uv", "pip", "install", "--no-deps", "--target", str(site), str(wheel)])
    if r.returncode != 0:
        print(f"  [✗] 安装失败：{r.stdout[-400:]}{r.stderr[-400:]}")
        return 1
    print(f"  [✓] 装到 {site}")

    print("[4/4] 在空 cwd 里跑 check-worldpack …")
    env = dict(os.environ, PYTHONPATH=str(site))
    r = _run([sys.executable, "-c",
              "import game_agent; from game_agent.cli import main;"
              "raise SystemExit(main(['check-worldpack']))"], cwd=cwd, env=env)
    if r.returncode != 0:
        failures.append(f"装出来的包跑不起来（退出码 {r.returncode}）：\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    elif "加载成功" not in r.stdout:
        failures.append(f"装出来的包没有加载到任何卡：\n{r.stdout[-800:]}")
    else:
        first = next((ln.strip() for ln in r.stdout.splitlines() if "加载成功" in ln), "")
        print(f"  [✓] {first}")

    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"  （保留临时目录：{tmp}）")

    if failures:
        print("\n✗ 分发冒烟失败：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n✓ 分发冒烟通过：wheel 能构建、含全部卡、声明了入口、装完在空目录里跑得起来。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

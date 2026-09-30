"""前端构建产物守卫（Stage B）。

**为什么需要这个文件**：`game_agent/webui/dist/` 是**入库的构建产物**——CI 只跑
pytest、不跑 npm，服务端直接托管 dist。这带来一个必然的风险：

    某人改了 `src/App.vue`，忘了 `npm run build`，
    测试全绿、CI 全绿，但线上跑的是旧前端。

这个文件就是用来把这件事变成**红色测试**的。

**为什么不用 mtime**：全新 clone 里所有文件的 mtime 都是检出时间，比不出先后；
`git checkout` 某几个文件后 mtime 更是不反映内容。故构建时（`vite.config.js` 的
`build-manifest` 插件）把每个源文件的内容哈希写进 `dist/build-manifest.json`，
这里**重算源哈希**比对——与 clone、与时钟、与平台都无关。

与参照实现（dsh-tavern 的 `build-tavern-client.mjs --check`）是同一思路，
区别是我们比哈希而不是重跑构建：CI 里没有 Node 也成立。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parent.parent / "game_agent" / "webui"
DIST = WEBUI / "dist"
MANIFEST = DIST / "build-manifest.json"

# 与 vite.config.js 的 SOURCE_GLOBS 保持一致（那边是构建侧声明的集合）
SOURCE_GLOBS = ["index.html", "vite.config.js", "package.json", "src"]


def _source_files() -> list[str]:
    """参与哈希的源文件（相对 webui/ 的 posix 路径，排序后）。"""
    out: list[str] = []
    for glob in SOURCE_GLOBS:
        p = WEBUI / glob
        if p.is_file():
            out.append(glob)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    out.append(f.relative_to(WEBUI).as_posix())
    return sorted(out)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@pytest.fixture(scope="module")
def manifest() -> dict:
    if not MANIFEST.is_file():
        pytest.fail(
            f"缺少 {MANIFEST.relative_to(WEBUI.parent.parent)}——前端尚未构建。"
            " 在 game_agent/webui/ 下运行 `npm install && npm run build`。"
        )
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_dist_entry_and_assets_exist():
    """构建产物的入口与静态资源都在（缺任何一个，页面在浏览器里都是白屏）。"""
    assert (DIST / "index.html").is_file()
    assert (DIST / "assets").is_dir()
    assert list((DIST / "assets").iterdir()), "assets/ 是空的——构建没有真正产出"


def test_index_html_references_existing_assets():
    """入口页引用的每个资源都必须存在，且路径形态是预期的两种之一。

    这条看着多余，其实不然：Vite 的 `base` 配错（默认 `/assets/` 而不是 `/static/assets/`）
    时**文件都在、路径却 404**，服务端与其它测试都发现不了——只有浏览器会白屏。

    允许两种引用（白名单而不是"必须以 /static/ 开头"）：
    - `/static/...`：由 FastAPI 托管，**必须存在**；
    - `data:`：内联（favicon 就是），没有可检查的文件。
    其它任何形态（`http://` 外链、`/assets/...`、相对路径）都是配置漂移的信号
    ——第一版只允许 `/static/`，于是加上内联 favicon 后这条守卫自己红了；
    收紧是对的，但白名单要写全，否则守卫会拦住合法的实现方式。
    """
    html = (DIST / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs, "入口页没有任何资源引用——构建异常"
    for ref in refs:
        if ref.startswith("data:"):
            continue  # 内联资源：无文件可查
        assert ref.startswith("/static/"), (
            f"资源路径 {ref!r} 既不是内联 data: 也不是 /static/ 开头："
            " FastAPI 在 /static 下托管 dist，说明 vite.config.js 的 base 配错了"
        )
        rel = ref[len("/static/"):]
        assert (DIST / rel).is_file(), f"入口页引用了不存在的资源：{ref}"


def test_dist_is_not_stale(manifest):
    """**核心守卫**：源码内容哈希必须与构建时一致。

    红了就是"前端源码改了但没重新构建"——在 game_agent/webui/ 下跑 `npm run build`。
    """
    recorded = manifest.get("files") or {}
    assert recorded, "build-manifest.json 里没有文件清单"

    current = {rel: _digest(WEBUI / rel) for rel in _source_files()}
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    changed = sorted(r for r in set(current) & set(recorded) if current[r] != recorded[r])

    assert not (added or removed or changed), (
        "前端构建产物已过期（dist 与源码不一致）——请在 game_agent/webui/ 下运行 "
        "`npm run build`。\n"
        f"  新增源文件（未参与构建）: {added}\n"
        f"  已删除源文件（仍在 dist 里）: {removed}\n"
        f"  内容已变: {changed}"
    )


def test_manifest_digest_is_self_consistent(manifest):
    """清单自证：把清单里的逐文件哈希按同样规则聚合，必须等于它记录的 sourceDigest。

    防止"清单被手工改过"或"两边的聚合规则不一致"——那会让上面那条守卫形同虚设。
    **规则必须与 `vite.config.js` 的 `hashSources()` 逐字节一致**：
    Node 的 `hash.update(str)` 默认按 utf8 编码，所以这里要显式 `.encode("utf-8")`
    （Python 的 hashlib 只接受 bytes——第一版就是漏了这一步而 TypeError）。
    """
    files = manifest.get("files") or {}
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(files[rel].encode("utf-8"))
        h.update(b"\0")
    assert h.hexdigest()[:16] == manifest["sourceDigest"]
    assert manifest["count"] == len(files)


def test_node_modules_is_not_committed():
    """依赖目录不入库（.gitignore 已声明；这里防"误把 node_modules 当成产物加进来"）。"""
    if (WEBUI / "node_modules").is_dir():
        import subprocess

        out = subprocess.run(
            ["git", "status", "--porcelain", "--", "game_agent/webui/node_modules"],
            cwd=WEBUI.parent.parent,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert not out, f"node_modules 出现在 git 视野里：\n{out[:400]}"

"""N11 / §E-2：世界包内资源引用的 `asset://` 间接层。

## 为什么需要它

世界包现在是纯 YAML：所有文本都写在 `world.yaml` / `npcs/*.yaml` 里。一旦出现
**长文本附件**（一大段年表、一份完整律法、几条长设定），把它们塞进 YAML 会同时伤三件事：
YAML 变得没法人工读、`pack_digest` 每次都要哈希这些不常改的大文本、
以及**换环境就失效**——作者在上面写死本机绝对路径，别人的机器上立刻打不开。

后者正是 SillyTavern 踩过的坑（`.charx` 的 `embeded://` / `__asset:` 前缀就是为它加的，
`src/charx.js:9-11`）。它给出的解法也对：**定一个 URI 前缀 + 一个统一的解析入口**，
让各处代码不再自己拼路径。

## 约定

```yaml
# world.yaml
lore:
  - id: law_book
    keys: [律法, 刑]
    text: asset://lore/law_book.txt     # ← 加载期读成文件内容
```

- 前缀固定 `asset://`，后面是**相对世界包根目录**的路径；
- **解析发生在加载期**（`load_worldpack`），之后引擎各处看到的永远是解析好的文本，
  不会有第二套"这里要不要解析"的判断散落在上下文/校验/生成代码里；
- 文件不存在 / 越界（`..`、绝对路径、盘符）/ 不是文本 → 加载期抛 `WorldPackError`，
  带文件与引用原文。**不在运行时降级**：静默把一条设定读成空字符串，比直接报错
  糟得多——玩家只会发现"世界怎么变薄了"。

## 只做文本

本项目是文字游戏：没有渲染图片、没有播放音频的地方。所以 `asset://` 只用于**文本**，
二进制内容会被拒绝并说明原因。做一个"能加载图片但没有任何人消费它"的机制，
等于制造一段从未运行过的代码（项目里已经吃过这个亏，见 A 系列字段零用量）。
"""

from __future__ import annotations

from pathlib import Path

SCHEME = "asset://"
MAX_ASSET_BYTES = 256 * 1024
"""单个资源文件的上限（字符/字节数）。超过就报错而不是悄悄注入。

理由：`asset://` 的用途是"长文本附件"，不是"把整本小说塞进上下文"。真正需要长素材时
应当走素材导入（`scripts/import_story.py`）而不是让包在运行时膨胀——而一个 5MB 的
lore 文本会在每回合都进一次请求。
"""


class AssetError(ValueError):
    """资源引用无法解析（不存在 / 越界 / 非文本 / 过大）。"""


def is_asset_ref(value) -> bool:
    """`value` 是否是资源引用（**完整匹配**，不做前后缀容错）。

    严格完整匹配是刻意的：`text: "见 asset://x"` 这种夹在句子里的写法**不是**引用，
    而是正文。模糊匹配会让"作者提了一句 asset://"变成一次神秘的文件读取。
    """
    return isinstance(value, str) and value.startswith(SCHEME) and len(value) > len(SCHEME)


def resolve(ref: str, pack_root: str | Path) -> Path:
    """把 `asset://<相对路径>` 解析成世界包内的绝对路径，并**在加载期**校验它。

    三道校验，每一道都对应一类真实的坏包：
    1. 必须相对、不许盘符/绝对路径（作者从 Windows 粘过来的 `C:\\…` 很常见）；
    2. `..` 逃不出包根（路径穿越：世界包是**别人给的素材**，这是不可信输入）；
    3. 必须存在、必须是文件。
    """
    if not ref.startswith(SCHEME):
        raise AssetError(f"不是资源引用（应以 {SCHEME} 开头）：{ref!r}")
    rel = ref[len(SCHEME):].strip()
    if not rel:
        raise AssetError(f"资源引用为空：{ref!r}")
    raw = Path(rel)
    if raw.is_absolute() or raw.drive or raw.anchor:
        raise AssetError(
            f"资源引用必须是**相对世界包根目录**的路径，不能是绝对路径/盘符：{ref!r}。"
            f"写 `asset://lore/x.txt`，不要写 `asset://{raw}`"
        )
    root = Path(pack_root).resolve()
    p = (root / raw).resolve()
    if not p.is_relative_to(root):
        raise AssetError(
            f"资源引用越界（指向世界包目录之外）：{ref!r} → {p}。"
            f"`asset://` 只能引用包内文件——世界包是外来的素材，不能让它读到你机器上的任意文件。"
        )
    if not p.is_file():
        raise AssetError(f"资源文件不存在：{ref!r}（解析为 {p}）")
    return p


def load_text(ref: str, pack_root: str | Path) -> str:
    """读一个文本资源（UTF-8）。二进制/超大文件 → `AssetError`，不静默降级。"""
    p = resolve(ref, pack_root)
    size = p.stat().st_size
    if size > MAX_ASSET_BYTES:
        raise AssetError(
            f"资源文件过大（{size} 字节 > {MAX_ASSET_BYTES}）：{ref!r}。"
            f"长素材请走 `scripts/import_story.py` 生成进包里，不要在运行时注入大段文本。"
        )
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise AssetError(
            f"资源文件不是 UTF-8 文本（本项目只分发**文本**资源；图片/音频没有消费方）：{ref!r}。{e}"
        ) from e


def expand(data, pack_root: str | Path, *, where: str = ""):
    """递归把 `data` 里的资源引用替换成文件内容（**加载期**调用一次）。

    返回新对象，不改入参。`where` 只用于报错时指出是哪个文件/哪一层：
    作者拿到"`world.yaml` 的 `lore[3].text` 指向的文件不存在"才知道该改哪。
    """
    if isinstance(data, str):
        if not is_asset_ref(data):
            return data
        try:
            return load_text(data, pack_root)
        except AssetError as e:
            raise AssetError(f"{where}: {e}" if where else str(e)) from e
    if isinstance(data, dict):
        return {
            k: expand(v, pack_root, where=f"{where}.{k}" if where else str(k))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [
            expand(v, pack_root, where=f"{where}[{i}]")
            for i, v in enumerate(data)
        ]
    return data

"""N9：SillyTavern 人物卡 / 世界书 ↔ 本项目世界包的**单向素材导入**（外加导出方向）。

上游：`docs/sillytavern-borrow.md` **§E-1**（字段映射表）、**§E-3**（读宽写严）、
`docs/plan-dsh-tavern-parity.md` 期 4。

## 为什么只做导入，不做运行时兼容

ST 的运行时（Tavern Helper 脚本、MVU、正则美化、预设、小手机）本项目**明确不借**
（`sillytavern-borrow.md` §D 逐条论证）。但它的**内容载体**是这类素材的事实标准，
所以：**只借内容，不借运行时**——把现成卡里的世界书搬进来，作为世界包的素材起点。

## 读宽、写严（§E-3）

两条路径分开，**解析层不调用校验层**：

- `parse_card()` / `character_book_to_lore()`：**宽松**。字符串/数组混用、缺失字段、
  V1 老卡、`enabled/disable` 取反、`key/keysecondary` 的单数写法，全部归一；
- `check_worldpack()`（既有）：**严格**。归一之后仍然过不去就报错，且报错说的是
  "归一之后的值"而不是原始 JSON 的键名——作者照着改得动。

**宽松不等于静默**：任何被丢弃/不支持的东西都进 `warnings`（`StWarnings`），
包括 ST 有而本项目不借的字段（`position` / `depth` / `sticky` 等）。"悄悄少了一条"
是这类导入工具最难发现的故障。

## 一个**刻意不猜**的地方：`selectiveLogic` 的数字编号

ST 世界书的次级关键词逻辑是一个 0–3 的枚举。但在本环境里**无法核对**它的数值顺序
（无网络、仓库内无 ST 源码），所以这里**不硬编码**这个映射：带次键的条目一律使用
调用方给的 `logic`（缺省 `AND_ANY`）并附一条警告。宁可让作者显式选一次，
也不要写一个"看起来对、实际把 AND ANY 当 NOT ALL"的转换——那种错不会报错，
只会让世界书在特定关键词组合下静默不进上下文。

## 不做 PNG

ST 的人物卡常以 PNG 分发（元数据藏在 `tEXt` 块里）。本项目只吃 **JSON**：
PNG 解码要额外处理 `tEXt/zTXt/iTXt` 三种块与字符集，而这条链在本环境里**没有真卡可验**。
`read_card_file()` 遇到 PNG 会明确报错并告诉作者怎么导出 JSON，而不是崩在某个字节上。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: ST `character_book` 条目的字段名（与 `LoreSpec` 的对应关系见模块 docstring 与 §E-1）
ST_ENTRY_KEYS = ("keys", "content", "secondary_keys", "constant", "selective", "use_regex",
                 "enabled", "comment", "insertion_order", "extensions")

#: ST 有、本项目**明确不借**的字段（§D-3 等）。不静默丢弃——列出来告诉作者。
ST_UNSUPPORTED = {
    "position": "插入位置（D-3 不借：本项目按状态栏固定序列注入）",
    "depth": "注入深度（D-3）",
    "probability": "加权随机（D-5 不借）",
    "sticky": "sticky 定时（D-4 不借）",
    "cooldown": "cooldown 定时（D-4 不借）",
    "delay": "延迟注入（D-4）",
    "group": "条目分组（未实现）",
    "role": "注入角色（本项目固定 user/engine）",
    "vectorized": "向量化索引（本项目用 BM25 + 关键词，不建向量库）",
    "addMemo": "编辑器备注（不影响注入）",
}

#: ST 的**驼峰写法** → 本项目/世界书里的小写写法（N9 真卡实测发现的差异）。
#:
#: 真实世界书导出（`gundum_lorebook_early.json`，27 条）用的是 `ignoreBudget` /
#: `excludeRecursion` / `preventRecursion`，而文档里那张映射表只写了小写形式。
#: 只认一种写法 = 静默丢掉三个语义字段——这正是"对着映射表写、拿真实数据一跑就露"。
ST_CAMEL_ALIASES = {
    "ignoreBudget": "ignore_budget",
    "excludeRecursion": "exclude_from_recursion",
    "preventRecursion": "no_recursion_trigger",
}

#: 我们导出时写的 schema 版本（ST V2 的 `character_book`）
EXPORT_SPEC = "chara_card_v2"
EXPORT_SPEC_VERSION = "2.0"


class StConvertError(ValueError):
    """卡片/世界书无法解析（结构不对、是 PNG、条目不是列表等）。"""


@dataclass
class StWarnings:
    """导入过程中的**全部**降级/丢弃说明（读宽但不静默）。"""

    items: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        if message not in self.items:
            self.items.append(message)

    def __bool__(self) -> bool:
        return bool(self.items)

    def report(self) -> str:
        return "\n".join(f"- {m}" for m in self.items)


# ---------------------------------------------------------------------------
# 读：宽松解析
# ---------------------------------------------------------------------------


def _as_list(value) -> list[str]:
    """ST 里同一个字段可能是字符串、逗号串、或用 `|` 分隔的串。统一成 list[str]。

    §E-3 的"类型归一"：`alternate_greetings` 是字符串就包成单元素数组，`tags` 是逗号串
    就切分。这里对**所有**列表字段用同一套归一，避免每个字段各写一份。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    # 一次切开所有分隔符（不能"遇到第一个命中就切"：`古代,名门，第三人` 两种逗号混用，
    # 先匹配 "," 会把 `名门，第三人` 留成一条 —— 归一化的意义就是不挑输入风格）
    parts = re.split(r"[|,，、]", text)
    return [p.strip() for p in parts if p.strip()]


def _bool(value, default: bool = False) -> bool:
    """ST 的布尔字段可能是 `true` / `1` / `"true"` / 缺省。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def parse_card(raw: dict, warnings: StWarnings | None = None) -> dict:
    """把任意版本的 ST 人物卡归一成**我们的标准形状**（§E-3 的"归一为标准模型"）。

    支持三种载体：
    - **V2/V3**：`{"spec": "chara_card_v2", "data": {...}}`（V3 的 `data` 里还有
      `character_book` 等）；
    - **V1 老卡**：字段直接放在顶层（`name` / `description` / `mes_example`…）；
    - **裸世界书**：`{"entries": [...]}` 或 `{"entries": {"0": {...}}}`（ST 世界书导出）。

    **不校验**（那是 `check_worldpack` 的事）：这里只保证"字段齐全、类型正确"。
    """
    if warnings is None:  # 不能写 `warnings or StWarnings()`：空的 StWarnings 为假，
        warnings = StWarnings()  # 那会把调用方传进来的对象**静默换成新的**，警告全丢
    if not isinstance(raw, dict):
        raise StConvertError(f"卡片必须是 JSON 对象，实际是 {type(raw).__name__}")

    spec = raw.get("spec") or raw.get("specification") or ""
    if spec and not str(spec).startswith("chara_card"):
        warnings.add(f"未知的 spec {spec!r}——按 V1/V2 的形状尽力解析")
    if "data" in raw and isinstance(raw["data"], dict):
        data = raw["data"]
    else:
        data = raw
        if spec or "name" in raw:
            warnings.add("V1 老卡（字段在顶层，没有 data 块）——已按标准模型归一")

    # **裸世界书**：顶层就是 `entries`（ST 的"世界书导出"正是这个形状：没有 name/data）。
    # 这条最初只写在 docstring 里、**代码没实现**——被一份真实世界书当场戳穿
    # （`gundum_lorebook_early.json`，27 条，probe 报"0 条可导入"）。
    # 教训与项目里那条一样：**docstring 承诺的东西必须有代码路径**。
    book = data.get("character_book") or data.get("characterBook")
    if book is None and isinstance(data.get("entries"), (list, dict)):
        book = data
        warnings.add(
            f"这是一本**独立世界书**（顶层就是 entries，"
            f"{len(data['entries'])} 条），按世界书读取"
        )

    card = {
        "name": str(data.get("name") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "personality": str(data.get("personality") or "").strip(),
        "scenario": str(data.get("scenario") or "").strip(),
        "first_mes": str(data.get("first_mes") or "").strip(),
        "mes_example": str(data.get("mes_example") or "").strip(),
        "system_prompt": str(data.get("system_prompt") or "").strip(),
        "creator_notes": str(data.get("creator_notes") or "").strip(),
        "tags": _as_list(data.get("tags")),
        "alternate_greetings": _as_list(data.get("alternate_greetings")),
        "character_book": book,
    }
    if not card["name"] and book is not data:
        warnings.add("卡片没有 name 字段——生成世界包时必须显式给名字")
    if card["alternate_greetings"]:
        warnings.add(
            f"卡片有 {len(card['alternate_greetings'])} 条备用开场白：本项目只有"
            f"一个开场，已放在 source 文本里供作者取用（不自动切换）"
        )
    return card


def _book_entries(book, warnings: StWarnings) -> list[dict]:
    """从 `character_book` / 世界书导出里取出条目列表（两种载体：list / uid→entry 字典）。

    §E-1 的验收要求："`entries` 必须是列表"——ST 自己在导入时强制校验顶层含 `entries`。
    这里更宽松一点：字典也能读（老的世界书导出就是字典），但**报错信息明确**。
    """
    if book is None:
        return []
    if isinstance(book, list):
        entries = book
    elif isinstance(book, dict):
        raw_entries = book.get("entries")
        if raw_entries is None:
            raise StConvertError(
                "世界书缺少 entries（ST 导入同样强制要求它）。"
                "若你给的是人物卡，请确认 character_book 字段存在。"
            )
        if isinstance(raw_entries, dict):
            # ST 世界书导出：{"0": {…}, "1": {…}}——按 uid 数值升序，保持作者的顺序
            entries = [raw_entries[k] for k in sorted(raw_entries, key=_uid_key)]
            warnings.add(f"世界书 entries 是字典（uid → 条目），已按 uid 升序读取 {len(entries)} 条")
        elif isinstance(raw_entries, list):
            entries = raw_entries
        else:
            raise StConvertError(
                f"entries 必须是列表或字典，实际是 {type(raw_entries).__name__}"
            )
    else:
        raise StConvertError(f"世界书必须是对象或列表，实际是 {type(book).__name__}")
    return [e for e in entries if isinstance(e, dict)]


def _uid_key(key):
    try:
        return (0, int(key))
    except (TypeError, ValueError):
        return (1, str(key))


def _entry_id(entry: dict, index: int, used: set[str]) -> str:
    """条目 id：优先用 `comment`（作者写的人话），其次 `uid`（ST 世界书自带、稳定），
    最后才是 `entry_<序号>`。

    **为什么 `uid` 排在序号前面**：真实世界书导出（`{"entries": {"7": {...}}}`）的
    键就是 uid，而且条目自己也带 `uid` 字段。用序号等于把作者那边的稳定标识换成了
    "我读它的顺序"——顺序一变（有人改了字典顺序），lore id 跟着变，时间线/存档里
    对不上的就不只是 id 了。
    """
    base = str(entry.get("comment") or entry.get("id") or "").strip()
    if not base and entry.get("uid") is not None:
        base = f"entry_{entry['uid']}"
    if not base:
        base = f"entry_{index}"
    slug = re.sub(r"[^\w\-]+", "_", base, flags=re.UNICODE).strip("_") or f"entry_{index}"
    slug = slug[:40]
    candidate, n = slug, 2
    while candidate in used:
        candidate = f"{slug}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def character_book_to_lore(
    book,
    *,
    logic: str = "AND_ANY",
    warnings: StWarnings | None = None,
) -> list[dict]:
    """"`character_book` → 本项目的 `lore` 条目列表（§E-1 的导入方向）。

    返回的是**可以直接写进 `world.yaml` 的 dict**（用 `LoreSpec` 的字段名），
    不是 `LoreSpec` 实例——解析层不依赖校验层的模型，这样"读宽"与"写严"不会纠缠。

    映射（§E-1 的表）：`keys→keys` / `content→text` / `secondary_keys→secondary_keys` /
    `constant→constant` / `comment→id` / `use_regex→/re/ 包键` / `enabled:false→丢弃`。

    `logic`：带次键的条目用它（见模块 docstring 里"刻意不猜"的那一段）。
    """
    if warnings is None:  # 不能写 `warnings or StWarnings()`：空的 StWarnings 为假，
        warnings = StWarnings()  # 那会把调用方传进来的对象**静默换成新的**，警告全丢
    rows: list[dict] = []
    used: set[str] = set()
    dropped_disabled = 0
    selective_noop = 0
    unsupported_seen: dict[str, int] = {}

    for index, entry in enumerate(_book_entries(book, warnings), 1):
        for key in ST_UNSUPPORTED:
            if key in entry and entry[key] not in (None, "", 0, False, [], {}):
                unsupported_seen[key] = unsupported_seen.get(key, 0) + 1
        # 驼峰别名先归一到小写（真实世界书就是这么写的，见 `ST_CAMEL_ALIASES`）。
        # **只在值真的非默认时计数**：第一版按"字段存在"计，于是在一份 27 条、
        # 三个驼峰字段全是 `false` 的真实世界书上报告"识别到 27 处"——夸大了覆盖面。
        # 计数要回答的是"这套映射被真实数据检验到了吗"，不是"字段名出现了几次"。
        for camel, snake in ST_CAMEL_ALIASES.items():
            if camel not in entry:
                continue
            if snake not in entry:
                entry = {**entry, snake: entry[camel]}
            if entry[camel] not in (None, "", 0, False):
                unsupported_seen[f"{camel}→{snake}"] = (
                    unsupported_seen.get(f"{camel}→{snake}", 0) + 1
                )

        # `enabled: false` 或 ST 世界书的 `disable: true` 都表示"这条不参与注入"
        enabled = _bool(entry.get("enabled"), True)
        if _bool(entry.get("disable"), False):
            enabled = False
        if not enabled:
            dropped_disabled += 1
            continue

        keys = _as_list(entry.get("keys")) or _as_list(entry.get("key"))
        text = str(entry.get("content") or entry.get("text") or "").strip()
        if not keys or not text:
            missing = "关键词" if not keys else "正文"
            warnings.add(f"第 {index} 条缺少{missing}，已跳过（lore 条目两者缺一不可）")
            continue

        # `use_regex` 或 `/re/` 包裹的键 → 统一成 `/re/` 形式（本项目 lore 的原生写法）
        if _bool(entry.get("use_regex"), False):
            keys = [k if _looks_like_regex(k) else f"/{k}/" for k in keys]
        secondary = _as_list(entry.get("secondary_keys")) or _as_list(entry.get("keysecondary"))
        if _bool(entry.get("use_regex"), False) and secondary:
            secondary = [k if _looks_like_regex(k) else f"/{k}/" for k in secondary]

        row: dict = {
            "id": _entry_id(entry, index, used),
            "keys": keys,
            "text": text,
        }
        if secondary:
            row["secondary_keys"] = secondary
            row["logic"] = logic
            warnings.add(
                f"条目 {row['id']} 带次级关键词：logic 用了调用方给的 {logic!r}"
                f"（ST 的 selectiveLogic 数字枚举**未做映射**——本环境无法核对它的数值顺序，"
                f"不做猜测；请按语义自己确认一次）"
            )
        elif _bool(entry.get("selective"), False):
            # ST 的 `selective: true` 但没给 keysecondary → 实际没有收窄效果。
            # 不静默：说清"我们按 keysecondary 是否有值决定，不读 selective"。
            selective_noop += 1
        if _bool(entry.get("constant"), False):
            row["constant"] = True
        # A-1/A-4 的三个语义字段（驼峰/小写都认）
        if _bool(entry.get("ignore_budget"), False):
            row["ignore_budget"] = True
        if _bool(entry.get("exclude_from_recursion"), False):
            row["exclude_from_recursion"] = True
        if _bool(entry.get("no_recursion_trigger"), False):
            row["no_recursion_trigger"] = True
        rows.append(row)

    if dropped_disabled:
        warnings.add(f"跳过 {dropped_disabled} 条 `enabled: false` 的条目（ST 里本来就是关掉的）")
    if selective_noop:
        warnings.add(
            f"{selective_noop} 条 `selective: true` 但没有 keysecondary——等价于无次键收窄。"
            f"本项目按 `secondary_keys` 是否有值决定，不读 `selective` 标志"
        )
    for key, count in sorted(unsupported_seen.items()):
        if "→" in key:  # 驼峰别名：映射上了（只在值非默认时才会出现在这里）
            camel, snake = key.split("→")
            warnings.add(f"识别到 {count} 处驼峰写法 `{camel}`，已按 `{snake}` 映射")
        else:
            warnings.add(f"忽略 {count} 处 `{key}`：{ST_UNSUPPORTED[key]}")
    if not rows:
        warnings.add("这本世界书没有可导入的条目（全部为空或被关掉）")
    return rows


def _looks_like_regex(key: str) -> bool:
    return len(key) >= 2 and key.startswith("/") and key.endswith("/")


# ---------------------------------------------------------------------------
# 写：导出方向（让作者用别人的成熟前端验证自己的世界观）
# ---------------------------------------------------------------------------


def lore_to_character_book(lore: list[dict], *, name: str = "", description: str = "") -> dict:
    """本项目的 `lore` → ST `character_book`（§E-1 的导出方向）。

    为什么导出也要做：**往返转换是发现映射表错误最有效的手段**（§E-1 的验收口径）。
    单方向实现能把"读错"和"写错"混在一起，往返之后就分得清了。

    字段：`keys` / `secondary_keys` / `content` / `comment`（= id）/ `constant` /
    `enabled` / `use_regex`（由 `/re/` 形式推出）/ `insertion_order`（按顺序给出）。
    """
    entries: list[dict] = []
    for order, row in enumerate(lore):
        keys = list(row.get("keys") or [])
        use_regex = any(_looks_like_regex(k) for k in keys)
        if use_regex:
            keys = [k[1:-1] if _looks_like_regex(k) else k for k in keys]
        entry: dict = {
            "keys": keys,
            "content": str(row.get("text") or ""),
            "comment": str(row.get("id") or f"entry_{order + 1}"),
            "enabled": True,
            "insertion_order": order,
            "constant": bool(row.get("constant", False)),
        }
        secondary = list(row.get("secondary_keys") or [])
        if secondary:
            if use_regex:
                secondary = [k[1:-1] if _looks_like_regex(k) else k for k in secondary]
            entry["secondary_keys"] = secondary
        if use_regex:
            entry["use_regex"] = True
        entries.append(entry)
    return {
        "name": name or "worldpack",
        "description": description or "由本项目世界包导出（仅世界书部分）",
        "spec": EXPORT_SPEC,
        "spec_version": EXPORT_SPEC_VERSION,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# 素材：卡片的散文部分 → 生成管线的 source 文本
# ---------------------------------------------------------------------------


def card_to_source_text(card: dict) -> str:
    """把卡片的**散文字段**拼成生成管线吃的素材文本（结构由 LLM 生成，不在这里编）。

    这里只做"人读的整理"：把 description / personality / scenario / first_mes /
    mes_example 分段标注。世界书部分**不走 LLM**（它是作者写好的确定内容，见
    `character_book_to_lore`）——让模型去重新发明设定是最容易失真的一步。
    """
    parts: list[str] = []
    if card.get("name"):
        parts.append(f"角色：{card['name']}")
    for label, key in (
        ("角色设定", "description"),
        ("性格", "personality"),
        ("场景", "scenario"),
        ("开场", "first_mes"),
        ("对话示例", "mes_example"),
        ("作者备注", "creator_notes"),
    ):
        value = str(card.get(key) or "").strip()
        if value:
            parts.append(f"## {label}\n{value}")
    if card.get("tags"):
        parts.append("## 标签\n" + "、".join(card["tags"]))
    if not parts:
        # **纯世界书**（没有任何散文）：没有素材就没法生成世界包。
        # 但世界书的条目本身就是设定素材——用它们的标题 + 正文开头当 source，
        # 让生成管线至少有东西可依。真实世界书（27 条 Gundam 设定）就是这么来的。
        parts = _book_as_source(card.get("character_book"))
    return "\n\n".join(parts)


def _book_as_source(book, max_entries: int = 40, head_chars: int = 160) -> list[str]:
    """把世界书条目压成"设定素材"文本（纯世界书导入时的 source）。"""
    if book is None:
        return []
    try:
        entries = _book_entries(book, StWarnings())
    except StConvertError:
        return []
    lines: list[str] = []
    for i, entry in enumerate(entries, 1):
        enabled = _bool(entry.get("enabled"), True) and not _bool(entry.get("disable"), False)
        if not enabled:
            continue
        title = str(entry.get("comment") or "").strip() or f"条目 {i}"
        body = str(entry.get("content") or entry.get("text") or "").strip()
        body = " ".join(body.split())[:head_chars]
        keys = _as_list(entry.get("keys")) or _as_list(entry.get("key"))
        hint = f"（关键词：{'、'.join(keys[:4])}）" if keys else ""
        lines.append(f"- {title}{hint}：{body}")
        if len(lines) >= max_entries:
            break
    if not lines:
        return []
    return ["## 世界书设定（供生成世界观/角色/主线参考）\n" + "\n".join(lines)]


# ---------------------------------------------------------------------------
# 文件入口 + 合并进世界包
# ---------------------------------------------------------------------------


def read_card_file(path: str | Path, warnings: StWarnings | None = None) -> dict:
    """读一份 ST 卡片/世界书文件（**只支持 JSON**）。

    PNG 明确报错并给出办法：ST 的"导出为 JSON"就在卡片的右键菜单里。
    这条比"崩在某个字节上"有用得多，而 PNG 的 `tEXt/zTXt/iTXt` 三块在本环境里
    没有真卡可验，硬写等于制造一段从未运行过的代码。
    """
    p = Path(path)
    if not p.is_file():
        raise StConvertError(f"文件不存在：{p}")
    if p.suffix.lower() == ".png":
        raise StConvertError(
            f"{p.name} 是 PNG 人物卡。本工具只读 JSON——"
            f"请在 SillyTavern 里对这张卡选「导出为 JSON」，或用卡编辑器的 JSON 视图另存。"
            f"（PNG 元数据块有 tEXt/zTXt/iTXt 三种写法，本项目不做——只做能验证的路径。）"
        )
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StConvertError(f"{p.name} 不是合法 JSON：{e}") from e
    return parse_card(raw, warnings)


def lore_into_world_yaml(world_yaml: str | Path, rows: list[dict]) -> list[dict]:
    """把 lore 行**合并进**一个已生成的世界包的 `world.yaml`，返回合并后被顶替的旧条目。

    **默认替换而不是追加**：卡里的世界书是作者写好的确定内容；生成管线自己瞎编的 lore
    留着只会两套设定打架。被顶替的旧条目回传给调用方打印，作者能看见"换掉了什么"。
    """
    p = Path(world_yaml)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    old = list(data.get("lore") or [])
    data["lore"] = list(rows)
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=100),
        encoding="utf-8",
    )
    return old

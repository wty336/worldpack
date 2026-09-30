"""创作者 Agent（能力 C / E-8）：对话式改人物设定与世界书。

上游：`docs/plan-tavern-shaped-product.md` §4。用户的原始需求原话是
「也可以与 Agent 对话，从素材制作新卡，**修改人物设定和世界书**」——这一条就是后半句。

## 为什么是**另一条**循环，而不是往 `game.py` 里塞

`llm.run_turn` + `ToolRegistry` 是为**叙事回合**优化的：流式正文、`submit_narration`
收尾协议、Judge、factcheck、内轮自校正、整轮原子回滚。创作者任务是**结构化 CRUD**，
目标完全不同（§4.1）：

| | 叙事回合 | 创作者回合 |
| --- | --- | --- |
| 产出 | 一段给玩家读的正文 | 文件改动 |
| 收尾 | 必须调 `submit_narration`，否则熔断 | **不调工具就等于说完了**（没有协议） |
| 质量兜底 | Judge + factcheck + 回滚 | `check_worldpack`（硬门禁） |
| 失败代价 | 整轮作废、玩家看到兜底文本 | 草稿改坏了再改一次——**草稿就是沙箱** |

所以本模块只做三件事：一个**工作版**、一张**工具面**、一条**修复循环**。
复用的是同一套 `ToolSpec` / `ToolRegistry.dispatch`（声明式、异常转结构化拒绝文本），
以及同一个 `LLMClient`（模型路由 + usage 记账 + trace 只有一份实现——见
`complete_with_tools` 的注释）。

## 三个关键判断

1. **工作版 = 草稿目录本身**，不另做一份副本。E-7 已经把 `world-packs/_drafts/<name>/`
   定义成"可以随便改、过闸门才能发布"的区域，那就是工作版；原始包（已发布区）
   在结构上只读——本模块里**没有任何一行**写已发布区。
2. **基线快照取"会话第一次碰到它时"的内容**，而不是"已发布区里同名的那份"。
   因为生成的草稿根本没有已发布版本，而作者真正要看的是「**Agent 改了什么**」。
   一张卡无论是 fork 来的还是生成的，这条都成立。（基线在内存里；进程重启后
   从当前状态重新开始记——这一点如实写在 `baseline_note` 里，不假装是持久差异。）
3. **编辑走白名单 + 整体校验**。字段白名单（下表）挡住"模型手滑写个不存在的字段
   然后静默丢失"；`check_worldpack` 挡住"改完之后玩家一定会撞上"的问题
   （引用悬空、结局数值不可达、旗标没人写）。后者的**报错原文会回灌给模型**
   ——这就是 §4.2 说的"修复循环的燃料"。
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .registry import ToolRegistry, ToolSpec
from .worldpack import (
    PACK_CONTENT_FILES,
    WorldPackError,
    load_worldpack,
)


class CreatorError(Exception):
    """创作者 Agent 的使用错误（未知字段 / 草稿不存在 / 循环超限）。"""


MAX_CREATOR_ITERS = 6
"""一轮对话里最多几次模型调用。

为什么是 6：一次典型的"改设定"是 读→改→校验（3 次）；出现校验失败时
再 改→校验（2 次）；留 1 次余量给"读一张还没读过的卡"。超限**不是错误**——
循环会停下来并如实告诉作者"这一轮没做完"，而不是无限烧钱。
"""

# --- 可编辑字段白名单（§4.2 的「白名单字段」是硬约束，不是建议）---

WORLD_STRING_FIELDS = (
    "name", "era", "opening", "player_role", "player_goal", "start_scene",
)
WORLD_LIST_FIELDS = ("core_rules", "style_guide", "forbidden")

NPC_STRING_FIELDS = ("name", "identity", "personality", "speech_style")
NPC_LIST_FIELDS = ("secrets", "boundaries", "forbidden")

LORE_FIELDS = ("id", "keys", "text", "secondary_keys", "logic", "case_sensitive")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise CreatorError(f"缺少文件：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise CreatorError(f"YAML 解析失败（{path.name}）：{e}") from e
    if not isinstance(data, dict):
        raise CreatorError(f"{path.name} 的根节点必须是映射")
    return data


def _write_yaml(path: Path, data: dict) -> None:
    """写回 YAML。**必须 `allow_unicode`**，否则中文会变成转义序列，
    包内容变得不可读（frozenset 之类也会）。`sort_keys=False` 保持字段原有顺序，
    让 diff 只显示真正改掉的键。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.write_text(text, encoding="utf-8", newline="\n")


@dataclass
class WorkingCopy:
    """工作版（= 一份草稿目录）。所有写操作都**只落在 `root` 之内**。"""

    root: Path
    baseline: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """构造即取基线。

        **为什么放在这里而不是让调用方记得取**：基线为空时 `diff()` 会把**每一个
        存在的文件**都算成"新增"（旧侧是空串），于是作者看到一份"改得面目全非"的
        diff，而实际上什么都没改。让"没有基线的工作版"根本构造不出来，
        比在每个调用点提醒一次可靠（写守卫时就是这么踩到的：直接 `WorkingCopy(root=…)`
        的那几条测试全都看到 6 个文件"被改"）。
        """
        if not self.baseline:
            self.baseline = self.snapshot()

    # ---- 基线 / diff ----

    def snapshot(self) -> dict[str, str]:
        """读当前全部玩法内容的文本（相对路径 → 文本）。"""
        out: dict[str, str] = {}
        for rel in self._content_rels():
            p = self.root / rel
            if p.is_file():
                out[rel] = p.read_text(encoding="utf-8")
        return out

    def _content_rels(self) -> list[str]:
        rels = [n for n in PACK_CONTENT_FILES]
        npcs = self.root / "npcs"
        if npcs.is_dir():
            rels += [f"npcs/{p.name}" for p in sorted(npcs.glob("*.yaml"))]
        return rels

    def diff(self) -> str:
        """工作版 vs 基线的统一 diff。没改动返回空串。"""
        now = self.snapshot()
        chunks: list[str] = []
        for rel in sorted(set(self.baseline) | set(now)):
            old = self.baseline.get(rel, "")
            new = now.get(rel, "")
            if old == new:
                continue
            chunks.append(
                "".join(
                    difflib.unified_diff(
                        old.splitlines(keepends=True),
                        new.splitlines(keepends=True),
                        fromfile=f"基线/{rel}",
                        tofile=f"工作版/{rel}",
                    )
                )
            )
        return "\n".join(c for c in chunks if c)

    def changed_files(self) -> list[str]:
        now = self.snapshot()
        return sorted(
            rel for rel in set(self.baseline) | set(now)
            if self.baseline.get(rel, "") != now.get(rel, "")
        )

    # ---- 读 ----

    def read_world(self) -> dict:
        return _read_yaml(self.root / "world.yaml")

    def list_npcs(self) -> list[dict]:
        npcs = self.root / "npcs"
        if not npcs.is_dir():
            return []
        out = []
        for p in sorted(npcs.glob("*.yaml")):
            d = _read_yaml(p)
            out.append({"id": d.get("id", p.stem), "name": d.get("name", ""),
                        "identity": d.get("identity", "")})
        return out

    def read_npc(self, npc_id: str) -> dict:
        p = self._npc_path(npc_id)
        return _read_yaml(p)

    def read_lore(self) -> list[dict]:
        return list(self.read_world().get("lore") or [])

    def _npc_path(self, npc_id: str) -> Path:
        # 与 catalog 的 `_ID_PATTERN` 同一姿态：**id 绝不参与路径拼接**
        if not npc_id or "/" in npc_id or "\\" in npc_id or ".." in npc_id:
            raise CreatorError(f"非法 NPC id：{npc_id!r}")
        p = self.root / "npcs" / f"{npc_id}.yaml"
        if not p.is_file():
            raise CreatorError(
                f"没有这个角色：{npc_id!r}（现有：{', '.join(n['id'] for n in self.list_npcs()) or '无'}）"
            )
        return p

    # ---- 写（白名单）----

    def update_world_field(self, field_name: str, value: Any) -> str:
        if field_name in WORLD_STRING_FIELDS:
            if not isinstance(value, str):
                raise CreatorError(f"{field_name} 需要字符串，收到 {type(value).__name__}")
        elif field_name in WORLD_LIST_FIELDS:
            value = self._as_str_list(field_name, value)
        else:
            raise CreatorError(
                f"字段 {field_name!r} 不可改。可改："
                f"{', '.join(WORLD_STRING_FIELDS + WORLD_LIST_FIELDS)}"
            )
        w = self.read_world()
        old = w.get(field_name)
        w[field_name] = value
        _write_yaml(self.root / "world.yaml", w)
        return f"world.yaml: {field_name} 已更新\n旧值：{_brief(old)}\n新值：{_brief(value)}"

    def update_npc_field(self, npc_id: str, field_name: str, value: Any) -> str:
        if field_name in NPC_STRING_FIELDS:
            if not isinstance(value, str) or not value.strip():
                raise CreatorError(f"{field_name} 需要非空字符串")
        elif field_name in NPC_LIST_FIELDS:
            value = self._as_str_list(field_name, value)
        else:
            raise CreatorError(
                f"角色字段 {field_name!r} 不可改。可改："
                f"{', '.join(NPC_STRING_FIELDS + NPC_LIST_FIELDS)}"
            )
        p = self._npc_path(npc_id)
        d = _read_yaml(p)
        old = d.get(field_name)
        d[field_name] = value
        _write_yaml(p, d)
        return f"npcs/{npc_id}.yaml: {field_name} 已更新\n旧值：{_brief(old)}\n新值：{_brief(value)}"

    def add_npc(self, npc_id: str, name: str, identity: str, personality: str,
                speech_style: str) -> str:
        p = self.root / "npcs" / f"{_safe_id(npc_id)}.yaml"
        if p.exists():
            raise CreatorError(f"角色 {npc_id!r} 已存在——改它用 update_npc_field")
        _write_yaml(p, {
            "id": npc_id, "name": name, "identity": identity,
            "personality": personality, "speech_style": speech_style,
            "secrets": [], "boundaries": [], "forbidden": [],
        })
        # 返回文本必须**准确**：早先这里写的是"不登记 affections 会被 check-worldpack 拒绝"，
        # 实测是错的——闸门只校验"affection 指向了一个存在的角色卡"这个方向，
        # 反过来（有卡但没有 affection 条目）是**合法**的：那只是一个没有好感数值的
        # 在场角色。给模型一条错的因果，它会去白做一件事，然后发现自己没做错什么。
        return (
            f"已新增角色 npcs/{npc_id}.yaml。它现在就能在剧情里出场。"
            f"若希望它也有好感度（可被攻略/有关系数值），还需要在 `schedule.yaml` 的 "
            f"`affections` 里加一条 `{npc_id}: {{label: {name}, initial: 0}}`"
            f"——**这不是校验要求，是玩法要求**。改完记得 `validate_pack`。"
        )

    def remove_npc(self, npc_id: str) -> str:
        p = self._npc_path(npc_id)
        p.unlink()
        return (
            f"已删除 npcs/{npc_id}.yaml。⚠️ 引用它的地方（mainline 的 present、"
            f"events 的 present、schedule 的 affections）现在会悬空，"
            f"**必须**用 `validate_pack` 看 check-worldpack 怎么说。"
        )

    def upsert_lore(self, lore_id: str, keys: list[str], text: str,
                    secondary_keys: list[str] | None = None,
                    logic: str | None = None) -> str:
        """增改一条世界书条目（`world.yaml` 的 `lore` 数组）。

        键的校验**复用 `lore.validate_key`**（§4.2：「能校验的必能匹配」）——
        它在 `check_worldpack` 里也跑，所以这里只是把错误提前到工具层，
        让模型当场看到"这个键不合法"，而不是等到整体校验。
        """
        from .lore import validate_key

        if not lore_id or not str(lore_id).strip():
            raise CreatorError("lore id 不能为空")
        keys = self._as_str_list("keys", keys)
        if not keys:
            raise CreatorError("keys 至少要有一个触发词")
        for k in keys:
            validate_key(k, kind="keys", owner=f"lore '{lore_id}'", case_sensitive=True)
        for k in secondary_keys or []:
            validate_key(k, kind="secondary_keys", owner=f"lore '{lore_id}'", case_sensitive=True)
        if not str(text or "").strip():
            raise CreatorError("text 不能为空——空条目等于没有")

        w = self.read_world()
        lore = list(w.get("lore") or [])
        entry: dict[str, Any] = {"id": lore_id, "keys": keys, "text": text}
        if secondary_keys:
            entry["secondary_keys"] = list(secondary_keys)
            entry["logic"] = logic or "AND_ANY"
        elif logic:
            entry["logic"] = logic
        for i, item in enumerate(lore):
            if isinstance(item, dict) and item.get("id") == lore_id:
                lore[i] = entry
                w["lore"] = lore
                _write_yaml(self.root / "world.yaml", w)
                return f"世界书条目 {lore_id!r} 已更新（keys={keys}）"
        lore.append(entry)
        w["lore"] = lore
        _write_yaml(self.root / "world.yaml", w)
        return f"世界书条目 {lore_id!r} 已新增（keys={keys}，现有 {len(lore)} 条）"

    @staticmethod
    def _as_str_list(field_name: str, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise CreatorError(f"{field_name} 需要字符串数组")
        return [v for v in value if v.strip()]

    # ---- 校验 ----

    def validate(self) -> tuple[bool, str]:
        """跑闸门。**返回报错原文**——它就是喂给模型修的燃料。

        `load_worldpack` 就是闸门本身（CLI 的 `check-worldpack` 子命令与
        `catalog.can_publish` 用的都是它），所以这里的结论与"能不能发布"**必然一致**
        ——不会出现"Agent 说通过了、发布时却被拒"这种两边口径分叉。
        """
        try:
            load_worldpack(self.root)
        except WorldPackError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001 — 校验器自身的异常也要变成可读文本
            return False, f"内部错误 {type(e).__name__}: {e}"
        return True, "check-worldpack 通过"

    def summary(self) -> str:
        """给模型的现状摘要（世界名 + 角色 + 节点 + 结局 + 世界书条数）。"""
        try:
            pack = load_worldpack(self.root)
        except Exception as e:  # noqa: BLE001 — 坏包也要能给模型一个可读的现状
            return f"（当前包无法加载：{e}）"
        return (
            f"世界《{pack.world.name}》（{pack.world.era}）· "
            f"{len(pack.npcs)} 角色 · {len(pack.mainline.nodes)} 主线节点 · "
            f"{len(pack.endings.endings)} 结局 · 世界书 {len(pack.world.lore)} 条 · "
            f"日程行动 {len(pack.schedule.actions)} 个"
        )


def _safe_id(value: str) -> str:
    if not value or "/" in value or "\\" in value or ".." in value or value.startswith("."):
        raise CreatorError(f"非法 id：{value!r}")
    return value


def _brief(value: Any, limit: int = 120) -> str:
    if value is None:
        return "（原本没有这个字段）"
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# 工具面（§4.2 那张表）
# ---------------------------------------------------------------------------


def build_registry(
    wc: WorkingCopy,
    start_generation: Callable[[str, str, bool], str] | None = None,
) -> ToolRegistry:
    """工作版 → 工具注册表。

    只读工具没有 `rejects`（它们不该拒绝，除了未知 id）；写工具声明 `CreatorError`，
    于是 `dispatch` 会把拒绝变成 `[引擎拒绝] …` 的结构化文本回给模型——
    模型据此修正，而不是让异常打断整轮。

    `start_generation(name, source_text, offline) -> str`（N6 补）由 **HTTP 层注入**：
    起一个生成任务需要任务表与包根目录，那两样归 `web.py` 管。`creator.py` 不 import
    `web`（会成环），也不自己造任务表——**依赖注入在这里不是洁癖**：没有注入时
    （单测、或将来把生成管线关掉）这个工具会以"未接入"拒绝，而不是让模型以为能做。
    """
    reg = ToolRegistry()

    def add(name: str, description: str, parameters: dict, handler: Callable[[dict], str]) -> None:
        reg.register(ToolSpec(
            name=name, description=description, parameters=parameters,
            handler=handler, rejects=(CreatorError, WorldPackError, ValueError),
        ))

    add("list_npcs", "列出本包全部角色的 id / 名字 / 身份。改人之前先看这里。", {
        "type": "object", "properties": {},
    }, lambda a: json.dumps(wc.list_npcs(), ensure_ascii=False))

    add("read_world", "读世界级设定（名字/时代/开场/玩家身份与目标/核心规则/文风/禁用/世界书条目）。", {
        "type": "object", "properties": {},
    }, lambda a: json.dumps(wc.read_world(), ensure_ascii=False, indent=1))

    add("read_npc", "读一张角色卡的全部字段。", {
        "type": "object",
        "properties": {"npc_id": {"type": "string", "description": "角色 id"}},
        "required": ["npc_id"],
    }, lambda a: json.dumps(wc.read_npc(str(a.get("npc_id", ""))), ensure_ascii=False, indent=1))

    add("read_lore", "列出世界书条目（id / 触发词 / 正文摘要），改世界书前先读。", {
        "type": "object", "properties": {},
    }, lambda a: json.dumps(
        [{"id": x.get("id"), "keys": x.get("keys"),
          "text": (x.get("text") or "")[:80]} for x in wc.read_lore()],
        ensure_ascii=False, indent=1))

    add("update_world_field",
        "改一个世界级字段。可改：" + "、".join(WORLD_STRING_FIELDS + WORLD_LIST_FIELDS)
        + "（字符串字段给字符串，列表字段给字符串数组）。改完必须 validate_pack。", {
            "type": "object",
            "properties": {
                "field": {"type": "string", "enum": list(WORLD_STRING_FIELDS + WORLD_LIST_FIELDS)},
                "value": {"description": "新值：字符串或字符串数组"},
            },
            "required": ["field", "value"],
        }, lambda a: wc.update_world_field(str(a.get("field", "")), a.get("value")))

    add("update_npc_field",
        "改一张角色卡的一个字段。可改：" + "、".join(NPC_STRING_FIELDS + NPC_LIST_FIELDS)
        + "（字符串字段给字符串，列表字段给字符串数组）。改完必须 validate_pack。", {
            "type": "object",
            "properties": {
                "npc_id": {"type": "string"},
                "field": {"type": "string", "enum": list(NPC_STRING_FIELDS + NPC_LIST_FIELDS)},
                "value": {"description": "新值：字符串或字符串数组"},
            },
            "required": ["npc_id", "field", "value"],
        }, lambda a: wc.update_npc_field(
            str(a.get("npc_id", "")), str(a.get("field", "")), a.get("value")))

    add("add_npc", "新增一个角色卡。**加完还要在 schedule.yaml 的 affections 里登记**，否则校验不过。", {
        "type": "object",
        "properties": {
            "npc_id": {"type": "string", "description": "角色 id（英文/拼音，会成为文件名）"},
            "name": {"type": "string"},
            "identity": {"type": "string", "description": "身份（如「城南诊所的医生」）"},
            "personality": {"type": "string"},
            "speech_style": {"type": "string"},
        },
        "required": ["npc_id", "name", "identity", "personality", "speech_style"],
    }, lambda a: wc.add_npc(
        str(a.get("npc_id", "")), str(a.get("name", "")), str(a.get("identity", "")),
        str(a.get("personality", "")), str(a.get("speech_style", ""))))

    add("remove_npc", "删除一个角色卡。删完必须 validate_pack（引用它的事件/节点会悬空）。", {
        "type": "object",
        "properties": {"npc_id": {"type": "string"}},
        "required": ["npc_id"],
    }, lambda a: wc.remove_npc(str(a.get("npc_id", ""))))

    add("upsert_lore", "新增或更新一条世界书（lore）条目。keys 是触发词数组，text 是注入正文。", {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "keys": {"type": "array", "items": {"type": "string"}, "description": "触发关键词（命中任一即候选）"},
            "text": {"type": "string", "description": "命中后注入给模型的设定正文"},
            "secondary_keys": {"type": "array", "items": {"type": "string"}},
            "logic": {"type": "string", "description": "有 secondary_keys 时的逻辑：AND_ANY / AND_ALL / NOT_ANY / NOT_ALL"},
        },
        "required": ["id", "keys", "text"],
    }, lambda a: wc.upsert_lore(
        str(a.get("id", "")), a.get("keys"), str(a.get("text", "")),
        a.get("secondary_keys"), a.get("logic")))

    add("validate_pack",
        "跑 check-worldpack 并把**报错原文**返回给你。每次改完内容都要调用；"
        "有错就按原文修，改完再调一次。**不要**在没通过校验时告诉用户「改好了」。", {
            "type": "object", "properties": {},
        }, lambda a: _validate_text(wc))

    add("diff_pack", "显示工作版相对本次会话开始时的全部改动（给用户确认用）。", {
        "type": "object", "properties": {},
    }, lambda a: wc.diff() or "（还没有任何改动）")

    # ---- 从素材起一张新卡（对话式做卡的入口）----
    #
    # **为什么是"转交"而不是在这里跑**：生成是**分钟级后台任务**（8 块提取 × 修复轮），
    # 不能塞进一次工具调用里同步等——那会把一轮对话挂死几分钟，而用户看不到任何进度。
    # 所以这个工具只做三件事：校验参数 → 把任务丢进已有的任务表 → 告诉模型**别等**。
    # 进度、取消、结果全部复用 N1 那套（工作台会收到一个 `job` 事件并自动切到进度页签）。
    if start_generation is None:
        reg.register(ToolSpec(
            name="start_generation",
            description="从素材（小说/剧本/大纲正文）起一个新世界包。生成是后台任务，需要几分钟。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "新包名（目录名：中文/字母/数字/_/-）"},
                    "source_text": {"type": "string", "description": "素材正文（不是文件路径）"},
                },
                "required": ["name", "source_text"],
            },
            handler=None,
            disabled_msg=(
                "[协议错误] 这个部署没有接入生成管线——请让用户在工作台的"
                "「从素材生成一张卡」页签里粘贴素材。**不要**假装你能生成。"
            ),
        ))
    else:
        def _start(a: dict) -> str:
            name = str(a.get("name", "")).strip()
            text = str(a.get("source_text", ""))
            offline = bool(a.get("offline", False))
            if not name:
                raise CreatorError("包名不能为空")
            if not text.strip():
                raise CreatorError("素材为空：把小说/剧本/大纲的正文给全")
            return start_generation(name, text, offline)

        add(
            "start_generation",
            "从素材（小说/剧本/大纲的正文）起一个**新**世界包。"
            "生成是**分钟级后台任务**：调用后**立刻返回**一个任务号，"
            "用户会在「从素材生成」页签看到进度。"
            "**调用后不要等、也不要反复查**——告诉用户去那个页签看进度即可。"
            "真实生成会花钱（约 ¥0.1–0.3），只在用户明确要求做新卡时调用；"
            "素材是**文本**，不接受文件路径。",
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "新包名（只能是目录名：中文/字母/数字/下划线/连字符）",
                    },
                    "source_text": {
                        "type": "string",
                        "description": "素材正文（整段粘贴进来，超长会自动截断到 30 万字）",
                    },
                    "offline": {
                        "type": "boolean",
                        "description": "true = 离线试跑（内嵌假模型，不花钱）；默认 false",
                    },
                },
                "required": ["name", "source_text"],
            },
            _start,
        )

    return reg


def _validate_text(wc: WorkingCopy) -> str:
    ok, text = wc.validate()
    return f"[✓] {text}" if ok else f"[✗] 校验未通过：\n{text}"


CREATOR_SYSTEM = """你是文字养成游戏的世界包编辑器。用户在和你讨论怎么改一张卡（世界包）。

**你的工作对象是一份草稿（工作版）**，原始包只读。改动要经过校验才能发布，所以：

1. **先读再改**。不知道现状就先调用 `list_npcs` / `read_world` / `read_npc` / `read_lore`。
   **不要凭空猜字段里原来写了什么**，也不要假设某个角色存在。
2. **每次改完必须调用 `validate_pack`**。它会跑 check-worldpack 并把报错原文给你。
   有错就按原文改，再校验一次。**没通过校验就不要说"改好了"**——
   宁可如实说"还差一步，卡在……"。
3. **只改字段白名单里的字段**。白名单外的字段（数值、主线结构、结局条件等）
   目前不开放编辑，遇到这类请求要明说"这版还不支持改 X"，不要硬来。
4. **改动要小步、可回退**。一次只改用户说的那一处；不要顺手"优化"别的地方。
5. 用中文回答，简洁。改完之后一句话说清**改了什么、校验结果如何**，
   需要用户确认的（比如结构性改动）指出来让他看 diff。

常见坑（都是真实撞过的）：
- `add_npc` 之后还必须在 `schedule.yaml` 的 `affections` 里登记，否则校验不过；
- `remove_npc` 之后引用它的事件/节点会悬空，校验会报出来；
- 世界书条目的 keys 不能是空串，也不能全是标点。

你不知道怎么改时，**问清楚再动手**——比猜错一遍再回滚便宜。"""


def build_creator_llm(settings, tracker: "UsageTracker | None" = None):
    """按 Settings 造一个创作者用的 `LLMClient`（purpose="creator"）。

    复用 `LLMClient.from_settings`（模型路由 / usage / trace 只有一份实现），
    只把 `tools` 传空——**工具 schema 每轮由工作版现算**（角色列表会变），
    不能像叙事线那样在构造时定死。`complete_with_tools` 收的是显式 tools 参数。

    与 `worldgen.build_llm` 的分工：那个是"离线假 LLM / 真机"二选一，
    这个是单一真机路径（创作者 Agent 没有离线模式的意义——它改的是真草稿）。
    """
    from .llm import LLMClient

    return LLMClient.from_settings(settings, [], tracker=tracker)


@dataclass
class CreatorTurn:
    """一轮对话的结果。"""

    reply: str
    tools_used: list[dict] = field(default_factory=list)
    validate_ok: bool = True
    validate_text: str = ""
    changed: list[str] = field(default_factory=list)
    truncated: bool = False  # 达到迭代上限（这一轮没做完），不是错误但要说清


@dataclass
class CreatorSession:
    """一次创作会话（进程内，按草稿名索引）。

    只持有**对话历史**与**基线快照**；工作版就在磁盘上的草稿目录里，
    所以"刷新页面"不会丢改动，丢的只是对话上下文与基线（如实告诉用户）。
    """

    name: str
    wc: WorkingCopy
    messages: list[dict] = field(default_factory=list)
    baseline_note: str = ""
    turns: int = 0

    @classmethod
    def open(cls, draft_dir: Path, name: str) -> "CreatorSession":
        wc = WorkingCopy(root=draft_dir)  # 基线在 __post_init__ 里取
        s = cls(name=name, wc=wc)
        s.baseline_note = (
            f"已记下当前状态作为基线（{len(wc.baseline)} 个文件）；"
            f"diff 显示的是**本次会话以来**的改动。"
        )
        s.messages = [{"role": "system", "content": CREATOR_SYSTEM}]
        return s

    def history(self) -> list[dict]:
        """给前端的对话记录（去掉 system 与工具细节，只留人话）。"""
        return [m for m in self.messages if m.get("role") in ("user", "assistant")
                and m.get("content")]

    def send(self, text: str, llm, *, on_event: Callable[[dict], None] | None = None,
             start_generation: Callable[[str, str, bool], str] | None = None) -> CreatorTurn:
        """跑一轮：模型 ↔ 工具循环。`on_event` 用来把过程推给前端（SSE）。"""
        emit = on_event or (lambda ev: None)
        self.messages.append({"role": "user", "content": text})
        # 现状摘要每轮刷新：模型改完一轮之后看到的是最新状态，不用它自己问
        self.messages.append({
            "role": "system",
            "content": f"【当前工作版】{self.wc.summary()}",
        })
        registry = build_registry(self.wc, start_generation=start_generation)
        tools = registry.schemas()
        turn = CreatorTurn(reply="")

        for i in range(1, MAX_CREATOR_ITERS + 1):
            msg = llm.complete_with_tools(self.messages, tools)
            content = getattr(msg, "content", None) or ""
            tool_calls = getattr(msg, "tool_calls", None)
            self.messages.append(_assistant_dict(msg))
            if content.strip():
                emit({"type": "text", "text": content})
                turn.reply = content.strip()

            if not tool_calls:
                # **不调工具 = 说完了**。创作者循环没有 submit_* 收尾协议：
                # 模型停下来就是在回答用户（见模块 docstring 的对照表）。
                return self._finish(turn)

            if i == MAX_CREATOR_ITERS:
                turn.truncated = True
                self.messages.append({
                    "role": "user",
                    "content": "（已达本轮工具调用上限，请直接用一句话总结现在的状态。）",
                })
                break

            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as e:
                    result = f"[引擎拒绝] 参数不是合法 JSON：{e}"
                    status = "rejected"
                else:
                    res = registry.dispatch(name, args)
                    result = res.message
                    status = res.status
                self.messages.append({
                    "role": "tool", "tool_call_id": tc.id, "content": result,
                })
                record = {"name": name, "status": status, "result": result[:600]}
                turn.tools_used.append(record)
                emit({"type": "tool", "name": name, "status": status,
                      "result": result[:400]})

        # 上限内没结束：再要一句总结（不调工具就返回）
        try:
            msg = llm.complete_with_tools(self.messages, tools)
            content = getattr(msg, "content", None) or ""
            self.messages.append(_assistant_dict(msg))
            if content.strip():
                turn.reply = content.strip()
                emit({"type": "text", "text": content})
        except Exception as e:  # noqa: BLE001 — 收尾失败不该吞掉已完成的改动
            turn.reply = turn.reply or f"（这一轮工具调用达上限，且收尾失败：{e}）"
        return self._finish(turn)

    def _finish(self, turn: CreatorTurn) -> CreatorTurn:
        self.turns += 1
        turn.validate_ok, turn.validate_text = self.wc.validate()
        turn.changed = self.wc.changed_files()
        if not turn.reply:
            turn.reply = "（模型这一轮没有输出文字）"
        return turn


def _assistant_dict(msg: Any) -> dict:
    """assistant 消息 → 可回灌的 dict（含 tool_calls）。与 `llm._assistant_to_dict` 同形状，
    但创作者线不需要 origin 附加信息，故独立实现、不import 私有函数。"""
    d: dict[str, Any] = {"role": "assistant", "content": getattr(msg, "content", None) or ""}
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ]
    return d

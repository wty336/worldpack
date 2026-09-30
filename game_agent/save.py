"""存档读档（W7）：GameState + 对话历史 中立格式落盘（design.md §11）。

M1.5 起存档包含对话历史（history）：读档后 NPC 完整记得之前的对话，剧情连续。
M2b 完成压缩后，将升级为「近窗历史 + 剧情摘要」的更优方案（存档体积与上下文成本可控）。

**版本与回合轴对账**（K 系列）：存档带 `save_version`。此前没有版本字段，靠
`data.get(...)` 缺省值做事实上的迁移——对"新增字段"够用，但 `turn_count` 已经从
"记忆追踪用的计数"升级成**成本归因的时间轴**（trace 的 `game_turn`、usage 的
`game_turn` 都用它）。旧存档里没有这个字段 → 读档后从 0 重新计数，而 `usage.jsonl`
是**追加**的：同一文件里两段 `game_turn` 会重复且错位，`--by-game-turn` 把两段
合成一行，成本静默算错。故读档时显式对账（见 `resolve_turn_count`）。

**剧本身份戳**（G2，v3）：存档带 `pack: {id, digest}`。此前存档不含剧本身份，
后果是作者改版已发布剧本（改 NPC id、改旗标名）后，旧档被读进新版剧本 →
`GameState` 里的旗标/NPC 好感**在包内不存在** → 静默穿帮（NPC 失忆、条件永不触发、
结局判不出）。现在读档前校验，不一致**拒绝并提示**，不静默降级。
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import GameState

SAVE_VERSION = 3  # 1 = 无 save_version；2 = 带版本与回合轴对账；3 = 带剧本身份戳（G2）
SAVE_MARK = "【剧情摘要】"  # 与 compression.SUMMARY_MARK 同字面量（避免循环导入）
_HISTORY_TAIL = 200  # 对账扫描的历史尾部条数（读档只读尾部，不做全量统计）


class PackMismatchError(ValueError):
    """存档与当前世界包不是同一份内容（G2）。

    继承 `ValueError`：CLI/Web 的读档路径本来就把 `ValueError` 当"用户可纠正的
    输入问题"处理（转 error 帧 / 400），因此这里不需要新的异常分支，
    但调用方仍可按类型精确捕获。
    """


def _player_turns_in_tail(data: dict) -> int:
    """历史尾部的玩家回合数（A-2 口径：无 `name` 标记的 user 消息 = 玩家回合）。

    只作**对账下界**用：压缩会把多轮折成一条摘要，故真实 `turn_count` 可能更大
    ——所以取 ``max(记录值, 尾部回合数)``，宁可偏低也不凭空放大。
    """
    history = data.get("history") or []
    return sum(
        1
        for m in history[-_HISTORY_TAIL:]
        if isinstance(m, dict) and m.get("role") == "user" and not m.get("name")
    )


def resolve_turn_count(data: dict) -> int | None:
    """读档时解析 `turn_count`；**旧存档（无版本字段）返回修正值，新存档返回 None**。

    返回 None = 存档自带权威值，调用方直接用（不覆盖）。
    返回 int = 旧存档缺这个字段，用"历史尾部玩家回合数"作为对账下界。

    为什么不下调已有的非零值：`turn_count` 只增不减（`_txn_rollback` 也不回滚它），
    若存档里已有值就说明是新的（1.x 时代偶然写进去的）：那是真值，只补不漏。
    """
    if data.get("save_version") is not None:
        return None  # 新存档：turn_count 是权威值
    recorded = data.get("turn_count")
    tail_turns = _player_turns_in_tail(data)
    if isinstance(recorded, int) and recorded > 0:
        return max(recorded, tail_turns)
    return tail_turns if tail_turns > 0 else None


def _read(path: str | Path) -> dict:
    """读存档 JSON 一次（避免 load_game/load_history 各读一遍时的重复解析口径分叉）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"存档格式非法（顶层不是对象）: {path}")
    return data


def _pack_meta_from(data: dict) -> dict[str, str] | None:
    """从已解析的存档数据里取身份戳；缺失/形状不对返回 None（旧存档）。"""
    meta = data.get("pack")
    if not isinstance(meta, dict):
        return None
    pid, digest = meta.get("id"), meta.get("digest")
    if not isinstance(pid, str) or not isinstance(digest, str):
        return None
    return {"id": pid, "digest": digest}


def saved_pack_meta(path: str | Path) -> dict[str, str] | None:
    """读存档里的剧本身份戳；旧存档（G2 之前，无 `pack` 字段）返回 None。"""
    return _pack_meta_from(_read(path))


def save_summary(path: str | Path) -> dict:
    """存档摘要（E-9）：**只读顶层字段，不构造 GameState**。

    存档列表要给玩家看的是"哪一局、哪个剧本、多少回合、第几天"，
    不是把整局状态反序列化一遍。`history` 只报条数，不回传内容。
    """
    p = Path(path)
    data = _read(p)
    history = data.get("history")
    return {
        "path": p.name,  # 只回裸文件名：Web 侧的存档路径本来就是裸名（`_safe_save_path`）
        "save_version": data.get("save_version"),
        "pack": _pack_meta_from(data),
        # `pack_name` 是状态里自报的世界名。**旧档没有身份戳**（`pack` 为 None），
        # 只靠 id 无从辨认它属于哪张卡——列出世界名是让玩家能自己认出来的最低成本，
        # 也是"读档被拒"时把话说清楚所必需的（见 `state_pack_mismatch`）。
        "pack_name": data.get("pack_name"),
        "turn_count": data.get("turn_count"),
        "day": data.get("day"),
        "scene": data.get("scene"),
        "history": len(history) if isinstance(history, list) else 0,
        "size": p.stat().st_size,
        "mtime": int(p.stat().st_mtime),
    }


def state_pack_mismatch(state: GameState, pack) -> str | None:
    """状态与当前世界包**内容级不兼容**的原因；兼容则返回 None。

    **为什么光有身份戳不够**（2026-10 实测缺陷）：G2 之前的存档没有 `pack` 字段，
    `check_pack_identity` 按"只补不漏"放行——于是把 A 包的档读进 B 包时，
    `state.stats` 里的键（如 `charm` / `grace`）在 B 包的 `schedule.stats` 里不存在，
    `context.status_text()` 的 `self.pack.schedule.stats[k]` 直接 **KeyError → HTTP 500**。
    玩家看到的是一句"读取状态失败：HTTP 500"，而真正的原因是"拿 A 卡的档读了 B 卡"。

    所以这里不看存档自报的身份，只看**内容能不能对上**——同时覆盖有戳的档（双保险）、
    无戳的旧档、以及手工改过的档。

    口径与 `audit.audit_stats` 一致（属性 / 好感 / 计数器 + NPC 引用），
    避免"引用完整性"这一个概念出现两套判定。
    """
    reasons: list[str] = []

    unknown_stats = sorted(set(state.stats) - set(pack.schedule.stats))
    if unknown_stats:
        reasons.append(f"属性 {unknown_stats}")
    unknown_aff = sorted(set(state.affections) - set(pack.schedule.affections))
    if unknown_aff:
        reasons.append(f"好感对象 {unknown_aff}")
    unknown_counters = sorted(set(state.counters) - set(pack.schedule.counters))
    if unknown_counters:
        reasons.append(f"计数器 {unknown_counters}")
    unknown_items = sorted(set(state.items) - {i.id for i in pack.schedule.items})
    if unknown_items:
        reasons.append(f"物品 {unknown_items}")

    # NPC 引用：好感/记忆/洞察/在场都以 npc id 为键，缺一个就会在渲染角色卡时炸
    known_npcs = set(pack.npcs)
    for label, keys in (
        ("记忆中的角色", set(state.npc_memories)),
        ("洞察中的角色", set(state.npc_insights)),
        ("在场角色", set(state.present_npcs)),
    ):
        unknown = sorted(keys - known_npcs)
        if unknown:
            reasons.append(f"{label} {unknown}")

    if not reasons:
        return None
    # 结论先行、证据随后：玩家第一眼要看到的是"该选哪张卡"，而不是一串键名。
    return (
        f"这个存档属于《{state.pack_name}》，而当前在玩《{pack.world.name}》——不是同一份内容"
        f"（多半来自另一张卡，或是某张卡改版前的旧档）。"
        f"对不上的内容：{'、'.join(reasons)}。"
        f"请改选该卡后再读档，或另开新局。"
    )


def check_pack_identity(path: str | Path, pack_meta: dict[str, str] | None) -> None:
    """读档前校验剧本身份；不一致抛 `PackMismatchError`。

    **只做"两侧都有才比"**（与 K 系列 `resolve_turn_count` 同一纪律，只补不漏）：
    - 存档是旧格式（无 `pack`）→ 放过（不能因为存档早于本机制就拒绝玩家的档）；
    - 调用方没提供当前包身份（脚本/测试的裸 `load_game`）→ 放过；
    - 两侧都有 → 比 `id` 与 `digest`，任一不同即拒绝。

    为什么 `id` 不同也要拒：包名相同但内容不同（改版）由 `digest` 抓；
    包名不同而内容相同（复制了一份）则应当拒——`turn_count`/`game_turn` 归因、
    存档列表的"按剧本隔离"都以 id 为准，混着用会让成本账对不上。
    """
    if pack_meta is None:
        return
    saved = saved_pack_meta(path)
    if saved is None:
        return  # 旧存档：无身份可比，不拒绝
    if saved == pack_meta:
        return
    raise PackMismatchError(
        f"存档与当前剧本不是同一份内容——"
        f"存档 {saved['id']}@{saved['digest'][:8]}，当前 {pack_meta['id']}@{pack_meta['digest'][:8]}。"
        f"剧本改版后旧档不通用（NPC id / 旗标可能已不在包内，读进去会静默穿帮）。"
        f"请改用该存档所属版本的世界包，或另开新局。"
    )


def save_game(
    state: GameState,
    path: str | Path,
    history: list[dict] | None = None,
    pack_meta: dict[str, str] | None = None,
) -> None:
    """存档。history 为会话消息列表（纯 JSON 可序列化），可选但强烈建议传入。

    `pack_meta`（G2）为当前世界包的 `{id, digest}`（见 `worldpack.pack_meta`）；
    缺省不写身份戳（脚本的中间产物/崩溃转储不需要，也不该被读档校验拦住）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = state.to_dict()
    data["save_version"] = SAVE_VERSION  # 版本随数据走，读档据此决定是否对账
    if pack_meta is not None:
        data["pack"] = dict(pack_meta)
    if history is not None:
        data["history"] = history
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_game(path: str | Path, pack_meta: dict[str, str] | None = None) -> GameState:
    """读档：只恢复 GameState（history 字段由 load_history 单独读取）。

    K 系列：旧存档（无 `save_version`）在此对账 `turn_count`，使 trace/usage 的
    `game_turn` 在**跨存档**时仍是一条单调时间轴。
    G2：给出 `pack_meta` 时先校验剧本身份，不一致抛 `PackMismatchError`
    （**在构造 GameState 之前**，因此不会留下半应用的状态）。
    """
    check_pack_identity(path, pack_meta)
    data = _read(path)
    state = GameState.from_dict(data)
    reconciled = resolve_turn_count(data)
    if reconciled is not None:
        state.turn_count = reconciled
    return state


def load_history(path: str | Path) -> list[dict]:
    """读取存档中的对话历史；旧版存档（无 history 字段）返回空列表。"""
    return list(_read(path).get("history", []))

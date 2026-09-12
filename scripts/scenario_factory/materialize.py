"""材料装配器：卡 → GameState → ContextBuilder.status_text（§4.1，生产同形）。

**硬纪律**：必须复用生产组装器（`ContextBuilder`），不得另写材料模板——与
`plan-phase1-data.md` §4.2.2 开头"输入模板必须逐字对齐生产调用"是同一条纪律，
只是它落在材料侧。复用组装器只保证"组装器"同形；**"输入"同形**由本模块负责：

- `material.recent`（决策 17）：`status_text` 的 `rank_facts` / `select_lore` 都吃
  `context = scene + node.goal + recent`（`context.py:181`），生产环境的 recent 来自
  最近 2 条真实玩家发言；工厂不给 recent 就会让**排序输入与生产不同形**；
- `material.facts`（下标）：`None` = 缺省取 `facts[in_material=true]`；`[]` = 显式一条不写。

三道校验（§4.1）：① 在场角色卡确落材料；② in_material 事实的 anchors **全部**在位；
③ confab 的 anchors **不得**出现在材料中（泄漏即等于给判官开出第二条通路，
退回踩坑 #17 的老坑）。
"""
from __future__ import annotations

from game_agent.context import ContextBuilder
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import WorldPack, load_worldpack

from .cards import REPO_ROOT, ScenarioCard


class MaterializeError(ValueError):
    """材料装配或校验失败——调用方丢弃该样本并计数（§4.1）。"""


_PACK_CACHE: dict[str, WorldPack] = {}


def load_pack(pack_path: str) -> WorldPack:
    """带缓存的包加载：同批上千张卡只解析一次 YAML（否则每张卡都重读全部 yaml）。"""
    if pack_path not in _PACK_CACHE:
        _PACK_CACHE[pack_path] = load_worldpack(REPO_ROOT / pack_path)
    return _PACK_CACHE[pack_path]


def build_material(card: ScenarioCard) -> str:
    """卡 → 材料文本（与生产 `status_text` 逐字同形）。失败抛 MaterializeError。"""
    if not card.pack or not card.material:
        raise MaterializeError("judge/compress 卡必须带 pack + material")
    pack = load_pack(card.pack)
    m = card.material
    _precheck(card, pack)          # 包级前置：错就早抛，不做无用的组装
    state = GameState.from_pack(pack)
    state.day, state.scene = m.day, m.scene
    state.present_npcs = list(m.present)
    for k, v in m.affections.items():
        state.affections[k] = float(v)   # _precheck 已保证键存在，不再静默跳过
    idx = m.facts if m.facts is not None else [
        i for i, f in enumerate(card.facts) if f.in_material
    ]
    state.player_facts = [
        MemoryEntry(fact=card.facts[i].text, day=m.day, round=0,
                    importance=card.facts[i].importance)
        for i in idx
    ]
    state.npc_memories = {
        k: [MemoryEntry(fact=t, day=m.day, round=0) for t in v]
        for k, v in m.memories.items()
    }
    text = ContextBuilder.from_pack(pack).status_text(state, None, recent=m.recent)
    _check_text(card, pack, text)
    return text


def _precheck(card: ScenarioCard, pack: WorldPack) -> None:
    """包级前置校验：这些错**无法在材料里表达**，必须早抛 —— 否则静默产出坏标签。

    背景（评审指出、已实测）：旧实现把好感覆写写成 `if k in state.affections:`，
    而 `state.affections` 只含 `schedule.yaml` 列出的对象（`state.py:103`）——
    于是"卡要 45、包没有该好感轨"时**静默跳过**，而 `status_text` 用
    `state.affections.get(npc.id, 0.0)` 渲染在场角色卡的语气档，结果材料会
    **声明过 45 却按 0 档渲染语气**，同时把该 NPC 从好感行里整体略过
    —— 材料与卡片意图矛盾 = 坏标签（T2 语气冲突卡的整条通路就是好感档）。
    """
    m = card.material
    for npc_id in m.present:
        if npc_id not in pack.npcs:
            raise MaterializeError(f"present 的 NPC 不在包里: {npc_id}")
        if npc_id not in pack.schedule.affections:
            raise MaterializeError(
                f"present 的 NPC 没有好感轨（schedule.affections 未声明）: {npc_id}——"
                "语气档会按 0.0 兜底、好感行会略过它，材料与卡片意图不符")
    undeclared = [k for k in m.affections if k not in pack.schedule.affections]
    if undeclared:
        raise MaterializeError(
            f"material.affections 声明了包里没有好感轨的对象: {undeclared}——"
            "该覆写无法落地，语气档会按 0.0 兜底渲染")


def _check_text(card: ScenarioCard, pack: WorldPack, text: str) -> None:
    m = card.material
    if not text.strip():
        raise MaterializeError("材料为空")
    for npc_id in m.present:  # 校验①：在场 NPC 角色卡确落材料
        if pack.npcs[npc_id].name not in text:
            raise MaterializeError(f"在场角色卡未落入材料: {npc_id}")
    for f in card.facts:  # 校验②在位（全部 anchors）/ ③泄漏（任一 anchors 出现即泄漏）
        hit = [a for a in f.anchors if a in text]
        if f.in_material and len(hit) != len(f.anchors):
            raise MaterializeError(f"in_material 事实 anchors 未在材料中: {f.anchors}")
        if not f.in_material and hit:
            raise MaterializeError(f"材料泄漏：confab anchors 出现在材料中: {hit}")

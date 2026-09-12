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
    state = GameState.from_pack(pack)
    state.day, state.scene = m.day, m.scene
    state.present_npcs = list(m.present)
    for k, v in m.affections.items():
        if k in state.affections:
            state.affections[k] = float(v)
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
    _check(card, pack, text)
    return text


def _check(card: ScenarioCard, pack: WorldPack, text: str) -> None:
    m = card.material
    if not text.strip():
        raise MaterializeError("材料为空")
    for npc_id in m.present:  # 校验①：在场 NPC 角色卡确落材料
        if npc_id not in pack.npcs:
            raise MaterializeError(f"present 的 NPC 不在包里: {npc_id}")
        if pack.npcs[npc_id].name not in text:
            raise MaterializeError(f"在场角色卡未落入材料: {npc_id}")
    for f in card.facts:  # 校验②在位（全部 anchors）/ ③泄漏（任一 anchors 出现即泄漏）
        hit = [a for a in f.anchors if a in text]
        if f.in_material and len(hit) != len(f.anchors):
            raise MaterializeError(f"in_material 事实 anchors 未在材料中: {f.anchors}")
        if not f.in_material and hit:
            raise MaterializeError(f"材料泄漏：confab anchors 出现在材料中: {hit}")

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

import sys

from game_agent.context import ContextBuilder
from game_agent.state import GameState, MemoryEntry
from game_agent.worldpack import WorldPack, load_worldpack

from .cards import REPO_ROOT, ScenarioCard

# card_hook_check 不是包成员（脚本层）：注入 scripts/ 后 import（同 assemble.py 的先例）
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from card_hook_check import CARD_FIELDS  # noqa: E402


class MaterializeError(ValueError):
    """材料装配或校验失败——调用方丢弃该样本并计数（§4.1）。"""


_PACK_CACHE: dict[str, WorldPack] = {}


def load_pack(pack_path: str) -> WorldPack:
    """带缓存的包加载：同批上千张卡只解析一次 YAML（否则每张卡都重读全部 yaml）。"""
    if pack_path not in _PACK_CACHE:
        _PACK_CACHE[pack_path] = load_worldpack(REPO_ROOT / pack_path)
    return _PACK_CACHE[pack_path]


def speaker_card_text(pack: WorldPack, speaker: str) -> str:
    """说话人角色卡的四个字段拼成一段（与 `hook_gate` 的比对口径同源：`card_hook_check.CARD_FIELDS`）。

    **放在这里**（而不是演绎器或出库器里）的原因：它是"说话人是谁、该怎么说话"的**单一真源**，
    两头都要用 —— 演绎器拿它**指导生成**（结构性修复：让矛盾真的落在被声明的说话人身上），
    出库器把它**随样本交付**（供 §7.5 标签校验与人工审计）。
    """
    spec = pack.npcs.get(speaker)
    if spec is None:
        return ""
    return "\n".join(f"{f}：{getattr(spec, f, '')}" for f in CARD_FIELDS)


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
    """材料三道校验（§4.1）：① 在场角色卡确落材料；② 声明入材料的事实 anchors 全在位；
    ③ **被断言的那条**事实的 anchors 不得出现在材料中（否则等于给判官开出第二条通路）。

    ⚠️ ③ 的口径 2026-09-14 **收窄**（原为"任一 `in_material=False` 的事实"）：
    原口径下 confab 卡必须**一条事实都不写进材料**（`cards.py:340` + 其校验强制
    "confab 卡的 facts 全部须 in_material: false"）→ 材料的「关键事实」段整体消失，后果三条：

    1. **材料形状 100% 泄露类别**：实测 train 457/457、dev 491/491 命中"材料无事实段 ⇒ 虚构事实"，
       **零误报** ⇒ 判官**不读叙事**就能判出 confab（而生产里材料永远有事实段 → 捷径在生产里失效）；
    2. 与生产分布不一致（模型只见过"空材料版"的 confab）；
    3. confab 的材料空间塌成 **42 种**（setting/ooc 分别 631/675 种）—— 最该补的一族上下文最单调。

    真正的硬要求只有一条：**被断言（`corruptions[0].target_fact`）那条事实的 anchors 不得入材料**。
    其余事实写不写是自由的（由 `material.facts` 显式指定；缺省仍按 `in_material` 标志，故
    **旧卡照旧通过**、新渲染被允许）。
    """
    m = card.material
    if not text.strip():
        raise MaterializeError("材料为空")
    for npc_id in m.present:  # 校验①：在场 NPC 角色卡确落材料
        if pack.npcs[npc_id].name not in text:
            raise MaterializeError(f"在场角色卡未落入材料: {npc_id}")
    shown = (set(m.facts) if m.facts is not None
             else {i for i, f in enumerate(card.facts) if f.in_material})
    asserted = (card.corruptions[0].target_fact
                if card.corruptions and card.corruptions[0].category == "confab" else None)
    for i, f in enumerate(card.facts):
        hit = [a for a in f.anchors if a in text]
        if i in shown and len(hit) != len(f.anchors):      # 校验②：声明入材料 → anchors 须全在位
            raise MaterializeError(f"声明入材料的事实 anchors 未在材料中: {f.anchors}")
        if i not in shown and hit:                          # 校验③：未声明 → 不得出现
            raise MaterializeError(f"材料泄漏：未声明入材料的事实 anchors 出现在材料中: {hit}")
        if i == asserted and hit:                           # 硬要求：被断言的事实绝不得入材料
            raise MaterializeError(f"材料泄漏：**被断言**的事实的 anchors 出现在材料中: {hit}")

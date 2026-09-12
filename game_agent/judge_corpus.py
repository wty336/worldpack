"""E1 Judge 灵敏度语料：中性 schema 与加载器（P0 / improvement-roadmap §7 E1）。

C-1（审查修复 M6）：语料数据是内容层资产，已迁出引擎层——
存放于世界包的 `judge_corpus.yaml`（随世界包生命周期），引擎层只保留
**与世界包无关**的 JudgeCase schema、加载器与材料构造。

用途：Judge（judge.py）只对照「给定材料」判定。生产实况中材料 = ContextBuilder
的 status_text（场景卡 + 状态栏 + 关键事实 + 在场角色卡），**不含** flags /
禁用词表 / 世界规则。因此语料的每条用例都自带状态构造（build_materials），
保证违规点在该材料内可判定——否则测量的是「材料缺失」而非「判据钝」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .context import ContextBuilder
from .state import GameState, MemoryEntry
from .worldpack import WorldPack

ADVERSARIAL_CATEGORIES = ("ooc", "setting", "confab")
NORMAL_CATEGORY = "normal"
CATEGORIES = (*ADVERSARIAL_CATEGORIES, NORMAL_CATEGORY)


@dataclass(frozen=True)
class JudgeCase:
    """一条 Judge 语料：叙事 + 状态覆盖（材料据此构造）。expected=True 表示应当通过。"""

    id: str
    category: str
    narration: str
    expected: bool
    day: int = 1
    scene: str = ""
    present: tuple[str, ...] = ()
    affections: dict[str, float] = field(default_factory=dict)
    facts: tuple[str, ...] = ()
    npc_memories: dict[str, tuple[str, ...]] = field(default_factory=dict)
    note: str = ""


def load_corpus(pack_root: str | Path) -> list[JudgeCase]:
    """从世界包加载语料：手写 `judge_corpus.yaml` + 可选机器生成的 `judge_corpus.gen.yaml`。

    - 手写文件缺失/结构非法 → 抛错（门禁资产必须显式）；
    - 生成文件（Phase 1 扩域，见 `scripts/build_judge_corpus.py`）存在即合并，
      且 **id 必须全局唯一**——重复即抛错，避免"手写与生成撞名"悄悄改变口径。
    """
    pack_dir = Path(pack_root)
    path = pack_dir / "judge_corpus.yaml"
    if not path.exists():
        raise FileNotFoundError(f"语料文件缺失: {path}")

    sources = [path]
    gen_path = pack_dir / "judge_corpus.gen.yaml"
    if gen_path.exists():
        sources.append(gen_path)

    cases: list[JudgeCase] = []
    seen: dict[str, str] = {}
    for src in sources:
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise ValueError(f"语料文件格式非法（需顶层 cases 列表）: {src}")
        for i, item in enumerate(data["cases"]):
            if not isinstance(item, dict):
                raise ValueError(f"{src} 第 {i} 条不是映射: {item!r}")
            try:
                case = JudgeCase(
                    id=str(item["id"]),
                    category=str(item["category"]),
                    narration=str(item["narration"]),
                    expected=bool(item["expected"]),
                    day=int(item.get("day", 1)),
                    scene=str(item.get("scene", "")),
                    present=tuple(str(p) for p in item.get("present", [])),
                    affections={str(k): float(v) for k, v in item.get("affections", {}).items()},
                    facts=tuple(str(f) for f in item.get("facts", [])),
                    npc_memories={
                        str(k): tuple(str(m) for m in v)
                        for k, v in item.get("npc_memories", {}).items()
                    },
                    note=str(item.get("note", "")),
                )
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"{src} 第 {i} 条非法: {e}") from e
            if case.id in seen:
                raise ValueError(
                    f"语料 id 重复: {case.id!r}（{seen[case.id]} 与 {src.name}）"
                )
            seen[case.id] = src.name
            cases.append(case)

    if not cases:
        raise ValueError(f"语料为空: {path}")
    return cases


def build_materials(pack: WorldPack, case: JudgeCase) -> str:
    """按用例状态构造 Judge 材料（与生产同款：ContextBuilder.status_text）。"""
    state = GameState.from_pack(pack)
    state.day = case.day
    state.scene = case.scene
    state.present_npcs = list(case.present)
    for k, v in case.affections.items():
        if k in state.affections:
            state.affections[k] = float(v)
    state.player_facts = [MemoryEntry(fact=f, day=case.day, round=0) for f in case.facts]
    state.npc_memories = {
        k: [MemoryEntry(fact=f, day=case.day, round=0) for f in v]
        for k, v in case.npc_memories.items()
    }
    return ContextBuilder.from_pack(pack).status_text(state, None)


def majority_hit(verdicts: list[bool | None]) -> bool | None:
    """多数票：以「被拦截」为阳性。``None`` = 该轮无法判定（空响应/截断/异常）。

    - 未知轮**不进分母**（否则空响应会被当成"未拦截"，系统性低估拦截率）；
    - 全部未知 → 返回 ``None``：该用例不可判定，**不是**"未拦截"。
    """
    known = [v for v in verdicts if v is not None]
    if not known:
        return None
    flagged = sum(1 for v in known if not v)
    return flagged >= (len(known) + 1) // 2

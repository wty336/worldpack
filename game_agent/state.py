"""游戏状态模型（W3）：引擎的唯一真值容器，纯数据 + 序列化。

设计要点（design.md §5、§11）：
- 数值/flags 只经 StatsSystem 变更，其余代码只读；
- choice_log / stat_log 只增不改（append-only），供多结局回溯与零偏差审计；
- to_dict/from_dict 是中立格式（不绑定任何模型厂商），W7 的存档文件 I/O 基于此。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .worldpack import WorldPack


@dataclass(frozen=True)
class ChoiceRecord:
    """玩家关键选择记录（append-only）。"""

    day: int
    node_id: str | None  # 所属主线节点；日常引导选择为 None
    choice_id: str  # 选项标识（关键选择的选项 id 或日常选项文本）
    text: str  # 玩家实际选择的内容


@dataclass(frozen=True)
class StatChangeRecord:
    """数值变更审计记录（append-only），供 W8 零偏差审计回放。"""

    day: int
    target: str  # "player" 或 NPC id
    stat: str  # 属性名；对 NPC 恒为 "affection"
    delta: float
    reason: str
    before: float
    after: float


@dataclass(frozen=True)
class MemoryEntry:
    """一条显式记忆（M2a）：事实 + 植入时间（来源追踪 + 时间衰减淘汰依据）。"""

    fact: str
    day: int
    round: int  # 植入时的叙事回合序号


@dataclass
class GameState:
    pack_name: str
    day: int = 1
    action_points_left: int = 0  # 当日剩余行动点（from_pack 按世界包初始化）
    stats: dict[str, float] = field(default_factory=dict)
    affections: dict[str, float] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    scene: str = ""
    current_node: str | None = None
    present_npcs: list[str] = field(default_factory=list)  # 当前场景在场的 NPC id
    completed_nodes: list[str] = field(default_factory=list)  # 已完成的主线节点
    node_turns: int = 0  # 当前节点内已进行的叙事回合数（卡壳保护计数）
    stuck_stage: int = 0  # 卡壳保护阶段：0=无 / 1=已注入推进提示 / 2=已注入命运事件
    pending_choice: str | None = None  # 等待玩家选择的关键选择 id（非 None 时锁定输入）
    resolved_choices: list[str] = field(default_factory=list)  # 当前节点内已选择的关键选择 id
    choice_log: list[ChoiceRecord] = field(default_factory=list)
    stat_log: list[StatChangeRecord] = field(default_factory=list)
    triggered_events: list[str] = field(default_factory=list)
    turn_count: int = 0  # 叙事回合总数（记忆来源追踪）
    player_facts: list[MemoryEntry] = field(default_factory=list)  # 玩家长期关键事实（状态栏常驻）
    npc_memories: dict[str, list[MemoryEntry]] = field(default_factory=dict)  # NPC 对玩家的记忆

    # ---- 构造 ----

    @classmethod
    def from_pack(cls, pack: "WorldPack") -> "GameState":
        """从世界包 schedule 定义初始化（所有初始值来自世界包，引擎不含内容）。"""
        s = pack.schedule
        return cls(
            pack_name=pack.world.name,
            action_points_left=pack.schedule.day_action_points,
            stats={k: float(v.initial) for k, v in s.stats.items()},
            affections={k: float(v.initial) for k, v in s.affections.items()},
            flags=dict(s.flags),
            scene=pack.world.start_scene,
        )

    # ---- 快照（W7 回滚用） ----

    def copy(self) -> "GameState":
        return copy.deepcopy(self)

    # ---- 中立格式序列化 ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "pack_name": self.pack_name,
            "day": self.day,
            "action_points_left": self.action_points_left,
            "stats": dict(self.stats),
            "affections": dict(self.affections),
            "flags": dict(self.flags),
            "scene": self.scene,
            "current_node": self.current_node,
            "present_npcs": list(self.present_npcs),
            "completed_nodes": list(self.completed_nodes),
            "node_turns": self.node_turns,
            "stuck_stage": self.stuck_stage,
            "pending_choice": self.pending_choice,
            "resolved_choices": list(self.resolved_choices),
            "choice_log": [vars(c) for c in self.choice_log],
            "stat_log": [vars(r) for r in self.stat_log],
            "triggered_events": list(self.triggered_events),
            "turn_count": self.turn_count,
            "player_facts": [vars(m) for m in self.player_facts],
            "npc_memories": {
                k: [vars(m) for m in v] for k, v in self.npc_memories.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GameState":
        version = d.get("version", 1)
        if version != 1:
            raise ValueError(f"不支持的存档版本: {version}")
        return cls(
            pack_name=d["pack_name"],
            day=d["day"],
            action_points_left=d.get("action_points_left", 0),
            stats=dict(d["stats"]),
            affections=dict(d["affections"]),
            flags=dict(d["flags"]),
            scene=d.get("scene", ""),
            current_node=d.get("current_node"),
            present_npcs=list(d.get("present_npcs", [])),
            completed_nodes=list(d.get("completed_nodes", [])),
            node_turns=d.get("node_turns", 0),
            stuck_stage=d.get("stuck_stage", 0),
            pending_choice=d.get("pending_choice"),
            resolved_choices=list(d.get("resolved_choices", [])),
            choice_log=[ChoiceRecord(**c) for c in d.get("choice_log", [])],
            stat_log=[StatChangeRecord(**r) for r in d.get("stat_log", [])],
            triggered_events=list(d.get("triggered_events", [])),
            turn_count=d.get("turn_count", 0),
            player_facts=[MemoryEntry(**m) for m in d.get("player_facts", [])],
            npc_memories={
                k: [MemoryEntry(**m) for m in v]
                for k, v in d.get("npc_memories", {}).items()
            },
        )

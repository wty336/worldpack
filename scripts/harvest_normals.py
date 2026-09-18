"""真实轨迹导出 normal：采集（真材料）→ 补写干净叙事 → 判官校验 → 入库。

**为什么这样造**（2026-09-12 实测定的路线②）：评测集 normal 是判官误报率的分母，必须带
**真实形态的材料**（长材料 + 累积 facts/记忆/属性），而 flash 自己跑出来的原始叙事干净率只有
~12%（大量重复/时间线冲突，判官判违规）。所以：**材料取真轨迹、叙事补写成与材料一致**，
再过判官校验——保住最难造假的那一半（材料），叙事侧用两道门（补写约束 + 判官）保证干净。

三步（前两步付费、第三步免费离线）：

    # ① 采集真轨迹（每回合 1 次 turn 调用 + 生产同款侧信道）
    python scripts/harvest_normals.py --pack world-packs/P1_school_letters --turns 40 --days 14
    # ② 补写 + 校验 + 入库（每候选：1 次补写 + 1 次判官；侧信道已关思考）
    python scripts/harvest_normals.py --rewrite reports/normals-P1_school_letters-*.jsonl
    # ③ 只看不写（复核用）
    python scripts/harvest_normals.py --validate reports/normals-*.jsonl

产出：`world-packs/<pack>/judge_corpus.real.yaml`（**追加**式，id 连续编号），带 provenance：
源 jsonl / 采集模型 / 端点 / 补写模型 / 判官通过率。

纪律：
- 补写提示词硬约束"不得引入材料中没有的事实/承诺/数字/角色"；
- 判官判"通过"才入库（判官侧信道已关思考：`DEEPSEEK_DISABLE_THINKING=1`，否则长材料熔断）；
- 状态字段（day/scene/present/好感/facts/npc_memories/**stats**）全部落盘，材料原样重建；
- `--offline` 用桩 LLM 跑通管线（零成本）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
from datetime import datetime
from types import SimpleNamespace

import yaml

from game_agent.config import load_settings
from game_agent.context import ContextBuilder
from game_agent.endpoint import fingerprint_for
from game_agent.game import Game
from game_agent.judge import JudgeSystem
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FILLER = [
    "四处看看，留意周围有没有值得注意的东西。",
    "跟在场的人打个招呼，问问最近有什么事情。",
    "把手头的事整理一下，想想接下来该做什么。",
    "沿着街往前走一段，看看天色和路上的人。",
]

REWRITE_SYSTEM = """你是中文文字冒险游戏的叙事者。给一份「当前状态材料」，写一段与材料**完全一致**的叙事。

硬约束：
- 只描写材料里**已经存在**的人、物、地点与状态；
- **不得引入材料中没有的事实、承诺、约定、数字、角色**（这是最容易被判违规的地方）；
- 不要把材料里的事实或状态栏照抄一遍，只写现场一小段具体动静；
- 100~200 字，第二人称「你」，平实叙述，不替玩家做决定，不用问句结尾。

只输出叙事正文，不要标题、解释或任何前后缀。"""


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


class _OfflineFake:
    """--offline：内嵌假客户端（只验管线，不测能力）。形状照 longrun_probe 的桩。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        args = json.dumps(
            {"narration": "你环顾四周，风从檐角掠过，什么也没发生。",
             "choices": ["继续", "离开", "询问"], "plot_signal": "normal"}
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    reasoning_content=None,
                    tool_calls=[SimpleNamespace(
                        id="off1",
                        function=SimpleNamespace(name="submit_narration", arguments=args),
                    )],
                ),
            )],
            usage=None,
        )


def _dump_case(state: GameState, pack, narration: str) -> dict:
    """把当轮状态 + 叙事落成一条候选（字段与 JudgeCase 对齐，材料可原样重建）。"""
    return {
        "turn": state.turn_count,
        "day": state.day,
        "scene": state.scene,
        "present": list(state.present_npcs),
        "affections": {k: round(float(v), 3) for k, v in state.affections.items()},
        "facts": [m.fact for m in state.player_facts],
        "npc_memories": {k: [m.fact for m in v] for k, v in state.npc_memories.items() if v},
        "stats": {k: round(float(v), 3) for k, v in state.stats.items()},
        "material": ContextBuilder.from_pack(pack).status_text(state, None),
        "narration": narration,
    }


def collect(pack_path: str, turns: int, seed: int, days: int, offline: bool) -> tuple[list[dict], object]:
    """跑真实循环采集候选；返回 (候选列表, settings)。"""
    pack = load_worldpack(pack_path)
    state = GameState.from_pack(pack)
    settings = load_settings()

    if offline:
        tracker = None
        llm = LLMClient(_OfflineFake(), "offline-stub", build_tools(pack.schedule), models={})
    else:
        tracker = UsageTracker("reports/usage-normals.jsonl")
        llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)

    game = Game(
        pack, state, llm, rng=random.Random(seed),
        extract_every=2, compress_threshold=10**9, judge_every=5, reflect_every=10,
    )
    cases: list[dict] = []
    view = game.start()
    dialogue_left = 0
    guard = 0
    while state.day <= days and view.ending is None and len(cases) < turns and guard < turns * 6:
        guard += 1
        if view.choice_prompt is not None:
            view = game.pick(0)
        elif state.action_points_left > 0:
            avail = [a.id for a in game.actions_available()]
            view = game.act(avail[0] if avail else next(iter(pack.schedule.actions)))
            dialogue_left = 1
        elif dialogue_left > 0:
            view = game.say(FILLER[len(cases) % len(FILLER)])
            dialogue_left -= 1
        else:
            game.end_day()
            continue
        if view.narration:
            cases.append(_dump_case(state, pack, view.narration))
    return cases, settings


# ---------------------------------------------------------------------------
# 补写 / 校验 / 入库
# ---------------------------------------------------------------------------


def _load_rows(jsonl_path: str) -> list[dict]:
    path = pathlib.Path(jsonl_path)
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _pack_dir_of(jsonl_path: pathlib.Path) -> pathlib.Path | None:
    stem = jsonl_path.stem  # normals-<pack>-<ts>
    parts = stem.split("-")
    if len(parts) < 3 or parts[0] != "normals":
        return None
    pack_dir = REPO_ROOT / "world-packs" / parts[1]
    return pack_dir if pack_dir.exists() else None


def _next_index(pack_dir: pathlib.Path) -> int:
    """已有 real 用例的最大编号 + 1（追加式，不覆盖旧条目）。"""
    path = pack_dir / "judge_corpus.real.yaml"
    if not path.exists():
        return 1
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    idx = 0
    for case in data.get("cases", []):
        cid = str(case.get("id", ""))
        if cid.startswith("real_normal_"):
            try:
                idx = max(idx, int(cid.rsplit("_", 1)[1]))
            except ValueError:
                pass
    return idx + 1


def _write_real(pack_dir: pathlib.Path, rows: list[dict], provenance: dict, source_name: str) -> pathlib.Path:
    """追加写入 judge_corpus.real.yaml（保留旧条目与旧 provenance 历史）。"""
    path = pack_dir / "judge_corpus.real.yaml"
    old: dict = {}
    if path.exists():
        old = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    start = _next_index(pack_dir)
    new_cases = [
        {
            "id": f"real_normal_{start + i:02d}",
            "category": "normal",
            "narration": r["narration"],
            "expected": True,
            "day": r["day"],
            "scene": r["scene"],
            "present": r["present"],
            "affections": r["affections"],
            "facts": r["facts"],
            "npc_memories": r["npc_memories"],
            "stats": r["stats"],
            "note": f"真轨迹材料（第 {r['turn']} 回合）+ 补写叙事并判官校验通过（{source_name}）",
        }
        for i, r in enumerate(rows)
    ]
    history = list(old.get("provenance_history", []))
    history.append(provenance)
    out = {
        "version": 1,
        "content_version": f"real-{datetime.now().strftime('%Y%m%d')}",
        "generated_by": "scripts/harvest_normals.py",
        "note": "真实轨迹 normal：材料取真轨迹（长材料 + 累积 facts/记忆/属性），"
                "叙事按路线②补写并过判官校验；状态字段齐全，材料由 build_materials 原样重建",
        "provenance_history": history,
        "cases": list(old.get("cases", [])) + new_cases,
    }
    path.write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")
    return path


def rewrite_and_validate(jsonl_path: str, offline: bool = False, temperature: float = 0.8) -> int:
    """路线②：把候选的叙事**补写**成与材料一致的干净版本，判官校验通过才入库。"""
    path = pathlib.Path(jsonl_path)
    pack_dir = _pack_dir_of(path)
    if pack_dir is None:
        print(f"[✗] 从文件名推断不出包（{path.name}）")
        return 1
    rows = _load_rows(jsonl_path)
    settings = load_settings()
    tracker = UsageTracker("reports/usage-normals-rewrite.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    judge = JudgeSystem(llm)

    kept: list[dict] = []
    for i, row in enumerate(rows, 1):
        messages = [
            {"role": "system", "content": REWRITE_SYSTEM},
            {"role": "user", "content": f"<材料>\n{row['material']}\n</材料>\n\n写这一段叙事："},
        ]
        text = llm.complete(messages, max_tokens=400, temperature=temperature, purpose="normal_rewrite").strip()
        if not text:
            print(f"  [丢] 候选 {i}：补写为空")
            continue
        ok, verdict = judge.check(text, row["material"])
        if ok is not True:
            print(f"  [丢] 候选 {i}：判官未判通过（{str(verdict)[:36]}）")
            continue
        kept.append({**row, "narration": text})
        print(f"  [留] 候选 {i}（第 {row['turn']} 回合）{text[:34]}…")

    if not kept:
        print("[✗] 没有一条通过，不落盘")
        return 1
    out_path = _write_real(
        pack_dir, kept,
        {"source_jsonl": path.name, "mode": "rewrite+judge",
         "model": settings.model, "endpoint": fingerprint_for(settings, "judge"),
         "thinking_disabled": settings.no_thinking_side_channel,
         "validated_at": datetime.now().isoformat(timespec="seconds"),
         "kept": len(kept), "candidates": len(rows)},
        path.name,
    )
    print(f"\n[✓] 追加 {len(kept)} 条 → {out_path.relative_to(REPO_ROOT)}"
          f"（候选 {len(rows)}，良率 {len(kept) / len(rows):.0%}）")
    print(tracker.cost_report())
    return 0


def validate(jsonl_path: str) -> int:
    """只复核：不补写，直接判候选自带叙事（供对比原始轨迹良率）。"""
    path = pathlib.Path(jsonl_path)
    pack_dir = _pack_dir_of(path)
    if pack_dir is None:
        print(f"[✗] 从文件名推断不出包（{path.name}）")
        return 1
    settings = load_settings()
    tracker = UsageTracker("reports/usage-normals-validate.jsonl")
    judge = JudgeSystem(LLMClient.from_settings(settings, [], tracker=tracker))
    rows = _load_rows(jsonl_path)
    kept = 0
    for i, row in enumerate(rows, 1):
        ok, _ = judge.check(row["narration"], row["material"])
        kept += bool(ok is True)
        if ok is not True:
            print(f"  [丢] 候选 {i}：未判通过")
    print(f"\n原始轨迹良率：{kept}/{len(rows)} = {kept / len(rows):.0%}（不写文件）")
    print(tracker.cost_report())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实轨迹 normal：采集 / 补写 / 校验")
    parser.add_argument("--pack", default="world-packs/P1_school_letters")
    parser.add_argument("--turns", type=int, default=40, help="目标候选数（= 回合数）")
    parser.add_argument("--days", type=int, default=14, help="游戏内天数预算")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--offline", action="store_true", help="桩 LLM（零成本，验管线）")
    parser.add_argument("--out", default=None, help="候选 jsonl 路径（缺省自动命名）")
    parser.add_argument("--rewrite", default=None, help="路线②：补写叙事 + 判官校验 → real.yaml")
    parser.add_argument("--validate", default=None, help="只复核原始轨迹良率（不写文件）")
    parser.add_argument("--temperature", type=float, default=0.8, help="补写温度（默认 0.8 求多样）")
    args = parser.parse_args(argv)

    if args.rewrite:
        return rewrite_and_validate(args.rewrite, offline=args.offline, temperature=args.temperature)
    if args.validate:
        return validate(args.validate)

    cases, settings = collect(args.pack, args.turns, args.seed, args.days, args.offline)
    pack_id = pathlib.Path(args.pack).name
    out = pathlib.Path(args.out or f"reports/normals-{pack_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in cases:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n[✓] 采集 {len(cases)} 条候选 → {out}")
    print(f"    model={settings.model} · {'离线桩' if args.offline else '真实调用'}")
    if not args.offline:
        print("    下一步：python scripts/harvest_normals.py --rewrite " + str(out))
    return 0 if cases else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

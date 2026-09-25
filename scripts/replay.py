"""runtime 平台化 ②：回放一局的第 N 回合（可换 prompt / 模型）并 diff。

用法::

    python scripts/replay.py --run runs/<run_id> --turn 5
    python scripts/replay.py --run runs/<run_id> --turn 5 --prompt-patch patch.yaml
    python scripts/replay.py --run runs/<run_id> --turn 5 --model deepseek-v4-pro
    python scripts/replay.py --run runs/<run_id> --resume 3   # 从 checkpoint 3 继续交互

diff 维度：narration（相似度 + 全文）/ choices / stat_changes 数 / 结局标题 /
重放成本（tokens/时延走本次 usage）。prompt 补丁 = 对 ENGINE_RULES 的文本替换
（yaml：``replace: [{old, new}]``，每条 old 必须恰好出现一次）。
"""

from __future__ import annotations

import argparse
import difflib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.runlog import RunRecorder, apply_prompt_patch, rebuild_game
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

REPO_ROOT = Path(__file__).resolve().parent.parent


def _apply_action(game: Game, entry: dict):
    """按 runlog 记录的动作重放。返回 (view, 动作摘要)；end_day 无 view。"""
    action = entry.get("action") or {"kind": "start"}
    kind, payload = action.get("kind"), action.get("payload")
    if kind == "start":
        return game.start(), "开局"
    if kind == "say":
        return game.say(payload), f"自由输入：{payload[:30]}"
    if kind == "act":
        return game.act(payload), f"日程行动：{payload}"
    if kind == "pick":
        return game.pick(payload), f"关键选择：{payload}"
    if kind == "end_day":
        # end_day 已叙事化（时序过渡回合）：返回 view 参与对比；
        # 旧 runlog 的 end_day 未记录 view（rec_nar 为空），diff 会如实显示口径变化
        return game.end_day(), "结束今天"
    raise ValueError(f"未知动作类型: {kind}")


def _diff(recorded: dict | None, view, replay_cost: dict) -> dict:
    rec_nar = (recorded or {}).get("narration") or ""
    new_nar = (view.narration or "") if view is not None else ""
    return {
        "narration_ratio": round(difflib.SequenceMatcher(None, rec_nar, new_nar).ratio(), 3),
        "narration_recorded": rec_nar,
        "narration_replayed": new_nar,
        "choices_recorded": recorded.get("choices_count") if recorded else None,
        "choices_replayed": len(view.choices) if view is not None else None,
        "stat_changes_recorded": (recorded or {}).get("stat_changes"),
        "replay_tokens": replay_cost,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay", description="回放一局的第 N 回合并 diff")
    parser.add_argument("--run", required=True, help="runlog 目录（runs/<run_id>）")
    parser.add_argument("--turn", type=int, default=None, help="回放的回合号（与 --resume 二选一）")
    parser.add_argument("--resume", type=int, default=None, help="从 checkpoint 继续交互")
    parser.add_argument("--prompt-patch", default=None, help="ENGINE_RULES 补丁 yaml")
    parser.add_argument("--model", default=None, help="换模型重放（覆盖 DEEPSEEK_MODEL）")
    args = parser.parse_args(argv)

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1
    if args.model:
        settings = replace(settings, model=args.model)

    rec = RunRecorder(args.run)
    entries = rec.entries()
    if not entries:
        print(f"[✗] runlog 为空: {args.run}")
        return 1

    if args.resume is not None:
        cp = rec.load_checkpoint(args.resume)
        pack = _pack_from_state(cp)
        llm = LLMClient.from_settings(settings, build_tools(pack.schedule))
        game = rebuild_game(pack, cp["state"], cp["history"], llm)
        from game_agent.cli import _repl

        print(f"已从 checkpoint {args.resume} 恢复，进入交互（Ctrl+C 退出）")
        return _repl(game)

    if args.turn is None:
        print("[✗] 需要 --turn 或 --resume")
        return 1
    entry = next((e for e in entries if e["turn"] == args.turn), None)
    if entry is None:
        print(f"[✗] 没有第 {args.turn} 回合（runlog 共 {len(entries)} 条）")
        return 1
    cp = rec.load_checkpoint(args.turn - 1)

    # 包名从 checkpoint 的 state 取（meta 只是展示用）
    pack = _pack_from_state(cp)
    tracker = UsageTracker(rec.run_dir / f"usage-replay-{datetime.now().strftime('%H%M%S')}.jsonl")
    llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)

    # prompt 补丁：改 ENGINE_RULES 全局文本（ContextBuilder 在构造时读取 → 补丁生效）
    if args.prompt_patch:
        import game_agent.context as ctx

        original = ctx.ENGINE_RULES
        ctx.ENGINE_RULES = apply_prompt_patch(args.prompt_patch, original)
        print(f"[prompt-patch] 已应用 {args.prompt_patch}（规则文本 {len(original)} → {len(ctx.ENGINE_RULES)} 字）")

    game = rebuild_game(pack, cp["state"], cp["history"], llm)
    view, label = _apply_action(game, entry)
    replay_tokens = None
    if tracker.entries:
        last = tracker.entries[-1]
        replay_tokens = {k: last.get(k, 0) for k in ("prompt_tokens", "completion_tokens")}
    diff = _diff(entry.get("outcome"), view, replay_tokens)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": rec.run_id,
        "turn": args.turn,
        "action": label,
        "model": settings.model,
        "prompt_patch": args.prompt_patch,
        "diff": diff,
    }
    out = REPO_ROOT / "reports" / f"replay_{rec.run_id}_t{args.turn:03d}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== replay 报告 · {rec.run_id} · 第 {args.turn} 回合（{label}）· 模型 {settings.model} ===")
    print(f"叙事相似度 {diff['narration_ratio']:.0%}（1.0 = 完全复现）")
    if diff["stat_changes_recorded"] is not None:
        print(f"数值变更（原 {diff['stat_changes_recorded']} 条）· 重放 tokens {diff['replay_tokens']}")
    print(f"【原叙事】{diff['narration_recorded']}")
    print(f"【重放叙事】{diff['narration_replayed']}")
    print(f"\n报告已写入 {out}")
    return 0


def _pack_from_state(cp: dict):
    from game_agent.worldpack import load_worldpack

    pack_name = cp["state"]["pack_name"]
    for candidate in (REPO_ROOT / "world-packs").iterdir():
        if candidate.is_dir() and load_worldpack(candidate).world.name == pack_name:
            return load_worldpack(candidate)
    raise FileNotFoundError(f"找不到世界包: {pack_name}")


if __name__ == "__main__":
    raise SystemExit(main())

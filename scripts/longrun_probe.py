"""④ 缩减版长局验收（100 回合）——计划文档 docs/plan-worldpack-qa.md §6。

用法：
  uv run python scripts/longrun_probe.py --pack world-packs/xianxia_wendao [--turns 100] [--seed 42]
  uv run python scripts/longrun_probe.py --pack world-packs/xianxia_wendao --offline --turns 30

测量（长局机制换包后是否腐化）：
  - 压缩：history 出现剧情摘要（llm.complete purpose=compress 调用 ≥1 次）；
  - 记忆：5 条事实探针在回合 5/10/15/20/25 植入，回合 50/100 独立调用召回
    （bigram ≥60% 容忍改写，方法学同 memory_regression）；
  - 协议：全程零熔断；数值：stat_log 审计零偏差；
  - 附带记录：Judge 调用数/校验反馈数、好感漂移（防结局提前触发）。

无结局路线（测量纪律：可复现优先）：
  拜阵峰（无好感）→ 弃碑救人（无 relic_taken → 封死「问道长生」）→ 死守山门；
  每日打坐保持修为 ≥30（封死第 12 天的「尘缘未了」）；台词全部中性（好感不涨 → 封死
  「剑心同尘」需好感 50 与「丹心相许」需白芷 50）。
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from types import SimpleNamespace

from game_agent.audit import audit_stats
from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.save import save_game
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

SAVE_DIR = Path("saves")

RECALL_SYSTEM = (
    "你是记忆检索器。根据给定材料回答问题：只给简短直接的答案（不超过一句话），"
    "不展开、不叙事、不编造；材料里没有就回答「不知道」。"
)

# 事实探针：(id, 关键词, 植入回合, 植入台词, 定向提问)
IMPLANTS = [
    ("sword_name", ["听雨"], 5, "（抚剑）我这柄剑名唤『听雨』。", "玩家的剑叫什么名字？"),
    ("hometown", ["江南", "渔村"], 10, "（追忆）我自幼在江南一个渔村长大的。", "玩家来自哪里？"),
    ("promise", ["老村长", "教他剑法"], 15, "（认真）我答应过老村长的孙儿，学成回去教他剑法。", "玩家答应过谁、什么承诺？"),
    ("fear_snake", ["蛇"], 20, "（不自在）说来惭愧，我从小就怕蛇。", "玩家害怕什么？"),
    ("like_chestnut", ["栗子"], 25, "（笑）我最爱吃糖炒栗子，山下一见就挪不动脚。", "玩家最爱吃什么？"),
]

# 中性填充台词（不推进好感，防「剑心同尘」提前触发）
FILLER = [
    "（抱拳）师姐，今日吐纳总觉气机滞涩，可是心法练岔了？",
    "（请教）这苍梧界可还有什么值得一去的秘境传闻？",
    "（望着远处）山下的坊市又热闹起来了。",
    "（认真）我想把剑法练得再稳些，可有诀窍？",
    "（随口）今日后山的雾，比昨日又重了几分。",
]

# 无结局路线的关键抉择索引
PICKS = {"choose_peak": 2, "relic_choice": 0, "battle_choice": 0}


class _OfflineFake:
    """--offline：内嵌假客户端（循环/审计离线回归；不测召回与压缩）。"""

    def __init__(self):
        self.chat = SimpleNamespace(completions=self)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        args = json.dumps(
            {"narration": "（离线假叙事）", "choices": ["继续", "离开", "询问"], "plot_signal": "normal"}
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="缩减版长局验收（计划文档④）")
    parser.add_argument("--pack", default="world-packs/xianxia_wendao")
    parser.add_argument("--turns", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--offline", action="store_true", help="离线循环回归（不测召回/压缩）")
    args = parser.parse_args(argv)

    pack = load_worldpack(args.pack)
    settings = load_settings()
    if not args.offline and not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    state = GameState.from_pack(pack)
    if args.offline:
        llm = LLMClient(_OfflineFake(), "offline-fake", build_tools(pack.schedule))
        tracker = None
    else:
        tracker = UsageTracker(SAVE_DIR / f"usage-longrun-{pack.root.name}.jsonl")
        llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)
    game = Game(
        pack, state, llm, rng=random.Random(args.seed),
        extract_every=2, compress_threshold=30000, judge_every=5, reflect_every=10,
    )

    # 侧信道计数（压缩触发数/Judge 数）——包装 complete 统计 purpose
    calls: dict[str, int] = {}
    orig_complete = llm.complete

    def counting_complete(messages, **kw):
        purpose = kw.get("purpose", "aux")
        calls[purpose] = calls.get(purpose, 0) + 1
        return orig_complete(messages, **kw)

    llm.complete = counting_complete  # type: ignore[method-assign]

    transcript: list[str] = []
    recall_report: dict[str, dict] = {}

    def log(*parts) -> None:
        text = "\n".join(str(p) for p in parts if p)
        transcript.append(text + "\n" + "-" * 60)
        print(text)
        print("-" * 60)

    def probe_recall(checkpoint: int) -> dict:
        """三层召回测量（修正 2026-09-08 第 1 轮的测量缺陷，复盘 §7.4）：

        1) 存储保持（代码层，确定性）：探针事实是否仍在 state.player_facts——
           记忆完整性，与上下文无关；
        2) 注入可见（检索策略）：探针事实是否进入本轮 rank_facts 注入集——
           不在注入集的，模型**没有机会**看到，不能计入门禁（A1 检索设计如此）；
        3) 可答出（模型层）：定向提问（材料 = 生产同款 build_messages，系统消息换
           检索器人格防"散文化输出截断"），逐条核对关键词。
        门禁只约束「已存储 + 已注入」的探针必须可答出；存储缺失才是真故障。
        """
        from game_agent.context import _recent_player_text
        from game_agent.memory import rank_facts

        facts_text = [m.fact for m in state.player_facts]
        context = " ".join(filter(None, [state.scene, _recent_player_text(game.history)]))
        injected_text = [m.fact for m in rank_facts(state.player_facts, context, state.turn_count)]

        msgs = game.builder.build_messages(state, game.history, None)
        msgs[0] = {"role": "system", "content": RECALL_SYSTEM}

        items: dict[str, dict] = {}
        for fid, kws, _t, _line, question in IMPLANTS:
            stored = any(kw in f for f in facts_text for kw in kws)
            injected = any(kw in f for f in injected_text for kw in kws)
            answered = False
            if injected:
                try:
                    answer = llm.complete(
                        [*msgs, {"role": "user", "content": f"[记忆检查] {question}"}],
                        max_tokens=200,
                        purpose="recall",
                    )
                except Exception:  # noqa: BLE001
                    answer = ""
                answered = any(kw in answer for kw in kws)
            items[fid] = {"stored": stored, "injected": injected, "answered": answered}

        stored_n = sum(1 for v in items.values() if v["stored"])
        injected_n = sum(1 for v in items.values() if v["injected"])
        answered_n = sum(1 for v in items.values() if v["answered"])
        log(
            f"[检查点] 回合 {checkpoint} · 存储 {stored_n}/5 · 注入 {injected_n}/5 · "
            f"注入可答 {answered_n}/{injected_n} · 明细 {items}"
        )
        return {
            "checkpoint": checkpoint,
            "items": items,
            "stored": stored_n,
            "injected": injected_n,
            "answered_of_injected": answered_n,
        }

    try:
        print(f"pack={pack.world.name} · seed={args.seed} · 回合预算 {args.turns} · "
              f"{'离线模式' if args.offline else f'model={settings.model}'}\n")

        view = game.start()
        said = False
        implant_idx = 0
        line_idx = 0
        ending_fired = None
        while state.turn_count < args.turns:
            if view.choice_prompt is not None:
                pick = PICKS[view.choice_prompt.id]
                view = game.pick(pick)
                continue
            if view.ending is not None:
                ending_fired = view.ending.title
                log(f"[!] 意外提前结局：{ending_fired}（无结局路线被突破）")
                break

            if state.action_points_left > 0:
                view = game.act("meditate")  # 保持修为 ≥30（封死第 12 天「尘缘未了」）
                continue

            # 对话：植入回合播探针台词，其余播中性填充
            if not said:
                said = True
                if implant_idx < len(IMPLANTS) and state.turn_count >= IMPLANTS[implant_idx][2]:
                    fid, _kws, _t, line, _q = IMPLANTS[implant_idx]
                    implant_idx += 1
                    view = game.say(line)
                    log(f"[植入 {fid}] 回合 {state.turn_count} · 第 {state.day} 天 · {line}")
                else:
                    line = FILLER[line_idx % len(FILLER)]
                    line_idx += 1
                    view = game.say(line)
                continue

            # 检查点召回（在日终前，避免同日重复触发）
            if state.turn_count >= 50 and 50 not in recall_report:
                recall_report[50] = probe_recall(50)
            if state.turn_count >= 100 and 100 not in recall_report:
                recall_report[100] = probe_recall(100)

            said = False
            game.end_day()
            su = state.affections.get("su_wanying", 0.0)
            if su >= 40:
                log(f"[警告] 苏晚晴好感已达 {su:g}——接近「剑心同尘」阈值 50，请关注漂移")
            log(
                f"—— 第 {state.day} 天 · 回合 {state.turn_count} ——",
                f"[状态] 修为 {state.stats['xiu_wei']:g} · 好感 {state.affections}",
            )

        # 末轮检查点（循环可能正好停在 <100）
        if not args.offline and 100 not in recall_report and state.turn_count >= 100:
            recall_report[100] = probe_recall(100)

        # 验收断言
        status = 0
        if ending_fired:
            log(f"[✗] 无结局路线被突破：{ending_fired}")
            status = 1
        if not args.offline:
            if calls.get("compress", 0) >= 1:
                log(f"[✓] 压缩触发 {calls.get('compress')} 次（purpose=compress 调用数）")
            else:
                log("[✗] 压缩未触发（100 回合内无剧情摘要）")
                status = 1
            last = recall_report.get(100)
            if last is not None:
                stored, injected, answered = last["stored"], last["injected"], last["answered_of_injected"]
                if stored == 5 and answered >= injected:
                    log(f"[✓] 记忆探针达标：存储 {stored}/5 · 注入 {injected}/5 · 注入可答 {answered}/{injected}")
                else:
                    log(f"[✗] 记忆探针未达标：{last}")
                    status = 1
            if calls.get("judge", 0) >= 1:
                log(f"[观察] Judge 调用 {calls.get('judge')} 次 · 提取 {calls.get('extract', 0)} 次 · "
                    f"反思 {calls.get('reflect', 0)} 次 · 去重 {calls.get('dedup', 0)} 次")
        deviations = audit_stats(pack, state)
        if deviations:
            log("[✗] 数值零偏差审计失败：", *deviations)
            status = 1
        else:
            log(f"[✓] 数值零偏差审计通过（{len(state.stat_log)} 条变更记录）")

        # 落盘
        SAVE_DIR.mkdir(exist_ok=True)
        save_game(state, SAVE_DIR / f"longrun-{pack.root.name}.json", game.history)
        (SAVE_DIR / f"longrun-{pack.root.name}.txt").write_text("\n\n".join(transcript), encoding="utf-8")
        report = {
            "pack": pack.world.name,
            "seed": args.seed,
            "turns": state.turn_count,
            "day": state.day,
            "offline": args.offline,
            "ending_fired": ending_fired,
            "side_calls": calls,
            "affections": state.affections,
            "stats": state.stats,
            "recall": recall_report,
            "audit_deviations": audit_stats(pack, state),
            "exit": status,
        }
        (SAVE_DIR / f"longrun-{pack.root.name}-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已保存 → saves/longrun-{pack.root.name}.{{json,txt,-report.json}}")
        if tracker is not None:
            print("\n" + tracker.cost_report())
        return status
    except LLMTurnError as e:
        print(f"[✗] 协议熔断: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[✗] 异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

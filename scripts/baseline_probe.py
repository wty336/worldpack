"""W-D 长会话基线：记忆保持腐化曲线（M2 压缩的边界集，plan-m1-5.md W-D）。

用法：uv run python scripts/baseline_probe.py [--seed 42] [--max-turns 120]

流程：
- 用探针世界包（无主线/无结局）跑 N 个叙事回合；
- 在回合 40/80/120 三个检查点，用独立调用逐条提问已植入的事实；
- 答案按关键词判定对错，输出腐化曲线（JSON + Markdown 报告）。

注：提问走独立消息列表（不进剧情历史），避免污染长会话；提问调用不带 tools，
被 API 拒绝时回退到带 tools + 纯文字指令。
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools, make_client
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

SAVE_DIR = Path("saves")

# 事实集：(id, 类别, 植入回合, 植入台词, 提问, 期望关键词)
FACTS = [
    ("f1_sword", "早期", 2,
     "（闲聊）我这柄剑，名唤『听雨』，是师父传下来的，跟了我三年。",
     "玩家曾说过他的剑叫什么名字？只回答剑名，不知道就说『不知道』。", ["听雨"]),
    ("f2_code", "早期", 4,
     "（约定）若姑娘有急事寻我，便在沈府后墙贴一枚红纸，上头写『七月』二字，我自会知晓。",
     "玩家和沈清秋约定的暗号是什么？只回答暗号内容，不知道就说『不知道』。", ["七月"]),
    ("f3_hometown", "早期", 6,
     "（谈及身世）我自江南而来，家乡多水，出门便坐船，倒也有趣。",
     "玩家是从哪里来的？只回答地名，不知道就说『不知道』。", ["江南"]),
    ("f4_master", "早期", 8,
     "（回忆）我师父姓陆，早年在镖局里走镖，脾气倔得很。",
     "玩家的师父姓什么？只回答姓氏，不知道就说『不知道』。", ["陆"]),
    ("f5_firewood", "中期", 42,
     "（随口提起）后山砍柴的老樵夫腿脚不便，我应了他，这几日替他送柴。",
     "玩家答应了帮谁送柴？只回答人物，不知道就说『不知道』。", ["樵夫"]),
    ("f6_dog", "中期", 46,
     "（闲话）我在城郊捡了条黄狗，瘦得可怜，我唤它『阿黄』，养在后院。",
     "玩家养的狗叫什么名字？只回答名字，不知道就说『不知道』。", ["阿黄"]),
    ("f7_midautumn", "中期", 50,
     "（邀约）待中秋月圆，我们同去曲江放河灯，可好？",
     "玩家和沈清秋约定了什么？只回答约定内容，不知道就说『不知道』。", ["中秋", "曲江", "河灯"]),
    ("f8_backdoor", "中期", 54,
     "（低语）那夜我们从沈府后门出来，那门是亥时虚掩着的，幸好无人撞见。",
     "那夜沈府后门是什么时辰虚掩的？只回答时辰，不知道就说『不知道』。", ["亥时"]),
    ("f9_cherry", "近期", 100,
     "（闲谈）上回在东市买的樱桃，姑娘说喜欢，我便记下了。",
     "沈清秋喜欢吃什么果子？只回答果子名，不知道就说『不知道』。", ["樱桃"]),
    ("f10_temple", "近期", 104,
     "（相约）后日大慈恩寺有香会，姑娘可愿同去上香？",
     "玩家约沈清秋去哪里？只回答地点，不知道就说『不知道』。", ["大慈恩寺"]),
]

CHECKPOINTS = [40, 80, 120]

GENERIC_LINES = [
    "（闲谈）今日天气不错，我晨起在院中练了趟剑。",
    "（闲聊）长安城的胡饼确实好吃，就是油重了些。",
    "（闲谈）昨夜读了几页书，字认得七七八八。",
    "（随口）城西的杏花开了，改日可去看看。",
    "（闲谈）东市的早市热闹，我买了两个包子当早饭。",
    "（闲聊）这几日风沙大了些，出门得遮着点脸。",
]


def _plant_line(turn: int) -> str | None:
    for fid, _cat, plant_turn, line, *_ in FACTS:
        if plant_turn == turn:
            return line
    return None


def _probe(game: Game, llm: LLMClient, model: str, turn: int) -> dict:
    """独立调用：逐条提问当前已植入的事实（不进剧情历史）。"""
    results: dict[str, dict] = {}
    due = [f for f in FACTS if f[2] <= turn]
    for fid, _cat, _pt, _line, question, keywords in due:
        messages = [
            game.builder.system_message,
            *game.history,
            {"role": "user", "content": f"[记忆检查] {question}"},
        ]
        answer: str | None = None
        for use_tools in (False, True):
            try:
                kwargs = dict(model=model, messages=messages, max_tokens=120, stream=False)
                if use_tools:
                    kwargs.update(
                        dict(
                            tools=llm.tools,
                            tool_choice="auto",
                            max_tokens=200,
                            messages=[
                                game.builder.system_message,
                                *game.history,
                                {
                                    "role": "user",
                                    "content": f"[记忆检查] {question}"
                                    "（请直接以文字回答，不要调用任何工具。）",
                                },
                            ],
                        )
                    )
                resp = llm._client.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                if getattr(msg, "tool_calls", None):
                    results[fid] = {
                        "correct": False,
                        "answer": f"[tool_calls: {[tc.function.name for tc in msg.tool_calls]}]",
                    }
                    break
                answer = msg.content or ""
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}"
        if answer is None:
            results[fid] = {"correct": False, "answer": f"[error: {last_err}]"}
        else:
            correct = any(k in answer for k in keywords)
            results[fid] = {"correct": correct, "answer": answer.strip()[:100]}
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="长会话记忆腐化基线")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-turns", type=int, default=120)
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    pack = load_worldpack("world-packs/baseline_probe")
    state = GameState.from_pack(pack)
    llm = LLMClient(make_client(settings), settings.model, build_tools(pack.schedule))
    game = Game(pack, state, llm, rng=random.Random(args.seed))
    print(f"model={settings.model} · seed={args.seed} · 目标 {args.max_turns} 回合 · 《{pack.world.name}》\n")

    report = {
        "meta": {
            "seed": args.seed,
            "model": settings.model,
            "max_turns": args.max_turns,
            "checkpoints": list(CHECKPOINTS),
            "facts": [{"id": f[0], "category": f[1], "plant_turn": f[2]} for f in FACTS],
        },
        "checkpoints": {},
        "protocol_retries": 0,
        "meltdowns": [],
        "stopped_at_turn": None,
    }

    turn = 0
    day = 1
    try:
        view = game.start()
        turn += 1
        while turn < args.max_turns:
            # 日程行动
            view = game.act("visit_shen")
            turn += 1
            if turn in CHECKPOINTS:
                print(f"[检查点] 回合 {turn} · 提问 {len([f for f in FACTS if f[2] <= turn])} 条事实……")
                report["checkpoints"][str(turn)] = _probe(game, llm, settings.model, turn)
                report["stopped_at_turn"] = turn
            # 两轮对话
            for _ in range(2):
                if turn >= args.max_turns:
                    break
                turn += 1
                line = _plant_line(turn) or GENERIC_LINES[(turn + day) % len(GENERIC_LINES)]
                view = game.say(line)
                if turn in CHECKPOINTS:
                    print(f"[检查点] 回合 {turn} · 提问 {len([f for f in FACTS if f[2] <= turn])} 条事实……")
                    report["checkpoints"][str(turn)] = _probe(game, llm, settings.model, turn)
                    report["stopped_at_turn"] = turn
            game.end_day()
            day += 1
            if turn % 10 == 0:
                print(f"[进度] 回合 {turn}/{args.max_turns} · 好感 {state.affections['shen_qingqiu']:.0f}")
    except LLMTurnError as e:
        report["meltdowns"].append({"turn": turn, "error": str(e)})
        print(f"[✗] 协议熔断于回合 {turn}: {e}")
    except Exception as e:  # noqa: BLE001
        report["meltdowns"].append({"turn": turn, "error": f"{type(e).__name__}: {e}"})
        print(f"[✗] 异常于回合 {turn}: {type(e).__name__}: {e}")

    # 汇总
    summary = {}
    for cp, results in report["checkpoints"].items():
        by_cat: dict[str, list] = {}
        for fid, _cat, _pt, _line, _q, _kw in FACTS:
            if fid in results:
                by_cat.setdefault(_cat, []).append(results[fid]["correct"])
        summary[cp] = {
            cat: {"correct": sum(v), "total": len(v)}
            for cat, v in by_cat.items()
        }
    report["summary"] = summary

    # 落盘
    SAVE_DIR.mkdir(exist_ok=True)
    out_json = SAVE_DIR / "baseline-report.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md = SAVE_DIR / "baseline-report.md"
    lines = [
        "# 长会话记忆腐化基线报告",
        "",
        f"- 模型：{settings.model} · seed：{args.seed} · 实际回合：{report['stopped_at_turn'] or turn}",
        f"- 协议重试：{report['protocol_retries']} · 熔断：{len(report['meltdowns'])}",
        "",
        "| 事实 | 类别 | 植入回合 | " + " | ".join(f"回合{cp}" for cp in report["checkpoints"]) + " |",
        "| --- | --- | --- | " + " | ".join("---" for _ in report["checkpoints"]) + " |",
    ]
    for f in FACTS:
        fid, cat, pt, _l, _q, _kw = f
        row = [fid, cat, str(pt)]
        for cp in report["checkpoints"]:
            r = report["checkpoints"][cp].get(fid)
            row.append("✓" if r and r["correct"] else ("—" if r is None else "✗"))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## 腐化曲线摘要")
    for cp, cats in summary.items():
        parts = []
        for cat, v in cats.items():
            parts.append(f"{cat} {v['correct']}/{v['total']}")
        lines.append(f"- 回合 {cp}：{' · '.join(parts)}")
    lines.append("")
    lines.append("## 判定详情")
    for cp in report["checkpoints"]:
        lines.append(f"\n### 回合 {cp}")
        for fid, r in report["checkpoints"][cp].items():
            mark = "✓" if r["correct"] else "✗"
            lines.append(f"- {mark} {fid}：{r['answer']}")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[✓] 基线完成 → {out_json} / {out_md}")
    return 0 if not report["meltdowns"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""③ 统一世界包冒烟（需要 DEEPSEEK_API_KEY；--offline 可离线跑）——计划文档 docs/plan-worldpack-qa.md §5。

用法：
  uv run python scripts/worldpack_smoke.py --pack world-packs/urban_neon [--days N] [--seed S] [--offline]

质量门命令链（README §质量门）：
  check-worldpack <pack> → pytest → judge_sensitivity --pack <pack> → worldpack_smoke --pack <pack>

由三份旧冒烟脚本（autoplay.py / xianxia_smoke.py / urban_smoke.py）合并而来：
每包策略（关键抉择索引 / 日程行动 / 对话台词 / 禁表扫描词表 / 观察词表）收敛为
下方 PROFILES 配置；--offline 用内嵌假客户端跑全流程（协议/循环/审计离线回归）。
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from types import SimpleNamespace

import yaml

from game_agent.audit import audit_stats
from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.save import save_game
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

SAVE_DIR = Path("saves")


def _profile_from_file(pack) -> dict | None:
    """B（素材导入工具）生成的包内 profile：<包>/smoke_profile.yaml（worldpack_smoke 自动读取）。"""
    path = pack.root / "smoke_profile.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None

PROFILES: dict[str, dict] = {
    "ancient_jianghu": {
        "picks": {"how_to_help": 0, "poetry_choice": 0},
        "action": "cultivate",
        "days": 6,
        "lines": [
            "（诚恳）在下初来长安，人地两疏，想向姑娘打听些习武之人谋生的门道。",
            "（认真）那日东市解围，还要多谢姑娘出言相帮。",
            "（望着远处）长安的黄昏，倒比别处更沉静些。",
        ],
        "forbidden_scan": ["手机", "微信", "互联网", "AI", "机器人", "相对论", "DNA"],
        "observe_modern": [],
        "target": "日常冒烟（不追结局，避免第 10 天「江湖独行」）",
    },
    "xianxia_wendao": {
        "picks": {"choose_peak": 0, "relic_choice": 1, "battle_choice": 2},
        "action": "meditate",
        "days": 8,
        "lines": [
            "（郑重抱拳）师姐，弟子吐纳总觉气机滞涩，可否指点一二？",
            "（望着洗剑池）这池水寒彻骨，师姐每日在此练剑，当真不易。",
            "（低声）昨夜又梦见那柄剑了。剑印上的纹路，究竟是何来历？",
        ],
        "forbidden_scan": ["手机", "微信", "AI", "魔法", "枪炮", "机甲", "吸血鬼", "绝绝子", "家人们"],
        "observe_modern": [],
        "target": "问道长生（取残碑 + 连修 + 独闯敌阵）",
    },
    "urban_neon": {
        "picks": {"how_to_repay": 1, "zero_choice": 1, "tower_choice": 1},
        "action": "scavenge",
        "days": 8,
        "lines": [
            "（揉着太阳穴）昨晚的记忆又缺了一块。林医生，这台手术到底切掉了我多少东西？",
            "（望着窗外的霓虹）这城市的灯，比白天更像白天。",
            "（低声）阿零，你到底是什么人？——算了，先告诉我旧终端区怎么走。",
        ],
        "forbidden_scan": [
            "修仙", "魔法", "内力", "御剑", "丹田", "真气", "绝绝子", "家人们", "星巴克", "特斯拉",
        ],
        "observe_modern": ["终端", "义体", "霓虹", "AI", "数据", "信用点"],
        "archaic_scan": ["在下", "姑娘", "公子", "弟子", "宗门", "道友", "仙途", "前辈", "少侠"],
        "target": "自由落体（卖坐标 + 谈判 + 连拾）",
    },
}


class _OfflineFake:
    """--offline：内嵌假客户端，每轮返回一个合法 submit_narration（协议/循环/审计离线回归）。"""

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
    parser = argparse.ArgumentParser(description="统一世界包冒烟（计划文档③）")
    parser.add_argument("--pack", required=True, help="世界包路径（profile 按目录名匹配）")
    parser.add_argument("--days", type=int, default=None, help="日数预算（缺省用 profile）")
    parser.add_argument("--seed", type=int, default=11, help="RNG 种子")
    parser.add_argument("--offline", action="store_true", help="离线模式：内嵌假客户端，不触网")
    args = parser.parse_args(argv)

    pack = load_worldpack(args.pack)
    profile = PROFILES.get(pack.root.name) or _profile_from_file(pack)
    if profile is None:
        print(
            f"[✗] 未配置 {pack.root.name} 的冒烟 profile："
            f"请在 PROFILES 中登记，或提供 {pack.root}/smoke_profile.yaml"
            f"（可由 scripts/import_story.py 自动生成）"
        )
        return 1
    day_cap = args.days or profile["days"]

    settings = load_settings()
    if not args.offline and not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（离线回归请加 --offline）")
        return 1

    state = GameState.from_pack(pack)
    if args.offline:
        llm = LLMClient(_OfflineFake(), "offline-fake", build_tools(pack.schedule))
        tracker = None
    else:
        tracker = UsageTracker(SAVE_DIR / f"usage-smoke-{pack.root.name}.jsonl")
        llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)
    game = Game(
        pack, state, llm, rng=random.Random(args.seed),
        extract_every=2, compress_threshold=30000, judge_every=5, reflect_every=10,
    )

    transcript: list[str] = []

    def log(*parts) -> None:
        text = "\n".join(str(p) for p in parts if p)
        transcript.append(text + "\n" + "-" * 60)
        print(text)
        print("-" * 60)

    try:
        print(f"pack={pack.world.name} · seed={args.seed} · 日预算 {day_cap} · "
              f"目标 {profile['target']} · {'离线模式' if args.offline else f'model={settings.model}'}\n")

        view = game.start()
        line_idx = 0
        said = False
        while state.day <= day_cap and view.ending is None:
            if view.choice_prompt is not None:
                pick = profile["picks"][view.choice_prompt.id]
                log(f"【关键抉择·{view.choice_prompt.id}】", view.choice_prompt.prompt)
                view = game.pick(pick)
                log("【抉择之后】", view.narration)
                continue

            if state.action_points_left > 0:
                view = game.act(profile["action"])
                log(f"【第 {state.day} 天·行动】", view.narration)
                continue

            if state.day % 2 == 0 and not said:
                said = True
                line = profile["lines"][line_idx % len(profile["lines"])]
                line_idx += 1
                view = game.say(line)
                log(f"【第 {state.day} 天·对话】{line}", view.narration)
                continue

            said = False
            game.end_day()
            log(
                f"—— 第 {state.day} 天 ——",
                f"[状态] 属性 {state.stats} · 好感 {state.affections}",
            )

        if view.ending is not None:
            log(f"【结局】{view.ending.title}", view.ending.text)
        else:
            log(
                f"[未达成结局] 第 {state.day} 天超出预算",
                f"好感 {state.affections} · flags {state.flags}",
            )

        # 数值零偏差审计
        deviations = audit_stats(pack, state)
        if deviations:
            log("[✗] 数值零偏差审计失败：", *deviations)
            status = 1
        else:
            log(f"[✓] 数值零偏差审计通过（{len(state.stat_log)} 条变更记录）")
            status = 0

        # 禁表扫描（硬失败）+ 观察项（报告）
        full_text = "\n".join(transcript)
        leaked = [w for w in profile["forbidden_scan"] if w in full_text]
        if leaked:
            log(f"[✗] 禁表扫描发现泄漏: {leaked}")
            status = 1
        else:
            log("[✓] 禁表扫描通过（无世界观外元素）")
        if profile["observe_modern"]:
            hits = {w: full_text.count(w) for w in profile["observe_modern"] if w in full_text}
            log("[观察] 世界观合法元素出现: " + (str(hits) or "（无）"))
        if profile.get("archaic_scan"):
            arch = {w: full_text.count(w) for w in profile["archaic_scan"] if w in full_text}
            log("[观察] 古风残留标记（含子串误报可能）: " + (str(arch) or "（无）"))

        # 落盘（离线模式也落盘，便于比对）
        SAVE_DIR.mkdir(exist_ok=True)
        save_game(state, SAVE_DIR / f"smoke-{pack.root.name}.json", game.history)
        (SAVE_DIR / f"smoke-{pack.root.name}.txt").write_text(
            "\n\n".join(transcript), encoding="utf-8"
        )
        print(f"\n已保存 → saves/smoke-{pack.root.name}.{{json,txt}}")
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

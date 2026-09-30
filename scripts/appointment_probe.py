"""真机探针：叙述模型是否真的会调用 make_appointment（约定真值）。

离线测试只能证明"约定落盘后每轮都在上下文里"；**模型会不会主动调用工具**
只能真机验（与规则第 10 条 do_action 当初同样的处境）。

关键：必须让 NPC **真的在场**（`present_npcs`），否则模型合理地不把话当约定
——首版探针就踩了这个坑（玩家对空教室里的空气说"周五见"，模型没调用是对的）。

用法：
  uv run python scripts/appointment_probe.py [--pack world-packs/campus_otome] [--runs 3]
"""

from __future__ import annotations

import argparse

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

# 明确、带具体日期、且 NPC 在场的邀约——引擎规则第 11 条要求此时必须调用工具
PROMPT = (
    "（我在天台上站定，看着江屿）那说定了：第五天傍晚六点，樱花道第三排第七个灯钩，"
    "我把那盏旧灯笼带来，你等我。"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="make_appointment 真机探针")
    ap.add_argument("--pack", default="world-packs/campus_otome")
    ap.add_argument("--runs", type=int, default=1, help="重复次数（看稳定性）")
    args = ap.parse_args(argv)

    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY")
        return 1

    tracker = UsageTracker("saves/usage-appointment-probe.jsonl")
    pack = load_worldpack(args.pack)
    hits = 0

    for run in range(1, args.runs + 1):
        state = GameState.from_pack(pack)
        llm = LLMClient.from_settings(settings, build_tools(pack.schedule), tracker=tracker)
        # 真机客户端不记录调用；包一层以取回真实 messages（验证注入用）
        sent: list[dict] = []
        _real_create = llm._client.chat.completions.create

        def _capture(**kwargs):
            sent.append(kwargs)
            return _real_create(**kwargs)

        llm._client.chat.completions.create = _capture
        game = Game(pack, state, llm)
        state.day = 3
        # 关键：先"占住"节点——否则 begin_turn 会进 N1 并把 scene/present_npcs
        # 按节点 on_enter 覆盖掉（引擎正确行为：场景与在场角色由节点拥有）。
        # 首版探针没做这步，模型在本班里对空气说话，不调用工具是**对的**。
        state.current_node = pack.mainline.nodes[0].id
        state.scene = "青槐高中·天台"
        state.present_npcs = ["jiang_yu"]
        # 关键抉择门与本探针无关（campus_otome 每节点都挂抉择，会短路回合）
        game.story.choice_locked = lambda s: False
        game.story.pending_choice = lambda s: None

        print(f"\n===== 第 {run} 次 =====")
        view = game.say(PROMPT)
        print("叙事:", (view.narration or "")[:220].replace("\n", " "))
        print("（本轮场景/在场:", state.scene, state.present_npcs, "）")

        appts = state.appointments
        called = bool(appts)
        hits += called
        print(f"make_appointment 被调用: {called}")
        for a in appts:
            print(f"  落盘 → npc={a.with_npc} what={a.what!r} "
                  f"due_day={a.due_day} made_day={a.made_day}")

        # 下一轮真实发给模型的上下文里是否带上了约定
        if appts:
            state.day = appts[0].due_day  # 约定当天
            game.say("（我提着灯笼往樱花道走）")
            blob = "\n".join(
                str(m.get("content") or "") for m in sent[-1]["messages"]
            )
            print("约定当天上下文含 <约定>:", "<约定>" in blob)
            print("约定内容进入上下文:", appts[0].what in blob)
            print("标记为今日到期:", "今日到期" in blob)

    print(f"\n===== 调用率 {hits}/{args.runs} =====")
    print(tracker.cost_report())
    return 0 if hits == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""F1 开场提示真机冒烟（P0 / improvement-roadmap §8 F1）：引擎零内容文案泄漏。

用法：uv run python scripts/start_smoke.py

三段验证：
1. 临时中性世界包（不含「长安城」）：开局叙事不得出现「长安城」——硬证明引擎
   不再注入内容层文案（换包即玩的核心承诺）；
2. ancient_jianghu：正常开局（包自身含长安城，仅打印供人审）；
3. baseline_probe：开局叙事（长安城来自包内容，仅打印对照）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from game_agent.config import load_settings
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools, make_client
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

NEUTRAL_PACK = {
    "world.yaml": """\
name: 烟雨录
era: 架空古代·临安府
start_scene: 临安·西子湖畔
player_role: 云游画师
player_goal: 在临安寻访故人
core_rules:
  - 玩家是初到临安的云游画师
style_guide:
  - 古风白话，克制典雅
forbidden:
  - 现代事物
opening: |
  江南三月，烟雨蒙蒙。
  你背着画箱，踏入了临安城。
  湖上画舫往来，柳色如烟。
""",
    "schedule.yaml": """\
day_action_points: 1
stats:
  charm: {label: 魅力, min: 0, max: 100, initial: 10}
affections:
  a_man: {label: 阿蛮, min: 0, max: 100, initial: 5}
flags: {}
actions: []
""",
    "mainline.yaml": "nodes: []\n",
    "events.yaml": "events: []\n",
    "endings.yaml": "endings: []\n",
    "npcs/a_man.yaml": """\
id: a_man
name: 阿蛮
identity: 湖畔茶寮的采茶女
personality: 爽朗直率，心细如发
speech_style: 吴侬软语，偶用乡谚
affection_stages:
  - {range: [0, 100], tone: 友善}
memory_limit: 20
""",
}


def _write_neutral_pack(tmp: Path) -> Path:
    root = tmp / "neutral"
    for rel, content in NEUTRAL_PACK.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _run_once(pack_path, label: str, forbid: str | None) -> bool:
    pack = load_worldpack(pack_path)
    state = GameState.from_pack(pack)
    llm = LLMClient(make_client(settings), settings.model, build_tools(pack.schedule))
    game = Game(pack, state, llm)
    view = game.start()
    if view.choice_prompt is not None:  # 开局即关键抉择的包：先选第一项再取叙事
        view = game.pick(0)
    narration = view.narration or ""
    print(f"---- {label} ----")
    print(narration)
    print()
    if forbid is not None and forbid in narration:
        print(f"[✗] {label}: 叙事出现禁止内容「{forbid}」——引擎疑似泄漏内容层文案")
        return False
    print(f"[✓] {label} 通过")
    return True


def main() -> int:
    global settings
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未找到 DEEPSEEK_API_KEY，请先配置 .env")
        return 1

    ok = True
    # 1. 中性包硬证明：包内无「长安城」，叙事不得出现
    with tempfile.TemporaryDirectory() as tmp:
        try:
            ok &= _run_once(_write_neutral_pack(Path(tmp)), "中性世界包（烟雨录·临安）", "长安城")
        except LLMTurnError as e:
            print(f"[✗] 中性包协议熔断: {e}")
            ok = False
    # 2/3. 真实包对照（长安城来自包内容，属合法）
    try:
        ok &= _run_once("world-packs/ancient_jianghu", "ancient_jianghu（江湖旧梦）", None)
        ok &= _run_once("world-packs/baseline_probe", "baseline_probe（探针）", None)
    except LLMTurnError as e:
        print(f"[✗] 协议熔断: {e}")
        ok = False

    print("\n[✓] F1 冒烟通过" if ok else "\n[✗] F1 冒烟失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""事实图接地完整性守卫：凡 status_text 注入的材料，都必须能接地。

**缺陷背景**（我引入的洞）：约定真值（`state.appointments`）加入后，`status_text`
会**无条件常驻注入** `<约定>` 区块，但 `build_graph` 的接地事实没同步——
于是模型叙述约定内容（引擎真值）时，缺席证据检查会判「虚构事实」并注入
`【校验反馈】`。关系洞察（`npc_insights`）同样注入角色卡却不在图内。

判据口径（见 `factgraph._anchor_tokens`）：锚点 = 数字 + 「」串 + **2~4 字窗口**，
任一锚点接地即通过；全部不在图内才算缺席。所以断言用 2~4 字词，而非整句。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp

from game_agent.context import ContextBuilder
from game_agent.factgraph import build_graph, check_graph
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import Appointment, GameState, InsightEntry, MemoryEntry
from game_agent.worldpack import load_worldpack

PACK = Path(__file__).resolve().parent.parent / "world-packs" / "campus_otome"
WHAT = "去城隍庙还愿"


def _state():
    pack = load_worldpack(PACK)
    state = GameState.from_pack(pack)
    state.present_npcs = ["jiang_yu"]
    state.day, state.turn_count = 10, 30
    return pack, state


def _appt():
    return Appointment(id="a1", with_npc="jiang_yu", what=WHAT, due_day=12, made_day=10)


def _llm_returning_claim(claim: str):
    """假模型：factcheck 抽取器原样吐出给定断言。"""
    return LLMClient(FakeClient([resp(msg(content=claim))] * 4), "fake", [])


# ---------------------------------------------------------------------------
# 图内容：约定与洞察必须入图
# ---------------------------------------------------------------------------


def test_appointment_content_grounds_in_fact_graph():
    pack, state = _state()
    state.appointments.append(_appt())

    graph = build_graph(pack, state)
    for token in ("城隍庙", "还愿"):
        assert graph.has(token), f"约定的内容 '{token}' 不在事实图里"


def test_appointment_npc_name_grounds():
    pack, state = _state()
    state.appointments.append(_appt())

    assert build_graph(pack, state).has("江屿")


def test_insight_text_grounds_in_fact_graph():
    pack, state = _state()
    state.npc_insights["jiang_yu"] = [
        InsightEntry(text="江屿对你的态度正从客气转向信任", day=9, round=20)
    ]

    graph = build_graph(pack, state)
    for token in ("信任", "转向"):
        assert graph.has(token), f"洞察文本 '{token}' 不在事实图里"


# ---------------------------------------------------------------------------
# 端到端：check_graph 不得把引擎真值判为虚构（本洞的复现钉）
# ---------------------------------------------------------------------------


def test_claim_quoting_appointment_is_not_confabulation():
    """模型转述约定内容 → 缺席检查必须放行（此前会判「虚构事实」）。"""
    pack, state = _state()
    state.appointments.append(_appt())
    graph = build_graph(pack, state)

    assert check_graph(_llm_returning_claim("与江屿约定去城隍庙还愿"), "x", graph) is None


def test_claim_with_unrelated_content_still_flagged():
    """回归保护：真正编造的内容仍必须被拦下（不能因为放宽而失去判据）。"""
    pack, state = _state()
    state.appointments.append(_appt())
    graph = build_graph(pack, state)

    verdict = check_graph(_llm_returning_claim("约定送她一匹汗血马"), "x", graph)
    assert verdict is not None, "真编造的内容应被拦下"
    assert "虚构事实" in verdict


def test_materials_and_graph_agree_on_appointments():
    """口径一致性：status_text 注入的 <约定> 内容，必须全部在图内。"""
    pack, state = _state()
    state.appointments.append(_appt())

    injected = ContextBuilder.from_pack(pack).status_text(state, None, recent="闲聊")
    assert "<约定>" in injected
    graph = build_graph(pack, state)
    for token in ("江屿", "城隍庙", "还愿"):
        assert token in injected, f"{token} 未注入"
        assert graph.has(token), f"{token} 已注入但未接地——判官会误判虚构"


# ---------------------------------------------------------------------------
# 回归保护：既有纪律不能被这次改动破坏
# ---------------------------------------------------------------------------


def test_superseded_facts_still_excluded_from_graph():
    """被取代的事实仍不得接地（A5 纪律）。"""
    pack, state = _state()
    state.player_facts.append(
        MemoryEntry(fact="玩家的旧称号是剑客", day=1, round=1, superseded=True)
    )

    assert not build_graph(pack, state).has("剑客")


def test_build_graph_does_not_crash_without_appointments():
    """老档 / 无约定场景：build_graph 不得因缺字段抛错。"""
    pack, state = _state()
    state.appointments = []
    state.npc_insights = {}

    graph = build_graph(pack, state)
    assert graph.facts, "图不应为空"

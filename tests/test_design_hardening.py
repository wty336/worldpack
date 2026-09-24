"""设计加固（docs/plan-design-hardening.md 批次 A/B）守卫测试。

- A1：事实图纳入压缩摘要与选择日志——摘要内事实引用不误报，真编造照旧拦截；
- A2：判官材料附上一轮叙事（连贯性证据面）+ JUDGE_SYSTEM 口径；
- A3：长程反重复（滑动窗口 n-gram 覆盖率）+ 相邻轮路径回归；
- A4：卡壳推进提示触发重规划（计划替换 + 快照刷新）；
- A5：协议熔断 → 保守回合（不抛异常、可继续、熔断文案不进反重复窗口）；
- B1：缺席证据检查常开轮 + judge 采样轮跳过（避免同轮两次抽取）；
- B2：校验反馈复查闭环（已修正清除 / 未修正升级一次 / 未知不升级）。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.compression import summary_text
from game_agent.factgraph import build_graph, check_graph
from game_agent.game import Game
from game_agent.judge import JUDGE_SYSTEM, recheck_feedback
from game_agent.llm import LLMClient, build_tools
from game_agent.state import ChoiceRecord, GameState
from game_agent.storyline import FREE_INPUT_OPTION
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "ancient_jianghu"


def _pack():
    return load_worldpack(PACK_PATH)


def _n1_done(s: GameState) -> None:
    """预置：N1 已完成（跳过开场节点，直接测日常轮逻辑）。"""
    s.completed_nodes.append("n1_first_meeting")
    s.flags["met_shen"] = True


def _game(responses, mutate=None):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    if mutate is not None:
        mutate(state)
    llm = LLMClient(FakeClient(responses), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm)


def _llm_with(*contents):
    return LLMClient(FakeClient([resp(msg(content=c)) for c in contents]), "fake", [])


def _submit(narration: str, call_id: str = "s1"):
    return tool_call(
        call_id,
        "submit_narration",
        {"narration": narration, "choices": ["一", "二", "三"], "plot_signal": "normal"},
    )


def _quiet(game: Game) -> None:
    """关闭 extract/reflect/熔断以外的侧信道，让调用序列可精确断言。"""
    game.extract_every = 0
    game.reflect_every = 0


# ---------------------------------------------------------------------------
# A1：事实图纳入摘要与选择日志
# ---------------------------------------------------------------------------


def test_summary_text_extracts_body():
    history = [
        {"role": "user", "name": "engine", "content": "【剧情摘要】\n玩家欠沈府五十两，约定中秋前归还。"},
        {"role": "user", "content": "最新玩家输入"},
    ]
    assert summary_text(history) == "玩家欠沈府五十两，约定中秋前归还。"
    assert summary_text([{"role": "user", "content": "无摘要"}]) == ""
    assert summary_text([]) == ""


def test_summary_grounds_claims():
    """摘要内的事实（长局中被压缩）引用不判虚构；不入图则缺席违规。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.present_npcs = ["shen_qingqiu"]
    history = [
        {
            "role": "user",
            "name": "engine",
            "content": "【剧情摘要】\n玩家欠沈府五十两，约定中秋前归还。",
        }
    ]
    graph_with = build_graph(pack, state, history)
    graph_without = build_graph(pack, state)
    assert graph_with.has("五十两") and graph_with.has("中秋前")
    assert not graph_without.has("五十两")

    llm = _llm_with("欠五十两中秋前归还")  # 抽取器返回的断言要点
    assert check_graph(llm, "你还记得欠下的银子。", graph_with) is None
    llm2 = _llm_with("欠五十两中秋前归还")
    assert check_graph(llm2, "你还记得欠下的银子。", graph_without) is not None


def test_choice_log_grounds_claims():
    """关键选择是代码结算的既成剧情，其文本接地断言；缺省（history=None）不入图。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    state.choice_log.append(
        ChoiceRecord(day=1, node_id="n1_first_meeting", choice_id="c1.1", text="把祖传玉佩赠予沈清秋")
    )
    graph = build_graph(pack, state, [])
    graph_default = build_graph(pack, state)
    assert graph.has("祖传玉佩")
    assert not graph_default.has("祖传玉佩")  # 向后兼容：不传 history 行为不变


def test_fabricated_still_rejected_with_history():
    """图扩大后真编造仍拦截（护栏：接地面变宽不等于放水）。"""
    pack = _pack()
    state = GameState.from_pack(pack)
    history = [
        {"role": "user", "name": "engine", "content": "【剧情摘要】\n玩家欠沈府五十两。"}
    ]
    graph = build_graph(pack, state, history)
    llm = _llm_with("把密码本交给了白鸮")
    assert check_graph(llm, "密谋完成。", graph) is not None


# ---------------------------------------------------------------------------
# A2：判官材料附上一轮叙事
# ---------------------------------------------------------------------------


def test_judge_materials_includes_last_narration():
    pack, state, game = _game([], mutate=_n1_done)
    game.last_narration = "上一轮沈清秋转身离开。"
    materials = game._judge_materials()
    assert "<上一轮叙事>" in materials
    assert "上一轮沈清秋转身离开。" in materials


def test_judge_materials_first_turn_has_no_prev():
    pack, state, game = _game([], mutate=_n1_done)
    assert "<上一轮叙事>" not in game._judge_materials()


def test_judge_system_covers_continuity():
    """② 口径含"与上一轮叙事直接矛盾"——design §10.2-4 的证据面落地。"""
    assert "上一轮叙事" in JUDGE_SYSTEM


# ---------------------------------------------------------------------------
# A3：长程反重复
# ---------------------------------------------------------------------------


def test_long_repetition_injects_hint():
    pack, state, game = _game([], mutate=_n1_done)
    game._narration_window = ["长安城的春天柳絮纷飞沈清秋在后院练剑她说要参加七夕诗会赢得头名"]
    game.last_narration = "今日无事。"  # 相邻轮不命中 → 走长程检查
    game._check_repetition("春去秋来长安城的春天柳絮纷飞沈清秋在后院练剑的画面又浮现眼前")
    assert any("更早回合高度重复" in (m.get("content") or "") for m in game.history)


def test_long_repetition_ignores_new_content():
    pack, state, game = _game([], mutate=_n1_done)
    game._narration_window = ["沈清秋在后院练剑她说要参加七夕诗会"]
    game.last_narration = "今日无事。"
    game._check_repetition("北方的荒漠中驼铃声声商队缓缓前行黄沙漫天遮蔽了远山")
    assert not any("反重复提示" in (m.get("content") or "") for m in game.history)


def test_adjacent_repetition_path_unchanged():
    """相邻轮相似度路径回归：原有提示文本不变。"""
    pack, state, game = _game([], mutate=_n1_done)
    text = "沈清秋在庭院中抚琴一曲琴音悠扬婉转动人令人心醉神迷"
    game.last_narration = text
    game._check_repetition(text + "琴音依旧绕梁")
    assert any("与上一轮高度重复" in (m.get("content") or "") for m in game.history)


def test_narration_window_trimmed():
    pack, state, game = _game([], mutate=_n1_done)
    game.last_narration = ""
    for i in range(10):
        game._check_repetition(f"第{i}轮的叙事内容各不相同,例如关于剑法修炼的描述{i * 111}")
    assert len(game._narration_window) == 6


# ---------------------------------------------------------------------------
# A4：卡壳推进提示触发重规划
# ---------------------------------------------------------------------------

_NEW_PLAN = "1. 打听诗会消息\n2. 取得参赛资格\n3. 完成诗会对策"


def test_replan_on_stuck_hint():
    pack, state, game = _game([resp(msg(content=_NEW_PLAN))])
    game.plan_node = True
    state.current_node = "n1_first_meeting"
    state.node_plan = ["早已过时的旧计划"]
    state.node_plan_step = 0
    state.node_flags_snapshot = {"met_shen": False}
    game._maybe_replan(
        [{"role": "user", "name": "engine", "content": "【推进提示】请向目标推进"}]
    )
    assert state.node_plan == ["打听诗会消息", "取得参赛资格", "完成诗会对策"]
    assert state.node_plan_step == 0
    assert state.node_flags_snapshot == state.flags  # 新计划新基线


def test_replan_only_on_stuck_hint():
    """无推进提示不触发；plan_node 关闭不触发（零 LLM 调用 = 响应队列耗尽即炸）。"""
    pack, state, game = _game([])
    game.plan_node = True
    state.node_plan = ["旧计划"]
    game._maybe_replan([{"content": "【节点完成】主线达成"}])
    assert state.node_plan == ["旧计划"]

    pack2, state2, game2 = _game([])
    game2.plan_node = False
    game2._maybe_replan([{"content": "【推进提示】请向目标推进"}])


# ---------------------------------------------------------------------------
# A5：熔断保守回合
# ---------------------------------------------------------------------------


def test_meltdown_returns_conservative_view():
    """连续协议失败 → 不抛 LLMTurnError，返回保守视图，历史含熔断标记。"""
    responses = [resp(msg(content="纯文本无工具"), finish_reason="stop") for _ in range(3)]
    pack, state, game = _game(responses, mutate=_n1_done)
    view = game.say("你好")
    assert view.narration is not None and "生成失败" in view.narration
    assert view.choices == [FREE_INPUT_OPTION]
    assert any("[引擎熔断]" in (m.get("content") or "") for m in game.history)


def test_game_recovers_after_meltdown():
    """熔断轮之后下一回合照常工作（会话不死）。"""
    responses = [
        resp(msg(content="a")),
        resp(msg(content="b")),
        resp(msg(content="c")),
        resp(msg(tool_calls=[_submit("熔断后恢复正常叙事")])),
    ]
    pack, state, game = _game(responses, mutate=_n1_done)
    bad = game.say("你好")
    assert "生成失败" in (bad.narration or "")
    good = game.say("继续")
    assert good.narration == "熔断后恢复正常叙事"
    # 熔断兜底文案不进反重复窗口（A5 细节）
    assert game.last_narration == "熔断后恢复正常叙事"
    assert all("[引擎熔断]" not in str(game._narration_window) for _ in [0])


# ---------------------------------------------------------------------------
# B1：缺席证据检查常开轮
# ---------------------------------------------------------------------------


def test_factcheck_every_injects_feedback():
    """factcheck_every=1：图外断言 → 缺席违规 → 反馈注入 + 复查登记。"""
    responses = [
        resp(msg(tool_calls=[_submit("我把密码本交给了白鸮。")])),
        resp(msg(content="把密码本交给白鸮")),  # 抽取器返回断言要点
    ]
    pack, state, game = _game(responses, mutate=_n1_done)
    game.factcheck_every = 1
    game.judge_every = 0
    _quiet(game)
    game.say("干活")
    assert any("【校验反馈】" in (m.get("content") or "") for m in game.history)
    assert game._pending_verdict and "把密码本交给白鸮" in game._pending_verdict


def test_factcheck_pass_no_feedback():
    """接地断言不注入反馈、不登记复查。"""
    responses = [
        resp(msg(tool_calls=[_submit("沈清秋依旧在沈府后院。")])),
        resp(msg(content="无")),  # 无断言
    ]
    pack, state, game = _game(responses, mutate=_n1_done)
    game.factcheck_every = 1
    game.judge_every = 0
    _quiet(game)
    game.say("随便说说")
    assert not any("【校验反馈】" in (m.get("content") or "") for m in game.history)
    assert game._pending_verdict is None


def test_factcheck_skipped_on_judge_round():
    """judge 采样轮跳过独立检查：总调用 = turn + judge 判定 + 图抽取（无第二次抽取）。"""
    responses = [
        resp(msg(tool_calls=[_submit("日常叙事。")])),
        resp(msg(content="通过")),  # judge 判定
        resp(msg(content="无")),  # judge.check 内附带的图抽取
    ]
    pack, state, game = _game(responses, mutate=_n1_done)
    game.factcheck_every = 1
    game.judge_every = 1
    _quiet(game)
    game.say("继续")
    assert len(game.llm._client.chat.completions.calls) == 3


# ---------------------------------------------------------------------------
# B2：校验反馈复查闭环
# ---------------------------------------------------------------------------


def test_recheck_feedback_tri_state():
    assert recheck_feedback(_llm_with("已修正"), "新叙事", "旧问题") is True
    assert recheck_feedback(_llm_with("未修正"), "新叙事", "旧问题") is False
    assert recheck_feedback(_llm_with(""), "新叙事", "旧问题") is None  # 空响应 = 未知
    assert recheck_feedback(_llm_with(), "新叙事", "") is None  # 无反馈不调用


def test_feedback_recheck_cleared_on_fix():
    """反馈注入 → 下轮复查"已修正" → 队列清除、无升级。"""
    responses = [
        resp(msg(tool_calls=[_submit("有问题的叙事。", "s1")])),
        resp(msg(content="问题类型：OOC 测试问题")),  # 回合1 judge
        resp(msg(content="无")),  # 回合1 图抽取
        resp(msg(tool_calls=[_submit("修正后的叙事。", "s2")])),
        resp(msg(content="已修正")),  # 回合2 复查
        resp(msg(content="通过")),  # 回合2 judge
        resp(msg(content="无")),  # 回合2 图抽取
    ]
    pack, state, game = _game(responses, mutate=_n1_done)
    game.judge_every = 1
    _quiet(game)
    game.say("第一句")
    assert game._pending_verdict is not None
    game.say("第二句")
    assert game._pending_verdict is None
    assert not any("仍未修正" in (m.get("content") or "") for m in game.history)


def test_feedback_recheck_escalates_once():
    """复查"未修正" → 升级反馈一次；不再登记第二次复查（无连环）。"""
    responses = [
        resp(msg(tool_calls=[_submit("有问题的叙事。", "s1")])),
        resp(msg(content="问题类型：OOC 测试问题")),
        resp(msg(content="无")),
        resp(msg(tool_calls=[_submit("还是老样子。", "s2")])),
        resp(msg(content="未修正")),  # 复查
        resp(msg(content="通过")),  # 回合2 judge
        resp(msg(content="无")),  # 回合2 图抽取
        resp(msg(tool_calls=[_submit("第三轮叙事。", "s3")])),
        resp(msg(content="通过")),  # 回合3 judge
        resp(msg(content="无")),  # 回合3 图抽取
    ]
    pack, state, game = _game(responses, mutate=_n1_done)
    game.judge_every = 1
    _quiet(game)
    game.say("第一句")
    game.say("第二句")
    assert any("【校验反馈·仍未修正】" in (m.get("content") or "") for m in game.history)
    assert game._pending_verdict is None  # 升级后不再登记
    game.say("第三句")  # 若仍复查会耗尽响应队列而炸
    assert game.llm._client.chat.completions.responses == []  # 恰好用完 = 无多余调用

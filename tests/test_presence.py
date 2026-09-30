"""在场真值守卫：`present_npcs` 必须是当前剧情的真值，而不是上次行动的残留副作用。

**缺陷背景**：`present_npcs` 全仓只有三处写入——进节点、节点结束清空、执行日程行动。
**对话回合完全不动它**，而且模型没有任何工具能改变在场。于是：

- 叙事写"她告辞离去" → 引擎仍把她在场注入（角色卡 + 记忆照发，白烧上下文）；
- 叙事写"江屿从走廊那头过来" → 引擎认为他不在场，**他的角色卡与记忆都不注入**；
- 执行 `study_hall`（`present: []`）→ 在场被清空，要等下一次节点触发才恢复；
- 接着写"和阿零在旧终端区说话" → 记忆归因错误（"谁见证了这件事"这个信号不存在）。

契约：模型必须能**提议**在场增减，引擎校验后落盘为真值——与 `change_scene`
（场景从自由字符串升级为受校验真值）同范式。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.context import ContextBuilder
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK = Path(__file__).resolve().parent.parent / "world-packs" / "campus_otome"
NPC = "jiang_yu"
NPC_NAME = "江屿"


def _game_factory():
    pack = load_worldpack(PACK)
    state = GameState.from_pack(pack)
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    return pack, state, Game(pack, state, llm)


def _submit(n="叙事。"):
    return tool_call("s1", "submit_narration",
                     {"narration": n, "choices": ["一", "二", "三"], "plot_signal": "normal"})


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------


def test_change_presence_tool_registered():
    pack = load_worldpack(PACK)
    names = [t["function"]["name"] for t in build_tools(pack.schedule)]
    assert "change_presence" in names, f"工具未注册: {names}"
    schema = next(t for t in build_tools(pack.schedule)
                  if t["function"]["name"] == "change_presence")
    props = schema["function"]["parameters"]["properties"]
    assert set(props["enter"]["items"]["enum"]) == {"jiang_yu", "su_qing", "wen_yan", "xia_ming"}
    assert set(props["leave"]["items"]["enum"]) == {"jiang_yu", "su_qing", "wen_yan", "xia_ming"}
    assert schema["function"]["parameters"]["required"] == ["reason"]


# ---------------------------------------------------------------------------
# 写入路径：模型提议 → 引擎校验 → 落盘
# ---------------------------------------------------------------------------


def test_model_can_bring_npc_on_stage():
    pack, state, game = _game_factory()
    state.present_npcs = []

    result = game.registry.dispatch(
        "change_presence", {"enter": [NPC], "leave": [], "reason": "他推门进来"}
    )

    assert result.status == "ok", result.message
    assert NPC in state.present_npcs


def test_model_can_take_npc_off_stage():
    """叙事里角色离开后，必须能把他移出在场——否则角色卡与记忆一直白烧。"""
    pack, state, game = _game_factory()
    state.present_npcs = [NPC]

    result = game.registry.dispatch(
        "change_presence", {"enter": [], "leave": [NPC], "reason": "她告辞离去"}
    )

    assert result.status == "ok", result.message
    assert NPC not in state.present_npcs


def test_leave_npc_not_present_is_noop_not_error():
    """退场一个本来就不在场的人：幂等处理（叙事常见，不该报错打断回合）。"""
    pack, state, game = _game_factory()
    state.present_npcs = []

    result = game.registry.dispatch(
        "change_presence", {"enter": [], "leave": [NPC], "reason": "她本就不在"}
    )

    assert result.status == "ok"
    assert NPC not in state.present_npcs


def test_enter_is_idempotent():
    """重复入场不产生重复条目（否则角色卡会注入两次）。"""
    pack, state, game = _game_factory()
    state.present_npcs = [NPC]

    game.registry.dispatch("change_presence", {"enter": [NPC], "reason": "他还在"})

    assert state.present_npcs.count(NPC) == 1


# ---------------------------------------------------------------------------
# 校验纪律
# ---------------------------------------------------------------------------


def test_unknown_npc_rejected():
    pack, state, game = _game_factory()
    before = list(state.present_npcs)

    result = game.registry.dispatch(
        "change_presence", {"enter": ["不存在的人"], "reason": "x"}
    )

    assert result.status == "rejected"
    assert state.present_npcs == before, "被拒绝的提议不得改动真值"


def test_same_npc_in_enter_and_leave_rejected():
    """同一人同时进出 = 语义矛盾，必须拒绝而不是悄悄取其一。"""
    pack, state, game = _game_factory()

    result = game.registry.dispatch(
        "change_presence", {"enter": [NPC], "leave": [NPC], "reason": "x"}
    )

    assert result.status == "rejected"


def test_missing_reason_rejected():
    pack, state, game = _game_factory()

    result = game.registry.dispatch("change_presence", {"enter": [NPC]})

    assert result.status == "rejected"


def test_locked_during_critical_choice():
    """关键抉择期间在场由节点接管（与 change_scene 同守卫）。"""
    pack, state, game = _game_factory()
    game.start()  # 进入 N1，关键抉择待决
    assert game.story.choice_locked(state)

    result = game.registry.dispatch(
        "change_presence", {"enter": [NPC], "reason": "x"}
    )

    assert result.status == "rejected"


# ---------------------------------------------------------------------------
# 下游效果：在场真值驱动角色卡与记忆注入
# ---------------------------------------------------------------------------


def test_presence_controls_npc_card_injection():
    """在场决定**角色卡**是否注入——本修复的直接收益（省上下文 / 不再失明）。

    注意判据不能用 NPC 名：好感行（`好感：江屿 0/100…`）**恒常展示**，与在场无关。
    角色卡的特征是 identity/性格/说话风格，故用 identity 文本作判据。
    """
    pack = load_worldpack(PACK)
    state = GameState.from_pack(pack)
    cb = ContextBuilder.from_pack(pack)
    card_marker = pack.npcs[NPC].identity

    state.present_npcs = []
    text_absent = cb.status_text(state, None, recent="闲聊")
    assert card_marker not in text_absent, "不在场却注入了角色卡"

    state.present_npcs = [NPC]
    text_present = cb.status_text(state, None, recent="闲聊")
    assert card_marker in text_present, "在场却没注入角色卡"


def test_presence_survives_save_roundtrip():
    """在场是真值 → 必须随存档回环（读档后不能丢在场）。"""
    pack, state, game = _game_factory()
    state.present_npcs = [NPC]

    restored = GameState.from_dict(state.to_dict())

    assert restored.present_npcs == [NPC]


# ---------------------------------------------------------------------------
# 协议集成：模型在一轮里调整在场
# ---------------------------------------------------------------------------


def test_full_round_applies_presence():
    pack = load_worldpack(PACK)
    state = GameState.from_pack(pack)
    state.present_npcs = []
    calls = [
        tool_call("p1", "change_presence",
                  {"enter": [NPC], "leave": [], "reason": "他推门进来"}),
        _submit("江屿推门进来，在门口站住了。"),
    ]
    llm = LLMClient(FakeClient([resp(msg(tool_calls=calls))]), "fake",
                    build_tools(pack.schedule))
    game = Game(pack, state, llm)
    game.story.choice_locked = lambda s: False
    game.story.pending_choice = lambda s: None

    game.say("有人进来吗")

    assert NPC in state.present_npcs
    tool_msgs = [m.get("content") or "" for m in game.history if m.get("role") == "tool"]
    assert any("在场" in c for c in tool_msgs), f"结果未回传模型: {tool_msgs}"

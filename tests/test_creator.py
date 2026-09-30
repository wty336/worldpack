"""N6 / E-8 守卫：创作者 Agent（对话式改人物设定与世界书）。

上游：`docs/plan-tavern-shaped-product.md` §4。

**被钉住的缺口**：三条能力里最后一条。前两条做完之后，"能从素材生成卡""能选卡玩"
都有了，但**改**只能靠手改 YAML——而用户的原始需求原话是
「也可以与 Agent 对话，从素材制作新卡，**修改人物设定和世界书**」。

本文件守五类性质：

1. **编辑只落在工作版（草稿）**——原始包在结构上只读，这里没有任何一条路径能写到
   已发布区；
2. **字段白名单**——白名单外的字段必须被**明确拒绝**，而不是静默丢掉
   （静默丢字段是"改了半天没生效"这类故障的根源）；
3. **校验口径与发布口径同源**——`validate()` 用的就是发布闸门那一次 `load_worldpack`，
   不许出现"Agent 说通过了、发布时却被拒"；
4. **修复循环真的闭环**——改坏之后 `validate_pack` 把原文回给模型，模型能据此改对；
5. **循环有上限且如实报告**——达到迭代上限不是错误，但不能假装做完了。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fakes import FakeClient, msg, resp, tool_call

import game_agent.catalog as catalog
from game_agent.creator import (
    MAX_CREATOR_ITERS,
    CreatorError,
    CreatorSession,
    WorkingCopy,
    build_registry,
)
from game_agent.llm import LLMClient
from game_agent.usage import UsageTracker
from game_agent.worldpack import PACK_CONTENT_FILES

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


@pytest.fixture
def work(tmp_path) -> Path:
    """一份隔离的工作版（从真实包复制，不碰仓库里的内容）。"""
    d = tmp_path / "_drafts" / "probe"
    shutil.copytree(REAL_PACK, d)
    return d


def _yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _llm(*responses) -> LLMClient:
    return LLMClient(FakeClient(list(responses)), "fake", [])


# ---------------------------------------------------------------------------
# 1. 编辑只落在工作版
# ---------------------------------------------------------------------------


def test_edits_only_touch_the_working_copy(work, tmp_path):
    """**核心断言**：改完之后原始包逐字节没变。

    这条比"草稿变了"更重要：草稿是沙箱，改坏了自己承担；而原始包可能正被某个
    玩家玩着，或者就是作者的定稿。工作版与原始包是**同一份内容的两个副本**，
    所以这条断言是"没有写穿"的直接证据。
    """
    original_before = (REAL_PACK / "world.yaml").read_bytes()
    npc_files = sorted(p.name for p in (REAL_PACK / "npcs").glob("*.yaml"))
    npc_before = {n: (REAL_PACK / "npcs" / n).read_bytes() for n in npc_files}

    wc = WorkingCopy(root=work)
    wc.update_world_field("era", "改过的时代")
    npc_id = wc.list_npcs()[0]["id"]
    wc.update_npc_field(npc_id, "personality", "改过的性格")

    assert (REAL_PACK / "world.yaml").read_bytes() == original_before, "写穿了到原始包！"
    for n in npc_files:
        assert (REAL_PACK / "npcs" / n).read_bytes() == npc_before[n], f"写穿了 {n}"
    # 而工作版确实变了
    assert _yaml(work / "world.yaml")["era"] == "改过的时代"
    assert _yaml(work / "npcs" / f"{npc_id}.yaml")["personality"] == "改过的性格"


def test_yaml_roundtrip_keeps_other_fields_and_chinese(work):
    """改一个字段不许动别的字段，也不许把中文转义成 \\uXXXX。

    这是"用 YAML 当数据库"最容易踩的两个坑：整体 dump 一个解析结果会顺手重排/丢键，
    而不带 `allow_unicode` 会把整份文件变成转义序列（内容还在，但人没法读、diff 没法看）。
    """
    wc = WorkingCopy(root=work)
    before = _yaml(work / "world.yaml")
    wc.update_world_field("era", "新朝代")
    after = _yaml(work / "world.yaml")

    assert after["era"] == "新朝代"
    changed_keys = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed_keys == {"era"}, f"只该改 era，实际动了 {sorted(changed_keys)}"
    raw = (work / "world.yaml").read_text(encoding="utf-8")
    assert "\\u" not in raw, "中文被转义了——allow_unicode 漏了"
    assert before["name"] == after["name"]


def test_npc_id_never_joins_a_path(work):
    """NPC id 是客户端/模型可控字符串，**绝不参与路径拼接**（与 catalog 同一条纪律）。"""
    wc = WorkingCopy(root=work)
    for evil in ("../world", "..\\world", "a/b", ""):
        with pytest.raises(CreatorError):
            wc.read_npc(evil)


# ---------------------------------------------------------------------------
# 2. 字段白名单：拒绝要明确，不能静默丢
# ---------------------------------------------------------------------------


def test_unknown_field_is_rejected_loudly_and_nothing_is_written(work):
    """白名单外的字段必须**拒绝**，且文件一个字节都不变。

    为什么这条重要：如果实现成"set 进去再说"，模型会以为改成功了，
    而 YAML 里多出一个游戏根本不认识的键——**改动看起来生效、实际没有**。
    """
    wc = WorkingCopy(root=work)
    before = (work / "world.yaml").read_bytes()
    for bad in ("start_scene_days", "max_recursion", "locations", "lore", "不存在的字段"):
        with pytest.raises(CreatorError) as e:
            wc.update_world_field(bad, "x")
        assert "不可改" in str(e.value)
        assert "可改" in str(e.value), "拒绝时要告诉模型哪些能改（否则它会一直试）"
    assert (work / "world.yaml").read_bytes() == before

    npc_id = wc.list_npcs()[0]["id"]
    npc_before = (work / "npcs" / f"{npc_id}.yaml").read_bytes()
    for bad in ("memory_limit", "affection_stages", "id", "secrets_x"):
        with pytest.raises(CreatorError):
            wc.update_npc_field(npc_id, bad, "x")
    assert (work / "npcs" / f"{npc_id}.yaml").read_bytes() == npc_before


def test_type_mismatch_is_rejected(work):
    """字符串字段收到数组、列表字段收到数字 → 拒绝（而不是写出一个坏 YAML）。"""
    wc = WorkingCopy(root=work)
    with pytest.raises(CreatorError):
        wc.update_world_field("era", ["不是", "字符串"])
    with pytest.raises(CreatorError):
        wc.update_world_field("core_rules", [1, 2, 3])
    with pytest.raises(CreatorError):
        wc.update_world_field("core_rules", 42)


# ---------------------------------------------------------------------------
# 3. 校验口径与发布同源
# ---------------------------------------------------------------------------


def test_validate_uses_the_same_gate_as_publish(work):
    """`validate()` 通过 ⟺ `catalog.can_publish` 放行。

    两个口径必须同源：否则会出现"Agent 说改好了、用户点发布被拒"，或者更糟的
    "Agent 说没过、其实发布没问题"（用户白改）。两条都走 `load_worldpack`。
    """
    from game_agent import catalog

    root = work.parent.parent  # 临时 world-packs/（草稿在 _drafts/probe）
    wc = WorkingCopy(root=work)
    ok, _ = wc.validate()
    assert ok, "真实包应当通过闸门"
    assert catalog.can_publish("probe", root) is None, "闸门与发布口径不一致"

    # 弄坏它 → 两边都不放行
    (work / "mainline.yaml").unlink()
    ok, text = wc.validate()
    assert not ok and "mainline.yaml" in text, "报错原文必须能拿到（那是喂模型的燃料）"
    reason = catalog.can_publish("probe", root)
    assert reason is not None and "mainline.yaml" in reason


def test_validate_returns_readable_text_for_a_broken_pack(work):
    """校验器自己抛非 WorldPackError 时也要变成可读文本，不能把异常漏给调用方。"""
    wc = WorkingCopy(root=work)
    (work / "world.yaml").write_text("name: [未闭合\n", encoding="utf-8")
    ok, text = wc.validate()
    assert not ok and text, "坏 YAML 也要有可读结论"
    assert isinstance(text, str)


# ---------------------------------------------------------------------------
# 4. 增删改的具体语义（含"改完还要做什么"）
# ---------------------------------------------------------------------------


def test_add_npc_needs_no_affection_but_affection_needs_a_card(work):
    """闸门只校验**一个方向**：`affections` 指向的角色卡必须存在。

    反过来（有卡、但 `affections` 里没登记）是**合法**的——那只是一个"没有好感数值
    的在场角色"。这条断言起初写反了（我以为不登记就会校验失败），实测才发现：
    `check_worldpack` 检的是 `好感对象 'x' 缺少对应角色卡 npcs/x.yaml`，
    没有反向要求。于是顺带修掉了 `add_npc` 返回文本里一句**误导模型**的话。

    这也是"工具返回文本必须准确"的守卫：给模型一条错的因果，它会去白做一件事，
    然后发现自己没做错什么——比不给提示更浪费时间。
    """
    wc = WorkingCopy(root=work)
    out = wc.add_npc("lin", "林医生", "城南诊所的医生", "话很少", "短句、不解释")
    assert "不是校验要求" in out, "工具返回里必须说清 affections 是玩法要求而非闸门要求"
    ok, text = wc.validate()
    assert ok, f"只加一张角色卡不该破坏校验，实际：{text}"

    # 真正会被闸门拒绝的方向：affections 指向一个不存在的角色卡
    sched = _yaml(work / "schedule.yaml")
    sched.setdefault("affections", {})["ghost"] = {"label": "幽灵", "initial": 0}
    (work / "schedule.yaml").write_text(
        yaml.safe_dump(sched, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ok, text = wc.validate()
    assert not ok, "affections 指向不存在的角色时必须被拒"
    assert "ghost" in text, f"报错要指名道姓，实际：{text[:160]}"
    assert "npcs/ghost.yaml" in text

    # 补上那张卡 → 通过（**修复循环闭环**：报错原文足以指导下一步）
    wc.add_npc("ghost", "幽灵", "不存在的存在", "沉默", "无言")
    ok, text = wc.validate()
    assert ok, f"补上角色卡后应当通过，实际：{text}"


def test_remove_npc_reports_dangling_references(work):
    """删角色会让引用悬空——校验必须抓出来（这正是闸门存在的理由）。

    `ancient_jianghu` 只有 1 个角色，删掉它只会撞上"npcs/ 目录下没有任何角色卡"
    这条更早的门（那也对，但测不到"悬空"这条路径）。所以先加一个角色，
    再删掉**被引用的那个**，才真的走到悬空检查。
    """
    wc = WorkingCopy(root=work)
    npc_id = wc.list_npcs()[0]["id"]
    wc.add_npc("lin", "林医生", "城南诊所的医生", "话很少", "短句")
    assert wc.validate()[0], "加了一个未被引用的角色后应当仍然通过"

    out = wc.remove_npc(npc_id)
    assert "validate_pack" in out
    ok, text = wc.validate()
    assert not ok, "删掉被引用的角色之后不该还能过闸门"
    assert npc_id in text, f"报错要指名道姓说是谁悬空，实际：{text[:160]}"


def test_upsert_lore_adds_then_updates(work):
    wc = WorkingCopy(root=work)
    before = len(wc.read_lore())
    out = wc.upsert_lore("lin_clinic", ["诊所", "林医生"], "城南诊所的设定正文。")
    assert "新增" in out
    assert len(wc.read_lore()) == before + 1
    # 同 id 再写 → 更新而不是重复
    out = wc.upsert_lore("lin_clinic", ["诊所"], "改过的正文。")
    assert "更新" in out
    lore = {x["id"]: x for x in wc.read_lore()}
    assert len(wc.read_lore()) == before + 1
    assert lore["lin_clinic"]["text"] == "改过的正文。"
    assert wc.validate()[0], "合法 lore 不该破坏校验"


def test_upsert_lore_rejects_empty_keys_and_empty_text(work):
    """空键/空正文必须当场拒绝——复用 `lore.validate_key`（"能校验的必能匹配"）。"""
    wc = WorkingCopy(root=work)
    before = (work / "world.yaml").read_bytes()
    with pytest.raises(CreatorError):
        wc.upsert_lore("x", [], "正文")
    with pytest.raises(CreatorError):
        wc.upsert_lore("x", ["键"], "   ")
    assert (work / "world.yaml").read_bytes() == before, "被拒的写入不该落盘"


# ---------------------------------------------------------------------------
# 5. diff：给用户确认用的那一面
# ---------------------------------------------------------------------------


def test_diff_is_empty_before_and_shows_each_change_after(work):
    wc = WorkingCopy(root=work)  # 基线在构造时自动取（见 diff_cannot_lie 那条）
    assert wc.diff() == "", "还没改就有 diff = 基线取错了"
    assert wc.changed_files() == []

    wc.update_world_field("era", "新朝代")
    d = wc.diff()
    assert "新朝代" in d and "world.yaml" in d
    assert wc.changed_files() == ["world.yaml"]

    npc_id = wc.list_npcs()[0]["id"]
    wc.update_npc_field(npc_id, "identity", "换了身份")
    # `changed_files()` 是**排序**的（字典序里 "npcs/…" 在 "world.yaml" 之前），
    # 这样调用方（UI/日志）拿到的顺序稳定、可比对
    assert wc.changed_files() == [f"npcs/{npc_id}.yaml", "world.yaml"]


def test_diff_cannot_lie_when_baseline_is_missing(work):
    """**守一个具体的坑**：基线为空时，`diff()` 会把每个存在的文件都算成"新增"，
    于是作者看到一份"改得面目全非"的 diff，而实际上什么都没改。

    修法是让这种状态**构造不出来**（`WorkingCopy.__post_init__` 取基线）。
    写这条守卫时正是踩了它：直接 `WorkingCopy(root=…)` 的测试全都看到 6 个文件"被改"。
    """
    wc = WorkingCopy(root=work)
    assert len(wc.baseline) >= len(PACK_CONTENT_FILES), "构造时就该取到基线"
    assert wc.diff() == "", "刚构造的工作版不该有任何 diff"
    assert wc.changed_files() == []


def test_diff_tool_is_available_to_the_model(work):
    """`diff_pack` 是工具面的一部分（§4.2）——模型要能主动看自己改了什么。"""
    wc = WorkingCopy(root=work)
    wc.baseline = wc.snapshot()
    reg = build_registry(wc)
    assert "diff_pack" in reg.names()
    assert "还没有任何改动" in reg.dispatch("diff_pack", {}).message
    wc.update_world_field("era", "改了")
    assert "改了" in reg.dispatch("diff_pack", {}).message


# ---------------------------------------------------------------------------
# 6. 循环：模型 ↔ 工具 ↔ 校验
# ---------------------------------------------------------------------------


def test_loop_applies_tool_calls_and_reports_validation(work):
    """一轮完整对话：读 → 改 → 校验 → 回话。改动真的落到工作版上。"""
    wc = WorkingCopy(root=work)
    s = CreatorSession(name="probe", wc=wc)
    s.messages = [{"role": "system", "content": "x"}]
    npc_id = wc.list_npcs()[0]["id"]

    events: list[dict] = []
    llm = _llm(
        resp(msg(tool_calls=[tool_call("c1", "update_npc_field",
                                       {"npc_id": npc_id, "field": "speech_style", "value": "只说短句。"})])),
        resp(msg(tool_calls=[tool_call("c2", "validate_pack", {})])),
        resp(msg(content="已把她的说话风格改成「只说短句。」，校验通过。")),
    )
    turn = s.send("把她改成话很少的人", llm, on_event=events.append)

    assert turn.validate_ok, turn.validate_text
    assert turn.reply == "已把她的说话风格改成「只说短句。」，校验通过。"
    assert [t["name"] for t in turn.tools_used] == ["update_npc_field", "validate_pack"]
    assert turn.changed == [f"npcs/{npc_id}.yaml"]
    assert _yaml(work / "npcs" / f"{npc_id}.yaml")["speech_style"] == "只说短句。"
    # 过程事件推给了调用方（前端靠它显示"正在做什么"）
    assert any(e["type"] == "tool" and e["name"] == "update_npc_field" for e in events)
    assert any(e["type"] == "text" for e in events)


def test_loop_feeds_validation_errors_back_to_the_model(work):
    """**修复循环的闭环**：模型改坏 → 校验拒绝 → 原文回灌 → 模型改对。

    这条是本能力的技术核心（§4.2："validate_pack 是整个设计的支点"）。
    断言不只是"最后成功了"，还要断言**模型真的看到了报错原文**——
    否则它可能是碰巧改对的，那这条链就没有意义。
    """
    wc = WorkingCopy(root=work)
    s = CreatorSession(name="probe", wc=wc)
    s.messages = [{"role": "system", "content": "x"}]

    llm = _llm(
        # 先改成一个注定不过的值：删掉角色会让引用悬空
        resp(msg(tool_calls=[tool_call("c1", "remove_npc",
                                       {"npc_id": wc.list_npcs()[0]["id"]})])),
        resp(msg(tool_calls=[tool_call("c2", "validate_pack", {})])),
        resp(msg(content="删掉之后有引用悬空，我先把它加回来。")),
    )
    turn = s.send("删掉那个角色", llm)
    assert not turn.validate_ok

    # 模型确实收到了报错原文（tool 消息里有校验器的话）
    tool_msgs = [m for m in s.messages if m.get("role") == "tool"]
    assert any("校验未通过" in m["content"] for m in tool_msgs), "报错没回灌给模型"
    assert any("悬空" in m["content"] or "不存在" in m["content"] for m in tool_msgs)
    # 而改动**没有**被静默通过：校验结论如实报给用户
    assert turn.reply
    assert turn.changed, "删了角色却没记下改动？"


def test_unknown_tool_does_not_break_the_loop(work):
    """模型调了一个不存在的工具 → 结构化拒绝文本，循环继续（不抛异常）。"""
    wc = WorkingCopy(root=work)
    s = CreatorSession(name="probe", wc=wc)
    s.messages = [{"role": "system", "content": "x"}]
    llm = _llm(
        resp(msg(tool_calls=[tool_call("c1", "delete_everything", {})])),
        resp(msg(content="没有那个工具，我改用 validate_pack。")),
    )
    turn = s.send("删掉一切", llm)
    assert turn.tools_used[0]["name"] == "delete_everything"
    assert turn.tools_used[0]["status"] == "unknown"
    assert "未知工具" in turn.tools_used[0]["result"]
    assert turn.reply


def test_bad_json_arguments_do_not_break_the_loop(work):
    """参数不是合法 JSON（真机上会被截断）→ 结构化拒绝，循环继续。"""
    wc = WorkingCopy(root=work)
    s = CreatorSession(name="probe", wc=wc)
    s.messages = [{"role": "system", "content": "x"}]
    llm = _llm(
        resp(msg(tool_calls=[tool_call("c1", "update_world_field", '{"field": "era", "val')])),
        resp(msg(content="参数写坏了，我重来。")),
    )
    turn = s.send("改时代", llm)
    assert turn.tools_used[0]["status"] == "rejected"
    assert "JSON" in turn.tools_used[0]["result"]


def test_rejected_tool_result_is_marked_not_silently_ok(work):
    """被白名单拒绝的调用，状态是 `rejected` 且原文可读——不能记成 ok。"""
    wc = WorkingCopy(root=work)
    s = CreatorSession(name="probe", wc=wc)
    s.messages = [{"role": "system", "content": "x"}]
    llm = _llm(
        resp(msg(tool_calls=[tool_call("c1", "update_world_field",
                                       {"field": "max_recursion", "value": 3})])),
        resp(msg(content="这个字段不能改。")),
    )
    turn = s.send("把递归上限改成 3", llm)
    rec = turn.tools_used[0]
    assert rec["status"] == "rejected" and "不可改" in rec["result"]


def test_iteration_cap_stops_and_reports_truncation(work):
    """达到迭代上限：停下来、如实标记 `truncated`，而不是无限烧钱或假装做完。"""
    wc = WorkingCopy(root=work)
    s = CreatorSession(name="probe", wc=wc)
    s.messages = [{"role": "system", "content": "x"}]
    # 每次都调工具，永不收尾 → 必须在 MAX_CREATOR_ITERS 处停
    looping = [
        resp(msg(tool_calls=[tool_call(f"c{i}", "read_world", {})]))
        for i in range(MAX_CREATOR_ITERS + 2)
    ]
    llm = _llm(*looping)
    turn = s.send("一直读世界", llm)
    assert turn.truncated is True, "超限必须如实标记"
    assert len(turn.tools_used) == MAX_CREATOR_ITERS - 1, (
        f"工具调用次数应受上限约束，实际 {len(turn.tools_used)}"
    )


def test_history_excludes_system_and_tool_messages(work):
    """给前端的对话记录只留人话（system 提示与 tool 结果不外泄给用户看）。"""
    wc = WorkingCopy(root=work)
    s = CreatorSession(name="probe", wc=wc)
    s.messages = [{"role": "system", "content": "x"}]
    llm = _llm(
        resp(msg(tool_calls=[tool_call("c1", "read_world", {})])),
        resp(msg(content="读完了。")),
    )
    s.send("看看世界", llm)
    hist = s.history()
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[0]["content"] == "看看世界"
    assert hist[1]["content"] == "读完了。"


def test_session_open_captures_baseline(work):
    """会话打开时取基线，于是"Agent 改了什么"从第一轮起就看得见。"""
    s = CreatorSession.open(work, "probe")
    assert s.wc.baseline, "基线是空的 → diff 会把整份文件都算成新增"
    assert s.wc.diff() == ""
    s.wc.update_world_field("era", "改一下")
    assert "world.yaml" in s.wc.diff()


# ---------------------------------------------------------------------------
# 7. N6 的前置：记账要有 pack 归因轴
# ---------------------------------------------------------------------------


def test_usage_tracker_carries_the_pack_axis(tmp_path):
    """创作者 Agent 的调用必须能回答"**改这一版花了多少钱**"。

    这是 roadmap 明确列在 N6 开工项里的前置（不是 N5 的事）：G-6 的教训是
    **事后补轴要重造历史数据**。所以这里直接钉住"包里带 pack 字段"。
    """
    p = tmp_path / "usage-creator-probe.jsonl"
    t = UsageTracker(p, session="cs1", pack="probe")
    t.record("m", "creator", {"prompt_tokens": 10, "completion_tokens": 2})
    entry = t.entries[0]
    assert entry["pack"] == "probe"
    assert entry["session"] == "cs1"
    assert entry["purpose"] == "creator"
    on_disk = yaml.safe_load  # noqa: F841 — 占位避免误用；下面用 json 读
    import json

    assert json.loads(p.read_text(encoding="utf-8").strip())["pack"] == "probe"

    # 不带 pack 时不该凭空多出这个键（否则历史账本口径会被污染）
    t2 = UsageTracker(tmp_path / "u2.jsonl", session="s")
    t2.record("m", "turn")
    assert "pack" not in t2.entries[0]


# ---------------------------------------------------------------------------
# 8. Web 端点：对话（SSE）、会话现状、重置、fork
# ---------------------------------------------------------------------------


def _sse_frames(text: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体切成 (event, payload)。"""
    import json as _json

    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if data:
            out.append((event, _json.loads(data)))
    return out


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """装配一个隔离的 Web 客户端：一个草稿 + 假 Settings + 假 LLM。"""
    from fastapi.testclient import TestClient

    import game_agent.web as web

    root = tmp_path / "world-packs"
    root.mkdir(parents=True)
    shutil.copytree(REAL_PACK, root / "published")          # 已发布（fork 的源）
    shutil.copytree(REAL_PACK, catalog.draft_dir("draft_one", root))
    monkeypatch.setattr(web, "_pack_root", lambda: root)
    monkeypatch.setattr(web, "CREATORS", {})
    # 不依赖本机 .env 里有没有 Key
    monkeypatch.setattr(web, "load_settings", lambda: SimpleNamespace(has_api_key=True))
    return TestClient(web.app), root


def _script(monkeypatch, *responses):
    """把创作者用的 LLM 换成脚本化的假客户端。"""
    import game_agent.creator as creator_mod

    monkeypatch.setattr(creator_mod, "build_creator_llm",
                        lambda settings, tracker=None: _llm(*responses))


def test_creator_state_before_any_session(web_client):
    client, _ = web_client
    d = client.get("/api/creator/draft_one").json()
    assert d["ok"] is True and d["open"] is False
    assert d["messages"] == [] and d["diff"] == ""
    assert "世界" in d["summary"], "未开会话也要能给出工作版摘要（前端要显示）"


def test_creator_rejects_unknown_draft(web_client):
    """创作 Agent **只改草稿**：不存在的名字 400，且已发布包不能走这条路。

    最后一条是关键：`published` 是已发布包，不在草稿区——如果这里放行了，
    Agent 就会直接改线上内容，`_drafts/` 那套隔离等于没有。
    """
    client, _ = web_client
    assert client.get("/api/creator/no_such").status_code == 400
    r = client.post("/api/creator/published/chat", json={"message": "改一下"})
    assert r.status_code == 400
    assert "草稿" in r.json()["detail"]


def test_creator_chat_streams_tools_and_done(web_client, monkeypatch):
    """**端到端**：一句话 → 工具调用事件 → 最终答复 + 校验结论 + diff。"""
    client, root = web_client
    npc = next((root / "_drafts" / "draft_one" / "npcs").glob("*.yaml")).stem
    _script(
        monkeypatch,
        resp(msg(tool_calls=[tool_call("c1", "update_npc_field",
                                       {"npc_id": npc, "field": "personality",
                                        "value": "外冷内热，说话极简。"})])),
        resp(msg(tool_calls=[tool_call("c2", "validate_pack", {})])),
        resp(msg(content="已把她的性格改成外冷内热，校验通过。")),
    )

    r = client.post("/api/creator/draft_one/chat", json={"message": "让她外冷内热一点"})
    assert r.status_code == 200
    frames = _sse_frames(r.text)
    kinds = [k for k, _ in frames]
    assert kinds[0] == "start"
    assert "tool" in kinds, f"没有工具过程事件：{kinds}"
    assert kinds[-1] == "done"

    done = [p for k, p in frames if k == "done"][0]
    assert done["validate_ok"] is True, done["validate_text"]
    assert done["changed"] == [f"npcs/{npc}.yaml"]
    assert "外冷内热" in done["diff"], "done 要带 diff（用户靠它确认改了什么）"
    assert [t["name"] for t in done["tools"]] == ["update_npc_field", "validate_pack"]
    assert "校验通过" in done["reply"]

    # 改动真的落到草稿上
    assert _yaml(root / "_drafts" / "draft_one" / "npcs" / f"{npc}.yaml")["personality"] \
        == "外冷内热，说话极简。"


def test_creator_state_reflects_the_session_afterwards(web_client, monkeypatch):
    """刷新页面要能接上：对话记录 + diff + 校验结论都在。"""
    client, _ = web_client
    _script(monkeypatch, resp(msg(content="先不改，先问你一句：想强化哪一面？")))
    client.post("/api/creator/draft_one/chat", json={"message": "帮我看看这个世界"})

    d = client.get("/api/creator/draft_one").json()
    assert d["open"] is True
    assert [m["role"] for m in d["messages"]] == ["user", "assistant"]
    assert d["messages"][0]["content"] == "帮我看看这个世界"
    assert d["validate_ok"] is True
    assert d["turns"] == 1
    assert "基线" in d["baseline_note"], "要如实说明 diff 的口径（会话以来，不是持久差异）"


def test_creator_reset_clears_context_but_not_content(web_client, monkeypatch):
    """重置只丢对话上下文，**不动草稿内容**——这两个动作必须分开。"""
    client, root = web_client
    npc = next((root / "_drafts" / "draft_one" / "npcs").glob("*.yaml")).stem
    _script(
        monkeypatch,
        resp(msg(tool_calls=[tool_call("c1", "update_npc_field",
                                       {"npc_id": npc, "field": "identity",
                                        "value": "改过的身份"})])),
        resp(msg(content="改好了。")),
    )
    client.post("/api/creator/draft_one/chat", json={"message": "改身份"})
    assert client.get("/api/creator/draft_one").json()["messages"]

    d = client.delete("/api/creator/draft_one").json()
    assert d["ok"] is True
    assert client.get("/api/creator/draft_one").json()["messages"] == []
    # 内容还在
    assert _yaml(root / "_drafts" / "draft_one" / "npcs" / f"{npc}.yaml")["identity"] \
        == "改过的身份"


def test_creator_chat_rejects_empty_message(web_client):
    client, _ = web_client
    assert client.post("/api/creator/draft_one/chat", json={"message": "   "}).status_code == 400


def test_creator_usage_tracker_carries_the_pack_axis(web_client, monkeypatch):
    """**接线契约**：创作端点造的 tracker 必须带 `pack` 轴与独立账本。

    与上面的 `UsageTracker` 单测分工：那条测"能力存在"，这条测"创作端点真的用上了"
    ——否则轴加了却没接，等于没加（G-6 的教训正是"接口存在但没人用对"）。
    """
    import game_agent.web as web

    seen: list[dict] = []
    real = web.UsageTracker

    def spy(path, session=None, pack=None):
        seen.append({"path": str(path), "session": session, "pack": pack})
        return real(path, session=session, pack=pack)

    monkeypatch.setattr(web, "UsageTracker", spy)
    _script(monkeypatch, resp(msg(content="好。")))
    client, _ = web_client
    client.post("/api/creator/draft_one/chat", json={"message": "在吗"})

    assert seen, "创作端点没有造 tracker —— 记账轴等于没接"
    assert seen[0]["pack"] == "draft_one"
    assert "usage-creator-draft_one" in seen[0]["path"]


def test_fork_copies_published_into_drafts(web_client):
    """fork：把现成的卡拿来改。**是复制不是移动**（已发布区原地不动）。"""
    client, root = web_client
    before = (root / "published" / "world.yaml").read_bytes()

    d = client.post("/api/packs/fork", json={"name": "published"}).json()
    assert d["ok"] is True and d["draft"]["draft"] is True
    assert (root / "_drafts" / "published" / "world.yaml").is_file()
    assert (root / "published" / "world.yaml").read_bytes() == before, "已发布内容被动了！"

    # 草稿立刻可用（可校验、可被 Agent 改）
    assert client.get("/api/creator/published").json()["open"] is False
    assert "世界" in client.get("/api/creator/published").json()["summary"]


def test_fork_refuses_when_draft_exists_or_pack_missing(web_client):
    client, _ = web_client
    assert client.post("/api/packs/fork", json={"name": "draft_one"}).status_code == 400
    r = client.post("/api/packs/fork", json={"name": "no_such_pack"})
    assert r.status_code == 400 and "没有这个已发布" in r.json()["detail"]


def test_forked_draft_can_be_edited_by_the_agent(web_client, monkeypatch):
    """fork 之后 Agent 能改它——这条把"改一张现成的卡"整条路走通。"""
    client, root = web_client
    client.post("/api/packs/fork", json={"name": "published"})
    _script(
        monkeypatch,
        resp(msg(tool_calls=[tool_call("c1", "upsert_lore",
                                       {"id": "probe_lore", "keys": ["探针"],
                                        "text": "这是一条新加的世界书设定。"})])),
        resp(msg(tool_calls=[tool_call("c2", "validate_pack", {})])),
        resp(msg(content="加了一条世界书设定，校验通过。")),
    )
    r = client.post("/api/creator/published/chat", json={"message": "加一条设定"})
    done = [p for k, p in _sse_frames(r.text) if k == "done"][0]
    assert done["validate_ok"] is True, done["validate_text"]
    assert done["changed"] == ["world.yaml"]
    lore = _yaml(root / "_drafts" / "published" / "world.yaml")["lore"]
    assert any(x["id"] == "probe_lore" for x in lore)


# ---------------------------------------------------------------------------
# 9. 对话式做卡：start_generation 工具（2026-10 补）
# ---------------------------------------------------------------------------


def test_start_generation_tool_is_disabled_when_not_wired(work):
    """**没接入生成管线时必须明说"不能做"，而不是让模型以为它能做。**

    这条挡的是最坏的一种失败：模型凭空答应"好的我这就做一张卡"，然后什么也没发生
    ——用户等一个永远不来的结果。`handler=None` + `disabled_msg` 是注册表的既有机制
    （`remember` / `query_world` 同款），所以拒绝是结构化的、模型看得到原因。
    """
    wc = WorkingCopy(root=work)
    reg = build_registry(wc)  # 不注入 start_generation
    res = reg.dispatch("start_generation", {"name": "x", "source_text": "素材"})
    assert res.status == "protocol_error"
    assert "没有接入生成管线" in res.message
    assert "不要" in res.message and "假装" in res.message, "要明确禁止假装能做"


def test_start_generation_tool_delegates_instead_of_running(work):
    """**接入后是"转交"不是"代跑"**：工具立刻返回，返回值里必须写明别等。

    为什么这条重要：生成是分钟级任务。若这个工具体内同步跑，一轮对话会被挂死几分钟，
    而且用户看不到任何进度——那就退化成了"点一次按钮然后发呆"。
    """
    wc = WorkingCopy(root=work)
    calls: list[tuple] = []

    def fake_start(name, text, offline):
        calls.append((name, text, offline))
        return f"已起任务 job_x（{name}）"

    reg = build_registry(wc, start_generation=fake_start)
    out = reg.dispatch("start_generation",
                       {"name": "new_card", "source_text": "一段素材", "offline": True})
    assert out.status == "ok", out.message
    assert calls == [("new_card", "一段素材", True)]

    # 参数校验必须在工具层（别把空素材丢给生成管线白烧一次）
    assert reg.dispatch("start_generation", {"name": "", "source_text": "x"}).status == "rejected"
    assert reg.dispatch("start_generation",
                        {"name": "x", "source_text": "   "}).status == "rejected"


def test_start_generation_is_offered_to_the_model_when_wired(work):
    """接入后工具**出现在 schema 里**（否则模型根本不知道有这条路）。"""
    wc = WorkingCopy(root=work)
    names = [t["function"]["name"] for t in
             build_registry(wc, start_generation=lambda a, b, c: "ok").schemas()]
    assert "start_generation" in names
    # 未接入时**也注册**（带 disabled_msg）——这样模型会被告知"不能"，而不是猜
    names2 = [t["function"]["name"] for t in build_registry(wc).schemas()]
    assert "start_generation" in names2


def test_chat_can_start_a_generation_and_emits_a_job_event(web_client, monkeypatch):
    """端到端：对话里让 Agent 从素材做新卡 → 起任务 → 推 `job` 事件给前端。

    前端靠这个事件**自动切到进度页签**，所以它必须在流里出现——
    只回一句"任务起了"而不推事件，作者就得自己去找进度在哪。
    """
    client, root = web_client
    _script(
        monkeypatch,
        resp(msg(tool_calls=[tool_call("c1", "start_generation",
                                       {"name": "brand_new", "source_text": "# 大纲\n\n某人醒来。",
                                        "offline": True})])),
        resp(msg(content="已经起了一张新卡的任务，进度在「从素材生成」页签。")),
    )
    r = client.post("/api/creator/draft_one/chat",
                    json={"message": "把这段大纲做成一张卡：# 大纲 某人醒来。"})
    frames = _sse_frames(r.text)
    kinds = [k for k, _ in frames]
    assert "job" in kinds, f"没有 job 事件，前端不会切到进度页签：{kinds}"

    job = [p for k, p in frames if k == "job"][0]
    assert job["pack_name"] == "brand_new"
    assert job["job_id"], "要带任务号（前端靠它订阅进度）"
    # 任务真的在任务表里，而且写的是**草稿区**
    from tests.test_jobs import _wait

    import game_agent.web as web

    j = web.JOBS.get(job["job_id"])
    assert j is not None, "只推了事件、没真起任务"
    assert str(j.pack_dir).endswith(str(Path("_drafts") / "brand_new"))
    # ⚠️ 这条断言是**钱**的守卫：工具默认 `offline=false`（对话式做卡的意图是"真做一张"），
    # 所以测试**必须**显式传 offline=True。第一版忘了传，结果测试跑了 60 秒真机生成、
    # 往 saves/usage-import.jsonl 追了 62 条真实调用记录（已回退）。
    assert j.offline is True, "测试里不许触发真机生成（那是要花钱的）"
    assert _wait(j).status in ("done", "failed")  # 别留悬挂任务


def test_chat_refuses_to_start_generation_shadowing_published(web_client, monkeypatch):
    """对话式做卡**同样**不许遮蔽已发布的包（与 HTTP 端点同一条护栏）。"""
    client, _ = web_client
    _script(
        monkeypatch,
        resp(msg(tool_calls=[tool_call("c1", "start_generation",
                                       {"name": "published", "source_text": "素材"})])),
        resp(msg(content="这个名字已经有已发布的包了，换个名字。")),
    )
    r = client.post("/api/creator/draft_one/chat", json={"message": "就叫 published"})
    tools = [p for k, p in _sse_frames(r.text) if k == "tool"]
    assert tools and tools[0]["name"] == "start_generation"
    assert tools[0]["status"] == "rejected", tools[0]
    assert "已发布" in tools[0]["result"]

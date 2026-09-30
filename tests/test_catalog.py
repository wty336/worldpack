"""E-3/E-4 守卫：目录层（选一张卡）与自由/剧本模式。

上游：`docs/plan-tavern-shaped-product.md` §2（能力 A）。

**被钉住的缺口**：`world-packs/` 早就是目录，`load_worldpack` 也能对任意目录全量加载
并做完交叉校验、返回富元数据；唯一挡住"玩家选一张卡"的是一个**进程级环境变量**
`GAME_WORLDPACK`（一个进程 = 一个包，换包要重启）。玩家没有任何选卡入口。

本文件守四件事：
1. **目录层**：能枚举、坏包隔离（单包失败不拖垮列表）、默认包兜底不硬编码；
2. **路径安全**：`pack_id` 只能查表命中，永不参与路径拼接（`../` 必须无效）；
3. **模式语义**：自由模式不进入主线节点，但不成"清空进度"（已进节点不动）；
4. **接口接线**：`/api/catalog`、`/api/new{pack_id,mode}`、`/api/{sid}/meta`、
   `/api/sessions`、`/api/saves` 的契约与错误码。
5. **`world-packs/` 的遍历者登记在案**（§5）——草稿区落地后新出现的一类坑，
   详见 `test_world_packs_enumerators_all_discriminate`。
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fakes import FakeClient, msg, resp, tool_call

import game_agent.web as web
from game_agent.catalog import (
    PackEntry,
    default_pack_id,
    list_packs,
    resolve_pack,
)
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.storyline import StorylineEngine
from game_agent.worldpack import load_worldpack

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


# ---------------------------------------------------------------------------
# 1. 目录层
# ---------------------------------------------------------------------------


def test_list_real_catalog_includes_all_packs():
    """目录里 8 个包必须全部列出且可玩（`check-worldpack` 已保证它们合法）。"""
    entries = list_packs(REPO / "world-packs")
    ids = {e.id for e in entries}
    for expected in ("ancient_jianghu", "campus_otome", "xianxia_wendao", "urban_neon"):
        assert expected in ids, f"目录层漏了 {expected}"
    assert all(e.playable for e in entries), [e.id for e in entries if not e.playable]


def test_entry_carries_card_metadata():
    """卡片要看的是"世界名/时代/几个角色/几个结局"，不是把整包塞给前端。"""
    entries = {e.id: e for e in list_packs(REPO / "world-packs")}
    a = entries["ancient_jianghu"]
    assert a.name == "江湖旧梦"
    assert a.era and a.npcs >= 1 and a.nodes >= 1 and a.endings >= 1
    assert len(a.digest) == 16  # 与存档身份戳同源
    assert a.path.endswith("ancient_jianghu")


def test_broken_pack_is_isolated_not_fatal(tmp_path):
    """**单包失败不拖垮目录**：坏包以 error 呈现，好包照常列出。

    目录里有一个改到一半的包（YAML 缩进错）是常态；整个选卡界面白屏是不可接受的。
    """
    root = tmp_path / "world-packs"
    shutil.copytree(REAL_PACK, root / "good")
    (root / "broken").mkdir()
    (root / "broken" / "world.yaml").write_text("name: 坏包\n", encoding="utf-8")  # 缺文件
    (root / "not_a_pack").mkdir()  # 没有 world.yaml → 不是卡，也不该算坏卡

    entries = {e.id: e for e in list_packs(root)}
    assert set(entries) == {"good", "broken"}
    assert entries["good"].playable
    assert not entries["broken"].playable
    assert entries["broken"].error  # 原因要能显示给玩家
    assert [e.id for e in list_packs(root, playable_only=True)] == ["good"]


def test_default_pack_id_prefers_example_then_first(tmp_path):
    """兜底不硬编码某个包：包被移走时退到"还有什么能玩"，而不是启动即 500。"""
    root = tmp_path / "world-packs"
    shutil.copytree(REAL_PACK, root / "only_pack")
    assert default_pack_id(root) == "only_pack"  # 没有 ancient_jianghu 时用第一个可玩的
    assert default_pack_id(tmp_path / "nope") is None  # 目录都不存在 → 明确的 None

    real = REPO / "world-packs"
    assert default_pack_id(real) == "ancient_jianghu"  # 示例包在库时优先它


def test_resolve_pack_rejects_path_traversal(tmp_path):
    """**路径安全**：pack_id 只能查表命中，`../` 之类必须无效。

    与 `_safe_save_path` 同一条教训——客户端可控的字符串直传文件 API 是路径穿越洞。
    实现上不拼路径，所以这些输入天然不可能生效；本测试把这条性质钉住。
    """
    root = REPO / "world-packs"
    assert resolve_pack("ancient_jianghu", root) is not None
    for evil in (
        "..",
        "../..",
        "../../etc",
        "ancient_jianghu/../..",
        "world-packs/ancient_jianghu",  # 带分隔符一律拒绝
        "ancient_jianghu/world.yaml",
        "C:\\Windows",
        "/etc/passwd",
        "",
        None,
        "nope_not_here",
    ):
        assert resolve_pack(evil, root) is None, f"不该接受 {evil!r}"


def test_resolve_pack_rejects_unknown_id():
    assert resolve_pack("does_not_exist", REPO / "world-packs") is None


def test_pack_entry_to_dict_shape():
    """接口形状稳定（前端直接渲染这个 dict）。"""
    d = PackEntry(id="x", name="世界", npcs=2).to_dict()
    assert d["playable"] is True and d["error"] == ""
    assert {"id", "name", "era", "npcs", "nodes", "endings", "digest", "path"} <= set(d)
    broken = PackEntry(id="y", error="缺文件").to_dict()
    assert broken["playable"] is False and broken["name"] == "y"  # 名字缺省回退 id


# ---------------------------------------------------------------------------
# 1b. 缓存：必须省时间，但**绝不能返回过期内容**
# ---------------------------------------------------------------------------


def test_cache_returns_fresh_content_after_edit(tmp_path):
    """**缓存失效的核心断言**：改了包内容，下一次列举必须看到新内容。

    这是本模块最容易出错的地方——缓存一旦不失效，玩家会选到一张"元数据与实际
    内容不符"的卡（作者刚改完却看到旧名字），而且**不会报错**。
    只看包目录 mtime 的实现会在这里失败：修改已存在的文件不改父目录 mtime。
    """
    root = tmp_path / "world-packs"
    shutil.copytree(REAL_PACK, root / "p")
    first = list_packs(root, use_cache=True)
    assert first[0].name == "江湖旧梦"

    world = root / "p" / "world.yaml"
    world.write_text(
        world.read_text(encoding="utf-8").replace("江湖旧梦", "改过的世界名"),
        encoding="utf-8",
    )
    again = list_packs(root, use_cache=True)
    assert again[0].name == "改过的世界名", "缓存没失效——返回了过期元数据"
    assert again[0].digest != first[0].digest


def test_cache_hit_is_not_shared_mutable_state(tmp_path):
    """缓存命中返回的列表必须是副本：调用方就地排序/清空不得污染缓存。"""
    root = tmp_path / "world-packs"
    shutil.copytree(REAL_PACK, root / "p")
    list_packs(root)  # 填充缓存
    a = list_packs(root)
    a.clear()
    assert len(list_packs(root)) == 1  # 缓存未被清空


def test_cache_isolated_per_root(tmp_path):
    """不同根目录各自成键：测试造大量 tmp 目录时不能互相串味。"""
    r1, r2 = tmp_path / "a", tmp_path / "b"
    shutil.copytree(REAL_PACK, r1 / "p1")
    shutil.copytree(REAL_PACK, r2 / "p2")
    assert [e.id for e in list_packs(r1)] == ["p1"]
    assert [e.id for e in list_packs(r2)] == ["p2"]


# ---------------------------------------------------------------------------
# 2. 模式语义（E-4）
# ---------------------------------------------------------------------------


def _engine(mainline_enabled: bool) -> tuple[StorylineEngine, GameState]:
    pack = load_worldpack(REAL_PACK)
    state = GameState.from_pack(pack)
    return StorylineEngine(pack, StatsSystem(pack.schedule), mainline_enabled=mainline_enabled), state


def test_story_mode_enters_first_node():
    """剧本模式：`when: {all: []}`（恒真）的开场节点立即进入并挂上关键抉择。"""
    engine, state = _engine(True)
    node, msgs = engine.begin_turn(state)
    assert node is not None and node.id == "n1_first_meeting"
    assert state.current_node == "n1_first_meeting"
    assert msgs and "主线节点" in msgs[0]["content"]
    assert engine.choice_locked(state)  # 关键抉择待决 → 引擎锁定输入


def test_free_mode_does_not_enter_nodes():
    """**自由游玩的核心断言**：不进入任何主线节点，也不锁定输入。"""
    engine, state = _engine(False)
    node, msgs = engine.begin_turn(state)
    assert node is None and msgs == []
    assert state.current_node is None
    assert not engine.choice_locked(state)
    assert engine.pending_choice(state) is None


def test_free_mode_does_not_clear_existing_progress():
    """自由模式**不是"清空进度"**：已进入的节点状态保持原样。

    玩家想切换的是"要不要被主线牵着走"，不是"把已经发生的剧情擦掉"——
    抹掉进度会让"先自由探索、之后再跟主线"变成不可能。
    """
    pack = load_worldpack(REAL_PACK)
    state = GameState.from_pack(pack)
    state.current_node = "n1_first_meeting"  # 模拟"从剧本模式带过来的存档"
    engine = StorylineEngine(pack, StatsSystem(pack.schedule), mainline_enabled=False)
    assert engine.active_node(state) is not None  # 进度还在
    assert not engine.choice_locked(state)  # 但不再把玩家锁在固定选项上
    # 自由模式下再走回合也不会推进到下一个节点
    node, _ = engine.begin_turn(state)
    assert node is None
    assert state.current_node == "n1_first_meeting"  # 未被改写也未被清空


def test_game_wires_mode_into_storyline():
    """`Game(mainline_enabled=False)` 必须真的传到 StorylineEngine（接线守卫）。"""
    pack = load_worldpack(REAL_PACK)
    llm = LLMClient(FakeClient([]), "fake", build_tools(pack.schedule))
    g = Game(pack, GameState.from_pack(pack), llm, mainline_enabled=False)
    assert g.mainline_enabled is False
    assert g.story.mainline_enabled is False
    assert g.story.begin_turn(g.state)[0] is None  # 开局不进节点

    g2 = Game(pack, GameState.from_pack(pack), llm)  # 缺省仍是剧本模式
    assert g2.mainline_enabled is True
    assert g2.story.begin_turn(g2.state)[0] is not None


# ---------------------------------------------------------------------------
# 3. 接口接线
# ---------------------------------------------------------------------------


def _submit(narration: str, choices: list[str]):
    return tool_call("s1", "submit_narration",
                     {"narration": narration, "choices": choices, "plot_signal": "normal"})


def _client(monkeypatch, seen: list | None = None) -> TestClient:
    """装配一个不依赖 API Key 的 Web 客户端；`seen` 记录 _make_game 的调用参数。"""
    pack = load_worldpack(REAL_PACK)

    def fake_make_game(sid: str, pack_id=None, *, draft=None, mainline_enabled=True):
        """签名必须与 `web._make_game` 一致（E-3/E-4/E-7 起多了选包、模式与草稿三个入参）。

        `pack_id` / `draft` / `mainline_enabled` 在这里**刻意不生效**：本文件的守卫关心的是
        视图契约与前端结构分流，不是选包/模式/草稿语义（那些由 `test_catalog.py` 的
        目录层守卫与 `test_jobs.py` 的任务守卫负责）。
        """
        if seen is not None:
            seen.append({"pack_id": pack_id, "draft": draft,
                         "mainline_enabled": mainline_enabled})
        llm = LLMClient(FakeClient([resp(msg(tool_calls=[_submit("开场。", ["甲", "乙", "丙"])]))]),
                        "fake", build_tools(pack.schedule))
        return (
            Game(pack, GameState.from_pack(pack), llm, mainline_enabled=mainline_enabled),
            web.UsageTracker("saves/usage-test-only.jsonl", session=sid),
        )

    monkeypatch.setattr(web, "_make_game", fake_make_game)
    return TestClient(web.app)


def test_catalog_endpoint_lists_packs():
    client = TestClient(web.app)
    d = client.get("/api/catalog").json()
    assert d["ok"] is True
    ids = [p["id"] for p in d["packs"]]
    assert "ancient_jianghu" in ids
    assert d["default"] in ids
    assert all("playable" in p for p in d["packs"])


def test_new_passes_pack_and_mode_through(monkeypatch):
    """选卡与选模式必须真的传到 `_make_game`（而不是被接口吞掉）。"""
    seen: list = []
    client = _client(monkeypatch, seen)

    d = client.post("/api/new", json={"pack_id": "campus_otome", "mode": "free"}).json()
    assert seen[-1] == {"pack_id": "campus_otome", "draft": None, "mainline_enabled": False}
    assert d["pack_id"] == "ancient_jianghu"  # 假 _make_game 固定用这个包
    assert d["mode"] == "free"

    client.post("/api/new", json={"pack_id": None, "mode": "story"})
    assert seen[-1] == {"pack_id": None, "draft": None, "mainline_enabled": True}


def test_new_without_body_still_works(monkeypatch):
    """**向后兼容**：旧前端/旧测试不带请求体，必须照常开局（缺省 = 剧本模式）。"""
    seen: list = []
    client = _client(monkeypatch, seen)
    d = client.post("/api/new").json()
    assert d["sid"] and d["mode"] == "story"
    assert seen[-1] == {"pack_id": None, "draft": None, "mainline_enabled": True}


def test_new_rejects_unknown_pack(monkeypatch):
    """不认识的 pack_id → 400 且**不做"猜一个相近的"兜底**（静默选错卡更糟）。"""
    _client(monkeypatch)
    r = TestClient(web.app).post("/api/new", json={"pack_id": "no_such_pack"})
    assert r.status_code == 400
    assert "未知的世界包" in r.json()["detail"]


def test_make_game_signature_is_pinned():
    """`web._make_game` 的签名变了 → 所有测试假件都必须跟着改。

    为什么值得一条专门的守卫：接口是**按关键字**把新参数传下来的，假件少一个参数
    不会"忽略它"，而是 `TypeError` → 500。本仓库已经因此红过一次——加 `draft` 时
    `test_web_frontend` 的假件没跟上，症状是五个**与草稿毫无关系**的前端测试报
    "读取状态失败"。这条守卫把那种漂移换成一句指名道姓的失败。

    改动方法：更新本断言，并同步 `test_catalog._client`、`test_web_frontend._client`
    （以及任何新的假件）。
    """
    import inspect

    params = inspect.signature(web._make_game).parameters
    assert list(params) == ["sid", "pack_id", "draft", "mainline_enabled"]
    assert params["pack_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params["draft"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["mainline_enabled"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["mainline_enabled"].default is True


def test_new_rejects_unknown_mode(monkeypatch):
    """未知 mode → 400。静默当 story 会让"模式开关没反应"这类问题无从排查。"""
    _client(monkeypatch)
    r = TestClient(web.app).post("/api/new", json={"mode": "sandbox"})
    assert r.status_code == 400
    assert "未知的游玩模式" in r.json()["detail"]


def test_meta_reports_binding(monkeypatch):
    """刷新页面后前端要能重新对齐"在玩哪张卡 / 什么模式"。"""
    _client(monkeypatch)
    client = TestClient(web.app)
    sid = client.post("/api/new", json={"mode": "free"}).json()["sid"]
    m = client.get(f"/api/{sid}/meta").json()
    assert m["pack_id"] == "ancient_jianghu"
    assert m["mode"] == "free"
    assert m["name"] == "江湖旧梦"
    assert "turn" in m and "day" in m


def test_sessions_endpoint_lists_live_sessions(monkeypatch):
    """E-9：会话列表。pack_id/mode 由 game 推出（不另存一份，避免分叉）。"""
    web.SESSIONS.clear()
    _client(monkeypatch)
    client = TestClient(web.app)
    sid = client.post("/api/new", json={"mode": "free"}).json()["sid"]
    d = client.get("/api/sessions").json()
    row = next(s for s in d["sessions"] if s["sid"] == sid)
    assert row["pack_id"] == "ancient_jianghu"
    assert row["mode"] == "free"
    assert row["name"] == "江湖旧梦"


def test_session_derives_pack_and_mode_from_game(monkeypatch):
    """派生而非复制：改 game 的状态，列表与 meta 必须跟着变（无第二份真值）。"""
    web.SESSIONS.clear()
    _client(monkeypatch)
    client = TestClient(web.app)
    sid = client.post("/api/new", json={"mode": "story"}).json()["sid"]
    assert client.get(f"/api/{sid}/meta").json()["mode"] == "story"
    web.SESSIONS[sid].game.mainline_enabled = False  # 只改 game
    assert client.get(f"/api/{sid}/meta").json()["mode"] == "free"
    assert client.get("/api/sessions").json()["sessions"][0]["mode"] == "free"


def test_saves_endpoint_lists_summaries(tmp_path, monkeypatch):
    """E-9：存档列表只报顶层摘要（不反序列化整局），坏档不拖垮列表。"""
    from game_agent.save import save_game

    root = tmp_path / "saves"
    root.mkdir()
    save_game(GameState(pack_name="甲"), root / "a.json",
              pack_meta={"id": "p1", "digest": "d" * 16})
    (root / "broken.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(web, "SAVE_ROOT", root)

    d = TestClient(web.app).get("/api/saves").json()
    by = {r["path"]: r for r in d["saves"]}
    assert by["a.json"]["pack"] == {"id": "p1", "digest": "d" * 16}
    assert by["a.json"]["turn_count"] == 0
    assert "error" in by["broken.json"]  # 坏档以 error 呈现，不抛
    # mtime 倒序：只比较有 mtime 的条目（坏档没有，会被 .get(...,0) 排到最后）
    stamps = [r["mtime"] for r in d["saves"] if "mtime" in r]
    assert stamps == sorted(stamps, reverse=True)
    assert d["saves"][-1]["path"] == "broken.json"  # 无 mtime 的坏档落到末尾


# ---------------------------------------------------------------------------
# 4. 读档被拒（2026-10 实测：异包旧档 → 状态栏 KeyError → HTTP 500）
# ---------------------------------------------------------------------------


def _ancient_session(monkeypatch) -> tuple[TestClient, str]:
    """开一局 ancient_jianghu（离线夹具），返回 (client, sid)。"""
    _client(monkeypatch)
    client = TestClient(web.app)
    sid = client.post("/api/new", json={"pack_id": "ancient_jianghu"}).json()["sid"]
    return client, sid


def test_load_cross_pack_legacy_save_is_400_not_500(monkeypatch, tmp_path):
    """**实测缺陷的守卫**：异包旧档必须被**拒绝**（400 + 可行动的理由），而不是 500。

    复现路径：选《江湖旧梦》开局 → 去读一个《青槐高中》留下来的旧档（无身份戳）。
    修复前：`state.stats` 里的 `grace` 在当前包里不存在 → 渲染状态栏时 KeyError
    → HTTP 500，前端只显示"读取状态失败：HTTP 500"。
    修复后：内容级校验把它变成一句能读懂、能照做的话，且**状态不被半应用**。
    """
    from game_agent.save import save_game

    campus = load_worldpack(REPO / "world-packs" / "campus_otome")
    legacy = tmp_path / "legacy_campus.json"
    save_game(GameState.from_pack(campus), legacy)  # 无身份戳（G2 之前的格式）

    client, sid = _ancient_session(monkeypatch)
    # 存档必须落在 SAVE_ROOT 下才过 `_safe_save_path`（只允许裸文件名）
    monkeypatch.setattr(web, "SAVE_ROOT", tmp_path)
    before = client.get(f"/api/{sid}/status").json()["text"]

    r = client.post(f"/api/{sid}/load", json={"path": legacy.name})
    assert r.status_code == 400, f"应当是 400（可纠正的输入问题），实际 {r.status_code}"
    detail = r.json()["detail"]
    assert "读档被拒绝" in detail
    assert campus.world.name in detail  # 说清存档自报的世界
    assert "江湖旧梦" in detail  # 说清当前是哪张卡

    # 关键：拒绝后状态**没有被半应用**（否则界面会进入一种半坏状态）
    assert client.get(f"/api/{sid}/status").json()["text"] == before


def test_status_endpoint_survives_and_reports_after_rejection(monkeypatch, tmp_path):
    """被拒之后状态栏仍然可用——这是"500 之后整块右栏空白"的直接回归。"""
    from game_agent.save import save_game

    campus = load_worldpack(REPO / "world-packs" / "campus_otome")
    legacy = tmp_path / "legacy.json"
    save_game(GameState.from_pack(campus), legacy)
    monkeypatch.setattr(web, "SAVE_ROOT", tmp_path)

    client, sid = _ancient_session(monkeypatch)
    client.post(f"/api/{sid}/load", json={"path": legacy.name})  # 被拒
    st = client.get(f"/api/{sid}/status")
    assert st.status_code == 200  # 不再是 500
    assert st.json()["text"]


# ---------------------------------------------------------------------------
# 5. `world-packs/` 的遍历者必须区分"目录"与"卡"
# ---------------------------------------------------------------------------


#: 文件 → 它用来把"一个目录"和"一张卡"分开的判据。
#:
#: **为什么需要这张表**：`world-packs/` 现在有两个区域——内容区（每个子目录都是一张卡）
#: 与草稿区 `_drafts/`（**是工作区，不是卡**）。裸 `iterdir()` + `load_worldpack()`
#: 的写法在草稿区出现后会直接抛 `缺少文件: world-packs\_drafts\world.yaml`；
#: 而 `_drafts` 在字母序上排在 `ancient_jianghu` **之前**，所以那不是"偶尔踩到"，
#: 是**每次都崩**。`scripts/replay.py` 就是这么坏掉的——而且 pytest 盖不到它
#: （它不是测试的一部分），是"改路径要连带查谁在断言它"这条纪律查出来的。
_WORLD_PACK_ENUMERATORS = {
    "game_agent/catalog.py": "world.yaml",  # `_is_pack_dir`——生产口径，单一真源
    "scripts/eval_quota_check.py": "judge_corpus.yaml",
    "scripts/card_hook_check.py": "judge_corpus.yaml",
    "scripts/axis_coverage_check.py": "judge_corpus.yaml",
    "scripts/tier_slice.py": "judge_corpus.yaml",
    "scripts/near_dup_check.py": "judge_corpus.yaml",
    "scripts/replay.py": "catalog.list_packs",
}


def test_world_packs_enumerators_all_discriminate():
    """每个遍历 `world-packs/` 的文件都必须写明"凭什么算一张卡"。

    两件事一起守：
    1. **登记在案的遍历者不许丢掉判据**——有人把 `if (p/"world.yaml").exists()` 删了
       会在这里红，而不是等到"生成过一次草稿"之后在某个脚本里炸；
    2. **新的遍历者必须登记**——扫 `scripts/` 与 `game_agent/` 里所有同时出现
       `world-packs` 与 `iterdir(` 的文件，不在表里就失败。
       要求登记不是官僚：`_drafts/` 这个坑的教训正是"写遍历时没想过目录会有第二种"。

    判据检查用 **AST 反解析后的源码**（`ast.unparse`）而不是原始文本：注释会被丢掉。
    这条不是洁癖——第一版用原始文本，结果把 `catalog.list_packs` 换成一个不存在的函数、
    只在上面留一句提到它的注释，守卫**照样绿**。注释能满足的守卫等于没有守卫。
    检测"这个文件遍历了 world-packs"仍用原始文本（宁可多抓：多登记一个文件是无害的，
    漏登记才是有害的方向）。
    """
    import ast

    root = REPO
    found = set()
    for sub in ("scripts", "game_agent"):
        for f in sorted((root / sub).glob("*.py")):
            text = f.read_text(encoding="utf-8")
            if "world-packs" in text and "iterdir(" in text:
                found.add(f"{sub}/{f.name}")

    unregistered = found - set(_WORLD_PACK_ENUMERATORS)
    assert not unregistered, (
        f"这些文件遍历了 world-packs/ 但没登记判据：{sorted(unregistered)}。"
        f"请在 `_WORLD_PACK_ENUMERATORS` 里登记它靠什么区分目录与卡"
        f"（或改用 `catalog.list_packs()`）——草稿区 `_drafts/` 会让裸遍历抛异常。"
    )

    for rel, discriminator in _WORLD_PACK_ENUMERATORS.items():
        path = root / rel
        assert path.is_file(), f"登记的文件不存在了：{rel}（遍历者被删或改名，请同步这张表）"
        # `utf-8-sig`：仓库里有个脚本带 BOM（`card_hook_check.py`），
        # `ast.parse` 见到 U+FEFF 会直接 SyntaxError。
        code = ast.unparse(ast.parse(path.read_text(encoding="utf-8-sig")))
        assert discriminator in code, (
            f"{rel} 的**代码**里找不到区分判据 {discriminator!r}——"
            f"草稿区 `_drafts/` 会被当成一张卡。（写在注释里不算：注释不能挡异常。）"
        )

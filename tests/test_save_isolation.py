"""N4 守卫：存档按剧本隔离（目录级）。

上游：`docs/roadmap.md` **N4**（来源 `plan-creator-player.md` A5）、`docs/plan-dsh-tavern-parity.md` 期 0。

**被钉住的缺口**：此前存档只有剧本身份戳（G2 的 `pack:{id,digest}`），能**事后检查**
"这个档不是这张卡的"，却挡不住两件已经发生的事：

1. **同名互覆**——`web.json` 是前端默认存档名。两张卡各存一次，第二次直接把第一次的
   文件盖掉。身份戳再准也没用：旧档已经不存在了。
2. **列表里同路径**——两个档都叫 `web.json`，界面上无法共存，玩家只能靠"读了被拒"
   来分辨它们属于哪张卡。

N4 的做法是**用目录结构表达归属**（`saves/<pack_id>/`），与 E-7 草稿区
（`world-packs/_drafts/`）同一条思路。这个文件守四件事：

- **写进去的位置**：新档落在本卡目录，不再污染根目录；
- **不互覆**：两张卡同名存档**同时存在**，各自可读（这是 N4 换来的唯一硬收益）；
- **旧档不被牺牲**：N4 之前平铺在根上的档**仍然列得出来、读得出来**；
- **归属是硬的**：跨卡读/写一律拒绝，且错误里说清"属于谁、该怎么办"。

外加两条防回归：时间线目录不许被当成存档列出；包 id 不许穿越出 `saves/`。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fakes import FakeClient, msg, resp, tool_call

import game_agent.web as web
from game_agent.llm import LLMClient
from game_agent.save import save_game, saved_pack_meta
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack, pack_meta

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


def _submit(narration: str, choices: list[str], cid: str = "s1"):
    return tool_call(cid, "submit_narration",
                     {"narration": narration, "choices": choices, "plot_signal": "normal"})


@pytest.fixture
def live(tmp_path, monkeypatch):
    """两张**不同的**卡（`alpha` / `beta`）+ 走真实 `_make_game` 的 Web 客户端。

    **为什么不用桩替掉 `_make_game`**：自动存档路径是 `_make_game` 里算出来的，
    把它替掉就等于把"自动存档落在哪"这件事一起替掉了——而这条正是要守的。
    """
    root = tmp_path / "world-packs"
    root.mkdir()
    for name in ("alpha", "beta"):
        shutil.copytree(REAL_PACK, root / name)
    saves = tmp_path / "saves"
    monkeypatch.setattr(web, "_pack_root", lambda: root)
    monkeypatch.setattr(web, "SAVE_ROOT", saves)
    monkeypatch.setattr(web, "SESSIONS", {})
    monkeypatch.setattr(web, "load_settings", lambda: SimpleNamespace(has_api_key=True))
    monkeypatch.setattr(web, "_game_options", lambda: {})

    def fake_from_settings(settings, tools, tracker=None):
        replies = [
            resp(msg(tool_calls=[_submit(f"第{i}段正文。", ["甲", "乙", "丙"], f"s{i}")]))
            for i in range(20)
        ]
        return LLMClient(FakeClient(replies), "fake", tools)

    # `web.py` 里 `from .llm import LLMClient` 拿的就是这个类对象，改类属性即可生效。
    monkeypatch.setattr(web.LLMClient, "from_settings", staticmethod(fake_from_settings))
    return SimpleNamespace(client=TestClient(web.app), saves=saves, root=root)


def _open(client: TestClient, pack_id: str) -> str:
    """开一局并解掉开局的**关键抉择**（该包开局即 `how_to_help`，不解掉后面 `say` 会被拒）。"""
    d = client.post("/api/new", json={"pack_id": pack_id}).json()
    sid = d["sid"]
    if d["view"].get("choice_prompt"):
        r = client.post(f"/api/{sid}/turn", json={"kind": "pick", "index": 0})
        assert "event: done" in r.text, r.text[:200]
    return sid


# ---------------------------------------------------------------------------
# 1. 写进去的位置
# ---------------------------------------------------------------------------


def test_save_lands_in_the_pack_directory(live):
    """新档必须落进 `saves/<pack_id>/`，且**不在**根上留一份。"""
    client, saves = live.client, live.saves
    sid = _open(client, "alpha")
    r = client.post(f"/api/{sid}/save", json={"path": "hand.json"})
    assert r.status_code == 200, r.text
    assert (saves / "alpha" / "hand.json").exists()
    assert not (saves / "hand.json").exists(), "根目录又被污染了——那正是 N4 要消除的"


def test_autosave_goes_into_the_pack_directory(live):
    """W-C 自动存档同样按卡分目录（`_autosave_path` 必须经 `pack_save_dir`）。"""
    client, saves = live.client, live.saves
    sid = _open(client, "alpha")
    path = Path(web.SESSIONS[sid].game.autosave_path)
    assert path.parent == (saves / "alpha").resolve() or path.parent.name == "alpha", path
    assert path.name == f"autosave-{sid}.json"


# ---------------------------------------------------------------------------
# 2. 不互覆（N4 的唯一硬收益）
# ---------------------------------------------------------------------------


def test_two_packs_can_hold_a_save_with_the_same_name(live):
    """**核心**：两张卡都用默认名 `web.json` 存档 → 两个文件同时存在、都能读回来。

    改动前它们会互相覆盖（同一个 `saves/web.json`），而"覆盖"是不可逆的——
    这条守卫就是那个 bug 的墓碑。
    """
    client, saves = live.client, live.saves
    a = _open(client, "alpha")
    b = _open(client, "beta")
    assert client.post(f"/api/{a}/save", json={"path": "web.json"}).status_code == 200
    assert client.post(f"/api/{b}/save", json={"path": "web.json"}).status_code == 200

    pa, pb = saves / "alpha" / "web.json", saves / "beta" / "web.json"
    assert pa.exists() and pb.exists(), "同名存档必须能并存"
    assert json.loads(pa.read_text("utf-8"))["pack"]["id"] == "alpha"
    assert json.loads(pb.read_text("utf-8"))["pack"]["id"] == "beta"

    # 各自读回来都成功（读档会先比对剧本身份戳，读错卡会 400）
    assert client.post(f"/api/{a}/load", json={"path": "web.json"}).status_code == 200
    assert client.post(f"/api/{b}/load", json={"path": "web.json"}).status_code == 200


# ---------------------------------------------------------------------------
# 3. 旧档不被牺牲
# ---------------------------------------------------------------------------


def test_legacy_flat_save_is_listed_and_still_loadable(live):
    """N4 之前平铺在根上的档：列表里看得见（`dir=None`），并且**读得出来**。

    目录改造不该让玩家的旧档凭空消失。它们原地不动，靠 `_safe_save_path` 的回退读回。
    """
    client, saves = live.client, live.saves
    saves.mkdir(parents=True, exist_ok=True)
    # 用**复制出来的那张卡**的元数据打戳：身份戳含 id，拿原始包（`ancient_jianghu`）
    # 的戳去给 `alpha` 的会话读，会被 G2 的身份校验正当拒绝——那条校验是对的。
    pack = load_worldpack(live.root / "alpha")
    legacy = saves / "legacy.json"
    save_game(GameState.from_pack(pack), legacy, [], pack_meta=pack_meta(pack))

    rows = {r["path"]: r for r in client.get("/api/saves").json()["saves"]}
    assert "legacy.json" in rows, rows
    assert rows["legacy.json"]["dir"] is None  # 根上的旧档：不属于任何卡目录

    sid = _open(client, "alpha")  # 复制出来的 alpha 与原始包内容相同 → 身份戳对得上
    r = client.post(f"/api/{sid}/load", json={"path": "legacy.json"})
    assert r.status_code == 200, r.text
    assert legacy.exists(), "读旧档不该把它搬走或删掉"


def test_listing_reports_pack_directory_and_directly_usable_path(live):
    """列表的 `path` 必须能**原样**回传给保存/读档端点（前端就是原样回传的）。"""
    client, saves = live.client, live.saves
    sid = _open(client, "beta")
    client.post(f"/api/{sid}/save", json={"path": "listed.json"})

    rows = {r["path"]: r for r in client.get("/api/saves").json()["saves"]}
    key = "beta/listed.json"
    assert key in rows, rows
    assert rows[key]["dir"] == "beta"
    assert rows[key]["pack"]["id"] == "beta"
    # 原样回传可用
    assert client.post(f"/api/{sid}/load", json={"path": key}).status_code == 200


# ---------------------------------------------------------------------------
# 4. 归属是硬的
# ---------------------------------------------------------------------------


def test_cross_pack_load_is_refused_with_a_usable_message(live):
    """跨卡读档：拒绝 **并说清属于谁**——这是目录隔离换来的更好的错误。"""
    client, saves = live.client, live.saves
    a = _open(client, "alpha")
    b = _open(client, "beta")
    client.post(f"/api/{b}/save", json={"path": "theirs.json"})

    r = client.post(f"/api/{a}/load", json={"path": "beta/theirs.json"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "beta" in detail and "alpha" in detail, detail
    assert "换到那张卡" in detail, detail  # 必须说清怎么办，而不是只说"不行"


def test_cross_pack_write_is_refused_and_does_not_touch_the_other_pack(live):
    """跨卡**写**更严重（会盖掉别人的档）——必须拒绝且目标目录不被创建。"""
    client, saves = live.client, live.saves
    a = _open(client, "alpha")
    r = client.post(f"/api/{a}/save", json={"path": "beta/sneaky.json"})
    assert r.status_code == 400, r.text
    assert not (saves / "beta" / "sneaky.json").exists()


@pytest.mark.parametrize("bad", [
    "../../.env",
    "..\\..\\pyproject.toml",
    "x.txt",
    "x",
    "/abs/x.json",
    "alpha/../../escape.json",
    "a/b/c.json",
    "C:/evil.json",
    "alpha/",
])
def test_save_paths_stay_inside_the_saves_root(live, bad):
    """A-1 的路径穿越防御在 N4 之后仍然成立（多了一层卡目录，穿越面更大）。"""
    client, _saves = live.client, live.saves
    sid = _open(client, "alpha")
    for ep in ("save", "load"):
        r = client.post(f"/api/{sid}/{ep}", json={"path": bad})
        assert r.status_code == 400, f"{ep} 应拒绝 {bad!r}，实际 {r.status_code}"


def test_pack_id_cannot_escape_the_saves_root():
    """`pack_save_dir` 也要自己拦（不只依赖调用方传对值）。"""
    for bad in ("../x", "a/b", "", "..", "a b"):
        with pytest.raises(Exception):
            web.pack_save_dir(bad)


# ---------------------------------------------------------------------------
# 5. 两条防回归
# ---------------------------------------------------------------------------


def test_timeline_directories_are_not_listed_as_saves(live):
    """N7 的 `saves/timeline-<sid>/rev-*.json` 是时间线快照，**不是**玩家存档。

    不加这条跳过，玩家会在存档列表里看到一堆"读了就炸"的档。
    """
    client, saves = live.client, live.saves
    tl = saves / "timeline-abc123"
    tl.mkdir(parents=True)
    (tl / "rev-000001.json").write_text("{}", encoding="utf-8")
    paths = [r["path"] for r in client.get("/api/saves").json()["saves"]]
    assert not any("timeline-" in p for p in paths), paths


def test_unknown_directories_are_not_treated_as_packs(live):
    """`saves/` 下不像卡 id 的目录不要当成存档目录（防御：别把别人的手工目录读成卡）。"""
    client, saves = live.client, live.saves
    weird = saves / "some dir"
    weird.mkdir(parents=True)
    (weird / "x.json").write_text("{}", encoding="utf-8")
    assert client.get("/api/saves").json()["saves"] == []


# ---------------------------------------------------------------------------
# 6. 迁移脚本（N4 的收尾工具）
# ---------------------------------------------------------------------------


def test_migration_plan_and_apply(tmp_path):
    """迁移脚本：有戳的搬、无戳的留、撞名的不覆盖。**默认只报告**。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migrate_saves", REPO / "scripts" / "migrate_saves.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    saves = tmp_path / "saves"
    saves.mkdir()
    pack = load_worldpack(REAL_PACK)
    meta = pack_meta(pack)
    state = GameState.from_pack(pack)

    save_game(state, saves / "stamped.json", [], pack_meta=meta)          # 有戳 → 搬
    save_game(state, saves / "unstamped.json", [])                        # 无戳 → 跳过
    save_game(state, saves / "clash.json", [], pack_meta=meta)            # 目标已存在 → 跳过
    (saves / meta["id"]).mkdir()
    (saves / meta["id"] / "clash.json").write_text("{}", encoding="utf-8")

    moves, skipped = mod.plan_migration(saves)
    assert [s.name for s, _ in moves] == ["stamped.json"]
    reasons = {s.name: why for s, why in skipped}
    assert "身份戳" in reasons["unstamped.json"]
    assert "目标已存在" in reasons["clash.json"]
    assert (saves / "stamped.json").exists(), "纯计算阶段不许动文件"

    mod.apply_migration(moves)
    assert (saves / meta["id"] / "stamped.json").exists()
    assert not (saves / "stamped.json").exists()
    assert saved_pack_meta(saves / meta["id"] / "stamped.json")["id"] == meta["id"]
    assert (saves / "unstamped.json").exists(), "无戳的档必须原地不动"

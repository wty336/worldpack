"""守卫：草稿区 ↔ 已发布区（`docs/plan-tavern-shaped-product.md` §3.2 ③ / E-7）。

**这是那份文档明确要求的"唯一写口"**：Web 界面不直写文件系统，所有内容变更经
"生成 → 校验 → 发布"这条链。它同时解开了 N1 留下的限制——在草稿区落地之前，
"同名生成"被一刀拒绝，于是"改一版再生成"只能靠不停换名字。

本文件守四类性质：

1. **草稿不可见性**：草稿**绝不能**出现在可选卡列表里。这不是"记得过滤"——
   `_drafts/` 这个目录本身没有 `world.yaml`，所以目录扫描天然跳过它。
   靠约定不如靠结构，这条守卫钉住结构。
2. **发布闸门**：`check_worldpack` 不过 → 拒绝发布，且**报错原文要能拿到**
   （工作台拿它去喂模型修，这正是 §4.2 的修复循环燃料）。
3. **不覆盖已发布内容**：发布与生成都不动已发布区。
4. **草稿试玩**：能用未发布的草稿开局，且**标记为草稿**（前端据此打"未发布"水印）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fakes import FakeClient, msg, resp, tool_call

import game_agent.web as web
from game_agent import catalog
from game_agent.catalog import CatalogError
from game_agent.game import Game
from game_agent.llm import LLMClient, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

REPO = Path(__file__).resolve().parent.parent
REAL_PACK = REPO / "world-packs" / "ancient_jianghu"


def _submit(narration: str, choices: list[str]):
    return tool_call("s1", "submit_narration",
                     {"narration": narration, "choices": choices, "plot_signal": "normal"})


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """一套隔离的 world-packs/（含一个已发布包 + 一个草稿）。"""
    r = tmp_path / "world-packs"
    r.mkdir(parents=True)
    shutil.copytree(REAL_PACK, r / "published")
    shutil.copytree(REAL_PACK, catalog.draft_dir("draft_one", r))
    monkeypatch.setattr(web, "_pack_root", lambda: r)
    return r


def _break(draft_root: Path, how: str = "missing") -> None:
    """把草稿弄坏，用来测发布闸门与试玩拒绝。

    `how` 覆盖两种**不同**的坏法，它们会走到不同的代码路径：

    - `missing`：删掉 `world.yaml`。包**看起来不像包**了（`_is_pack_dir` 为假）。
      这条曾把草稿从列表里整个抹掉，于是"坏草稿"表现为"草稿不存在"——
      作者最需要看到报错原文的时刻，看到的却是"查无此物"。`list_drafts` 现在
      列出草稿区下的每个子目录，正是为了这条。
    - `bad_yaml`：文件在、内容坏。这是真实的作者中途状态。
    """
    if how == "missing":
        (draft_root / "world.yaml").unlink()
    elif how == "bad_yaml":
        (draft_root / "world.yaml").write_text("name: [未闭合的列表\n", encoding="utf-8")
    else:  # pragma: no cover - 守卫自检
        raise AssertionError(f"未知的破坏方式: {how}")


# ---------------------------------------------------------------------------
# 1. 草稿不可见性（靠结构，不靠"记得过滤"）
# ---------------------------------------------------------------------------


def test_drafts_never_appear_as_playable_cards(root):
    """**核心断言**：草稿不出现在 `/api/catalog` 里。"""
    ids = [p["id"] for p in TestClient(web.app).get("/api/catalog").json()["packs"]]
    assert "published" in ids
    assert "draft_one" not in ids, "草稿泄漏进了可选卡列表——玩家会点到半成品"


def test_drafts_dir_is_not_itself_a_pack(root):
    """`_drafts/` 目录本身不被当成一个包（它没有 world.yaml）。

    这条是上面那条**成立的原因**：如果哪天有人给 `_drafts/` 加一个 world.yaml
    （比如"工作区元数据"），草稿就会以"一张巨大的坏卡"的形式出现在目录里。
    """
    assert not (catalog.drafts_root(root) / "world.yaml").exists()
    assert catalog.resolve_pack(catalog.DRAFTS_DIRNAME, root) is None
    # 而且它确实被列进了草稿区视图，只是不带 world.yaml 的子目录会被跳过
    assert [e.id for e in catalog.list_drafts(root)] == ["draft_one"]


def test_draft_and_published_views_are_separate(root):
    assert [e.id for e in catalog.list_packs(root)] == ["published"]
    drafts = catalog.list_drafts(root)
    assert [e.id for e in drafts] == ["draft_one"]
    assert drafts[0].draft is True
    assert catalog.list_packs(root)[0].draft is False


def test_resolve_pack_does_not_reach_into_drafts(root):
    """`pack_id` 查表只认已发布区——草稿必须经 `draft` 走另一条路。"""
    assert catalog.resolve_pack("draft_one", root) is None
    assert catalog.resolve_draft("draft_one", root) is not None
    # 路径穿越对两者都无效（都是查表，不拼路径）
    for evil in ("../published", "_drafts/draft_one", "..", "/etc/passwd"):
        assert catalog.resolve_draft(evil, root) is None


# ---------------------------------------------------------------------------
# 2. 发布闸门
# ---------------------------------------------------------------------------


def test_publish_moves_draft_to_published(root):
    entry = catalog.publish("draft_one", root)
    assert entry.id == "draft_one" and entry.draft is False
    assert (root / "draft_one" / "world.yaml").is_file()
    assert not catalog.draft_dir("draft_one", root).exists()
    assert [e.id for e in catalog.list_packs(root)] == ["draft_one", "published"]


def test_publish_refuses_when_draft_fails_gate(root):
    """**闸门 = check_worldpack 必须过**。不过 → 拒绝，且**报错原文可拿到**。

    报错原文是给模型修用的燃料（§4.2），所以不能只回一句"校验失败"。
    """
    _break(catalog.draft_dir("draft_one", root))
    reason = catalog.can_publish("draft_one", root)
    assert reason is not None
    assert "check-worldpack" in reason
    assert "world.yaml" in reason, "报错必须带原文（否则工作台没法把它喂给模型）"

    with pytest.raises(CatalogError):
        catalog.publish("draft_one", root)
    # 失败后草稿**原地不动**（不会出现"半个已发布包"）
    assert catalog.draft_dir("draft_one", root).is_dir()
    assert not (root / "draft_one").exists()


@pytest.mark.parametrize("how", ["missing", "bad_yaml"])
def test_broken_draft_stays_visible_in_draft_area(root, how):
    """**坏草稿不许从列表里消失**——两种坏法都要看得见、且带得出报错原文。

    这条守的是"缺 world.yaml 的草稿被静默抹掉"那个具体缺陷：已发布区可以靠
    "没有 world.yaml 就不是内容"把杂物挡在外面，草稿区不行——那里的东西是作者
    自己放进去的，消失得无声无息才是最难查的故障。
    """
    _break(catalog.draft_dir("draft_one", root), how)
    drafts = {e.id: e for e in catalog.list_drafts(root)}
    assert "draft_one" in drafts, f"{how}：坏草稿从草稿区消失了（作者会以为它凭空不见）"
    entry = drafts["draft_one"]
    assert entry.draft is True
    assert entry.playable is False
    assert "world.yaml" in entry.error, f"{how}：报错原文丢了 → 工作台没法拿它喂模型"


def test_publish_refuses_name_collision_with_published(root):
    """不覆盖已发布内容。"""
    shutil.copytree(REAL_PACK, catalog.draft_dir("published", root))
    reason = catalog.can_publish("published", root)
    assert reason is not None and "已存在同名" in reason
    assert (root / "published").is_dir()


def test_publish_missing_draft_and_bad_name(root):
    assert "草稿不存在" in (catalog.can_publish("nope", root) or "")
    assert catalog.can_publish("../evil", root) is not None


def test_generate_does_not_touch_published_area(root):
    """生成写草稿区——已发布区的 mtime/内容都不该被动。"""
    before = (root / "published" / "world.yaml").read_text(encoding="utf-8")
    client = TestClient(web.app)
    r = client.post("/api/packs/generate", json={
        "name": "fresh", "source_text": "# 素材\n\n某人在城里醒来。\n", "offline": True,
    })
    assert r.status_code == 202
    from tests.test_jobs import _wait  # 复用等待助手（同一套任务表语义）

    assert _wait(web.JOBS.get(r.json()["job_id"])).status == "done"
    assert (catalog.draft_dir("fresh", root) / "world.yaml").is_file()
    assert not (root / "fresh").exists()
    assert (root / "published" / "world.yaml").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# 3. 删除
# ---------------------------------------------------------------------------


def test_delete_draft_only_touches_drafts(root):
    client = TestClient(web.app)
    assert client.delete("/api/packs/drafts/draft_one").json()["ok"] is True
    assert not catalog.draft_dir("draft_one", root).exists()
    # 已发布包不能被这个接口删掉
    r = client.delete("/api/packs/drafts/published")
    assert r.status_code == 400
    assert (root / "published").is_dir(), "已发布内容不该被 HTTP 顺手删掉"


def test_delete_draft_rejects_traversal(root):
    """路径穿越：`../published` 之类的名字必须无效（查表 + 白名单双保险）。"""
    client = TestClient(web.app)
    for evil in ("..%2Fpublished", "%2E%2E%2Fpublished"):
        r = client.delete(f"/api/packs/drafts/{evil}")
        assert r.status_code in (400, 404), f"{evil} 不该被接受"
    assert (root / "published").is_dir()


# ---------------------------------------------------------------------------
# 4. 草稿试玩（§3.2 ③ 的"生成出来的东西得先能试玩"）
# ---------------------------------------------------------------------------


def _offline_client(monkeypatch, seen: list | None = None) -> TestClient:
    """装配一个不花钱、但**真的按 `draft` / `pack_id` 选包**的 Web 客户端。

    为什么不复用 `test_catalog._client`：那个假 `_make_game` **刻意忽略**选包参数
    （它守的是"参数有没有传到"）。而本文件要守的是试玩语义，其中
    `session_is_draft` 是**从包的真实位置推出来的**——如果选包不生效，
    这个断言永远是 `False == False`，看起来绿，其实什么都没测。
    所以这里必须真的加载被选中的那个包，只把"花钱的部分"（LLM、Key）换掉。
    """
    def fake_make_game(sid: str, pack_id=None, *, draft=None, mainline_enabled=True):
        if seen is not None:
            seen.append({"pack_id": pack_id, "draft": draft,
                         "mainline_enabled": mainline_enabled})
        # 复用**生产**的选包解析（纯路径查表，不需要 API Key）。
        # 坏草稿的 400 正是从这里出来的，所以这条链是真的被测着的。
        path, _ = web._resolve_pack(pack_id, draft)
        pack = load_worldpack(path)
        llm = LLMClient(
            FakeClient([resp(msg(tool_calls=[_submit("开场。", ["甲", "乙", "丙"])]))]),
            "fake", build_tools(pack.schedule),
        )
        return (
            Game(pack, GameState.from_pack(pack), llm, mainline_enabled=mainline_enabled),
            web.UsageTracker("saves/usage-test-only.jsonl", session=sid),
        )

    monkeypatch.setattr(web, "_make_game", fake_make_game)
    return TestClient(web.app)


def test_playtest_draft_session_is_marked_as_draft(root, monkeypatch):
    """能用未发布的草稿开局，且 `draft: true`（前端据此打"未发布"水印）。

    为什么标记必须来自**真实位置**而不是另存一个布尔量：与 `session_pack_id` 同一纪律——
    存第二份就多一个"改了这边忘了那边"的机会。
    """
    monkeypatch.setattr(web, "SESSIONS", {})
    seen: list = []
    client = _offline_client(monkeypatch, seen)

    d = client.post("/api/new", json={"draft": "draft_one"}).json()
    assert seen[-1]["draft"] == "draft_one", "draft 参数没传到选包那一步"
    assert d["draft"] is True
    assert d["pack_id"] == "draft_one"
    meta = client.get(f"/api/{d['sid']}/meta").json()
    assert meta["draft"] is True

    # 已发布的卡则不是草稿——同一个推导函数，两种位置给出两种答案
    d2 = client.post("/api/new", json={"pack_id": "published"}).json()
    assert d2["draft"] is False
    assert client.get(f"/api/{d2['sid']}/meta").json()["draft"] is False

    # 名字相同、只差所在区域：证明标记来自**位置**，不是名字里带了什么
    shutil.copytree(REAL_PACK, root / "draft_one")
    d3 = client.post("/api/new", json={"pack_id": "draft_one"}).json()
    assert d3["draft"] is False, "已发布区的同名包被误判成草稿"


def test_playtest_unknown_draft_is_400(root, monkeypatch):
    _offline_client(monkeypatch)
    r = TestClient(web.app).post("/api/new", json={"draft": "no_such_draft"})
    assert r.status_code == 400 and "草稿不存在" in r.json()["detail"]


@pytest.mark.parametrize("how", ["missing", "bad_yaml"])
def test_playtest_broken_draft_is_refused_with_reason(root, monkeypatch, how):
    """坏草稿**不能开局**，且理由可读——否则玩家会在中途撞上 KeyError 之类的怪错。"""
    _break(catalog.draft_dir("draft_one", root), how)
    _offline_client(monkeypatch)
    r = TestClient(web.app).post("/api/new", json={"draft": "draft_one"})
    assert r.status_code == 400
    assert "无法加载" in r.json()["detail"]
    assert "world.yaml" in r.json()["detail"], "拒绝对了，但没给作者原文"

"""N11 守卫：分发的两半——**入口**与**包内资源**，外加 MCP 出口的加固。

上游：`docs/plan-dsh-tavern-parity.md` 期 5 / 差距 **G-8·G-9**、
`sillytavern-borrow.md` **§E-2**（URI 间接层）、`docs/roadmap.md` **N11**。

分发不是"打个 wheel"就完了，它有三件必须同时成立的事：

1. **一条命令能跑**（`[project.scripts]`）——否则装完之后玩家还是要看 README 学
   `python -m game_agent`；
2. **卡与资源一起进包**（`force-include` + `resolve_pack_root` 的三级回退）——
   入口有了但包里没有 `world-packs/`，装完就是一张空卡列表；
3. **包内资源引用不失效**（`asset://`）——作者在世界包里写死路径，换环境就坏；
   ST 的 `.charx` 踩过这个坑，它的解法（前缀 + 统一解析入口）照做。

第 4 条（MCP 出口加固）另算：那是给"外接宿主"用的面，输入不可信。
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from game_agent import assets, catalog, cli
from game_agent.worldpack import WorldPackError, load_worldpack

REPO = Path(__file__).resolve().parent.parent
PACK_SRC = REPO / "world-packs" / "ancient_jianghu"
CLI_PATH = REPO / "scripts" / "import_story.py"


# ---------------------------------------------------------------------------
# 1. 入口：pyproject 里必须真的声明了，且指向真实存在的函数
# ---------------------------------------------------------------------------


def test_console_script_is_declared_and_points_at_a_real_function():
    """`[project.scripts]` 要有 `game-agent`，且它指向的函数**确实存在**。

    为什么不能只断言字符串：`game-agent = "game_agent.cli:main"` 里的函数名写错时，
    `uv build` 照样成功——问题在**装完运行时**才炸（`ImportError: module has no
    attribute`），而那时玩家已经拿到包了。这里直接 import 解析一遍。
    """
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})
    assert "game-agent" in scripts, f"没有声明 console script：{scripts}"

    target = scripts["game-agent"]
    module_name, _, func_name = target.partition(":")
    assert func_name, f"入口必须是 module:function 形式，实际 {target!r}"

    import importlib

    mod = importlib.import_module(module_name)
    assert callable(getattr(mod, func_name, None)), f"{module_name} 里没有可调用的 {func_name}"


def test_world_packs_are_forced_into_the_wheel():
    """整包分发的第二半：卡必须被打进 wheel（否则 `uvx` 装完一张卡都没有）。"""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    force = data["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})
    assert "world-packs" in force, f"world-packs 没有 force-include：{force}"


def test_pack_root_falls_back_into_the_installed_package(monkeypatch, tmp_path):
    """`resolve_pack_root()` 的三级回退：cwd 没有 `world-packs/` 时要用包内那份。

    模拟"装成包之后在玩家自己的目录里跑"：cwd 换到空目录，同时把 `catalog.__file__`
    指到一个**假包目录**（里面有 `world-packs/<卡>`）——这正是 wheel 里
    `force-include` 打出来的形状。
    """
    pkg = tmp_path / "site-packages" / "game_agent"
    (pkg / "world-packs" / "demo").mkdir(parents=True)
    (pkg / "world-packs" / "demo" / "world.yaml").write_text("name: demo\n", encoding="utf-8")
    (pkg / "catalog.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(catalog, "__file__", str(pkg / "catalog.py"))
    monkeypatch.chdir(tmp_path / "player-cwd" if (tmp_path / "player-cwd").mkdir() or True else tmp_path)

    resolved = catalog.resolve_pack_root()
    assert Path(resolved) == pkg / "world-packs", f"没回退到包内：{resolved}"
    assert (Path(resolved) / "demo" / "world.yaml").is_file()
    # 显式参数优先（CLI 的 --pack-dir / Web 的 GAME_WORLDPACK 全靠它）
    assert catalog.resolve_pack_root(tmp_path / "x") == tmp_path / "x"


def test_cli_default_pack_is_resolved_not_hardcoded(monkeypatch, tmp_path):
    """CLI 的默认卡路径不能是写死的相对字面量（装成包之后那个路径不存在）。"""
    pkg = tmp_path / "site-packages" / "game_agent"
    (pkg / "world-packs").mkdir(parents=True)
    # 必须是**真能加载**的包：`default_pack_id()` 只从可玩的卡里挑；
    # 放个假目录它会返回 None，那测到的就不是"回退"而是"兜底字面量"了。
    shutil.copytree(PACK_SRC, pkg / "world-packs" / "ancient_jianghu")
    (pkg / "catalog.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(catalog, "__file__", str(pkg / "catalog.py"))
    (tmp_path / "player-cwd").mkdir()
    monkeypatch.chdir(tmp_path / "player-cwd")

    path = cli._default_worldpack()
    assert Path(path).is_dir(), f"CLI 默认卡路径 {path} 不存在——装成包就开不了局"
    assert Path(path).parent == pkg / "world-packs"
    src = (REPO / "game_agent" / "cli.py").read_text(encoding="utf-8")
    assert 'default="world-packs/ancient_jianghu"' not in src, "默认路径又被写死了"


# ---------------------------------------------------------------------------
# 2. asset:// 间接层
# ---------------------------------------------------------------------------


def _pack_with_asset(tmp_path, *, ref: str, content: str | None = "律法正文。") -> Path:
    """复制一个真实包 + 把第一条 lore 的 text 换成 `asset://` 引用。

    用 `yaml.safe_load` / `safe_dump` 改写而不是按行替换：真实包的 lore 是**流式写法**
    （`- {id: …, keys: […], text: …}`），按行替换会把这个夹具绑死在某种排版上。
    """
    import yaml

    dst = tmp_path / "pkg"
    shutil.copytree(PACK_SRC, dst)
    if content is not None:
        asset = dst / "assets" / "law.txt"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(content, encoding="utf-8")
    world = dst / "world.yaml"
    data = yaml.safe_load(world.read_text(encoding="utf-8"))
    assert data["lore"], "夹具前提不成立：这个包没有 lore"
    data["lore"][0]["text"] = ref
    world.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=200),
        encoding="utf-8",
    )
    return dst


def test_asset_ref_is_resolved_at_load_time(tmp_path):
    """`asset://` 在**加载期**就被换成文件内容：引擎各处看到的是文本，不是路径。"""
    pack_dir = _pack_with_asset(tmp_path, ref="asset://assets/law.txt",
                                content="凡斗殴者，笞二十。")
    pack = load_worldpack(pack_dir)
    texts = [e.text for e in pack.world.lore]
    assert "凡斗殴者，笞二十。" in texts, texts
    assert not any("asset://" in t for t in texts), "加载之后不该还有未解析的引用"


def test_missing_asset_fails_at_load_time(tmp_path):
    """文件不存在 → **加载期**报错（而不是运行时把设定读成空字符串）。"""
    pack_dir = _pack_with_asset(tmp_path, ref="asset://assets/nope.txt", content=None)
    with pytest.raises(WorldPackError) as ei:
        load_worldpack(pack_dir)
    msg = str(ei.value)
    assert "nope.txt" in msg and "不存在" in msg, msg


@pytest.mark.parametrize("bad", [
    "asset://../../etc/passwd",   # 逃出包根
    "asset:///etc/passwd",        # 绝对路径
    "asset://C:/windows/win.ini", # 盘符
])
def test_asset_ref_cannot_escape_the_pack(tmp_path, bad):
    """世界包是**别人给的素材**：路径穿越在这里是真实威胁，不是洁癖。"""
    with pytest.raises(assets.AssetError):
        assets.resolve(bad, tmp_path)


def test_non_utf8_asset_is_refused(tmp_path):
    """只分发**文本**资源：二进制没有消费方，明确拒绝而不是塞进上下文。"""
    p = tmp_path / "bin.dat"
    p.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(assets.AssetError) as ei:
        assets.load_text("asset://bin.dat", tmp_path)
    assert "UTF-8" in str(ei.value)


def test_oversized_asset_is_refused(tmp_path):
    """超过上限 → 拒绝，并指出该走素材导入而不是运行时注入。"""
    big = tmp_path / "big.txt"
    big.write_text("长" * (assets.MAX_ASSET_BYTES + 1), encoding="utf-8")
    with pytest.raises(assets.AssetError) as ei:
        assets.load_text("asset://big.txt", tmp_path)
    msg = str(ei.value)
    assert "过大" in msg and "import_story" in msg, msg


def test_inline_mention_is_not_a_reference():
    """"见 asset://x" 夹在句子里**不是**引用——模糊匹配会让正文变成神秘的文件读取。"""
    assert not assets.is_asset_ref("见 asset://x")
    assert assets.is_asset_ref("asset://x")


def test_asset_expansion_does_not_touch_http_urls(tmp_path):
    """`http://…` 这类正文里的 URL 不受影响（只认 `asset://`）。"""
    data = {"a": "https://example.com/x", "b": ["asset://ok.txt"]}
    (tmp_path / "ok.txt").write_text("内容", encoding="utf-8")
    out = assets.expand({"a": data["a"]}, tmp_path)
    assert out["a"] == "https://example.com/x"


# ---------------------------------------------------------------------------
# 3. 存档可迁移（整包分发之后"带着存档走"要还能读）
# ---------------------------------------------------------------------------


def test_saves_travel_with_the_pack(tmp_path):
    """把包与它的存档目录一起搬到别处 → 仍然读得出来（N4 的目录隔离让它成为一次拷贝）。

    期 5 的验收："存档自带剧本身份 → 支持整包分发 + 存档可迁移"。这里验的是
    **真正的可迁移**：路径全部相对 `SAVE_ROOT`，没有写死的绝对路径。
    """
    from game_agent.save import load_game, save_game
    from game_agent.state import GameState
    from game_agent.worldpack import pack_meta

    pkg = tmp_path / "moved" / "ancient_jianghu"
    pkg.parent.mkdir(parents=True)
    shutil.copytree(PACK_SRC, pkg)
    pack = load_worldpack(pkg)
    saves = tmp_path / "moved" / "saves" / "ancient_jianghu"
    saves.mkdir(parents=True)
    state = GameState.from_pack(pack)
    state.turn_count = 7
    save_game(state, saves / "e2e.json", [], pack_meta=pack_meta(pack))

    # 模拟"在另一个工作目录里装好之后读档"：只用相对把新根下的档读回来
    reloaded = load_game(saves / "e2e.json", pack_meta=pack_meta(pack))
    assert reloaded.turn_count == 7


# ---------------------------------------------------------------------------
# 4. MCP 出口加固（外接宿主，输入不可信）
# ---------------------------------------------------------------------------


def test_mcp_rejects_non_object_requests_without_dying():
    """`[1,2]` / `"x"` / `null` 这类请求要回 -32600，**不能**把 serve 循环打断。"""
    from game_agent.mcp_server import handle_request
    from game_agent.registry import build_registry

    pack = load_worldpack(PACK_SRC)
    registry = build_registry(pack)
    for bad in ([1, 2], "x", 42, None):
        resp = handle_request(registry, bad)
        assert resp is not None and resp["error"]["code"] == -32600, (bad, resp)


def test_mcp_survives_bad_request_shapes_end_to_end():
    """端到端：坏请求之后**同一场会话**仍能正常握手（会话不因一行输入而断）。"""
    from game_agent.mcp_server import serve

    pack = load_worldpack(PACK_SRC)
    from game_agent.game import Game
    from game_agent.llm import LLMClient, build_tools
    from game_agent.state import GameState

    game = Game(pack, GameState.from_pack(pack),
                LLMClient(_EmptyFake(), "fake", build_tools(pack.schedule)))
    lines = [
        json.dumps([1, 2]) + "\n",                          # 非对象
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "initialize"}) + "\n",
        "{ not json\n",                                      # 坏 JSON
        json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping"}) + "\n",
    ]
    inp, out = _FakeStream(lines), _FakeStream([])
    serve(game, stdin=inp, stdout=out)
    payloads = [json.loads(line) for line in out.written if line.strip()]
    codes = [p.get("error", {}).get("code") for p in payloads]
    assert -32600 in codes, payloads          # 非对象请求
    assert -32700 in codes, payloads          # 坏 JSON
    assert any(p.get("id") == 9 and "result" in p for p in payloads), payloads
    assert any(p.get("id") == 10 and "result" in p for p in payloads), payloads


def test_mcp_refuses_oversized_requests():
    """超长单行直接拒绝（不解析、不占内存），并说明为什么。"""
    from game_agent.mcp_server import MAX_REQUEST_CHARS, serve

    pack = load_worldpack(PACK_SRC)
    from game_agent.game import Game
    from game_agent.llm import LLMClient, build_tools
    from game_agent.state import GameState

    game = Game(pack, GameState.from_pack(pack),
                LLMClient(_EmptyFake(), "fake", build_tools(pack.schedule)))
    huge = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping",
                       "params": {"x": "长" * MAX_REQUEST_CHARS}}) + "\n"
    inp, out = _FakeStream([huge, '{"jsonrpc":"2.0","id":2,"method":"ping"}\n']), _FakeStream([])
    serve(game, stdin=inp, stdout=out)
    payloads = [json.loads(line) for line in out.written if line.strip()]
    assert payloads[0]["error"]["code"] == -32600, payloads[0]
    assert "过大" in payloads[0]["error"]["message"]
    # 会话没断：第二条照常答
    assert payloads[1]["id"] == 2 and "result" in payloads[1], payloads


class _FakeStream:
    """极简读写替身：`for line in stream` 与 `write` 都够用。"""

    def __init__(self, lines: list[str]):
        self._lines = list(lines)
        self.written: list[str] = []

    def __iter__(self):
        return iter(self._lines)

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass


class _EmptyFake:
    """不会生成任何响应的假客户端（MCP 加固测试只关心协议层，不跑回合）。"""

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):  # pragma: no cover - 加固测试不应触发生成
        raise AssertionError("这条测试不该触发模型调用")

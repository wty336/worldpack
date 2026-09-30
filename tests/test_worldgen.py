"""B1 守卫：素材 → 世界包 的生成管线（`game_agent/worldgen.py`）。

上游：`docs/plan-tavern-shaped-product.md` §6 的 E-5；`docs/plan-creator-player.md` §2 缺口 G3。

**这一批是纯重构**（把管线从 945 行的 CLI 脚本提到引擎侧模块），所以守卫的重点是
**"行为一字未变" + "新的接缝真的能用"**：

1. **行为不变**：离线假 LLM 驱动整条管线（提取 → 物化 → 修复循环 → 语料 → smoke_profile），
   断言产出与修复轮次符合预期——重构把嵌套闭包搬进类里，最容易在闭包捕获上出错；
2. **新接缝可用**：`on_progress` 回调（B2 的 SSE 要靠它）、`GenerateOptions`、
   `GenerateResult`、可注入的 `SectionExtractor`——这些是 B2/B3 的接口，必须现在钉住；
3. **CLI 真的变薄了**：提示词与管线函数不得再出现在 `scripts/import_story.py` 里，
   否则"提取到引擎侧"就是假的（下次又会有人从 CLI 里 import）。

全部用离线假 LLM，零成本、可重复。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from game_agent import worldgen
from game_agent.worldgen import (
    ALL_SECTIONS,
    GenerateOptions,
    OfflineLLM,
    SectionExtractor,
    WorldgenError,
)

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "import_story.py"

SOURCE = "# 测试素材\n\n主角在环形都市醒来，遇到诊所医生林。她冷淡但可靠。\n"


def _run(tmp_path: Path, *, with_corpus: bool = False, draft_only: bool = False,
         on_progress=None) -> worldgen.GenerateResult:
    """跑一次完整的离线生成（模块 API——B2/B3 将来走的就是这条）。"""
    src = tmp_path / "story.md"
    src.write_text(SOURCE, encoding="utf-8")
    text = worldgen.read_sources([str(src)], on_progress=on_progress)
    llm, tracker = worldgen.build_llm(offline=True)
    assert tracker is None  # 离线不落任何账本
    return worldgen.generate(
        tmp_path / "pack", text, llm,
        options=GenerateOptions(rounds=4, with_corpus=with_corpus, draft_only=draft_only),
        offline=True,
        on_progress=on_progress,
    )


# ---------------------------------------------------------------------------
# 1. 行为不变：整条管线
# ---------------------------------------------------------------------------


def test_offline_pipeline_produces_valid_pack(tmp_path):
    """离线跑通全流程，产出六个文件 + NPC 卡，且能通过 check-worldpack。"""
    result = _run(tmp_path)
    pack_dir = result.pack_dir
    for name in ("world.yaml", "schedule.yaml", "mainline.yaml",
                 "events.yaml", "endings.yaml", "smoke_profile.yaml"):
        assert (pack_dir / name).is_file(), f"缺少 {name}"
    assert list((pack_dir / "npcs").glob("*.yaml")), "缺少 NPC 卡"
    # 产出的包必须真的合法（这是整条管线的验收口径）
    assert worldgen.validate_pack(pack_dir) is None


def test_repair_loop_fires_and_converges(tmp_path):
    """修复循环必须**真的被触发过**并收敛。

    `OfflineLLM` 的设定是"前两次 npcs 返回空卡"——首轮必然缺 NPC 卡 →
    校验报"npcs/ 目录下没有任何角色卡" → `repair_sections` 路由到 npcs → 重生成 → 过。
    若断言 repairs == 0，说明修复路径根本没跑到（那样的"通过"没有意义）。
    """
    result = _run(tmp_path)
    assert result.repairs >= 1, "离线假 LLM 首轮必坏，修复轮次不该是 0"
    assert "validate" in result.stages


def test_draft_only_does_not_materialize(tmp_path):
    """`draft_only`：只落 draft.json，不写任何 YAML（作者确认理解无误才正式生成）。"""
    result = _run(tmp_path, draft_only=True)
    pack_dir = result.pack_dir
    assert (pack_dir / "draft.json").is_file()
    for name in ("world.yaml", "schedule.yaml", "mainline.yaml"):
        assert not (pack_dir / name).exists(), f"draft_only 不该写 {name}"
    # 草稿本身是完整可读的 JSON（作者要拿它核对故事理解）
    draft = json.loads((pack_dir / "draft.json").read_text(encoding="utf-8"))
    assert draft["world"]["name"] == "离线测试世界"
    assert "draft_only" in result.stages


def test_corpus_written_and_structurally_valid(tmp_path):
    """`with_corpus`：语料落盘且过结构底线（条数/id/NPC 引用/note/极性）。

    **条数是 25 不是 30**：离线假 LLM 每类只产 5 条对抗 + 10 条正常（真实路径才是
    6×3 + 6×2 = 30）。结构底线要求 ooc/setting/confab ≥5 且 normal ≥10，25 条刚好过——
    也就是说这条守卫测的是**结构判定**，不是"条数正好等于 30"。
    """
    result = _run(tmp_path, with_corpus=True)
    assert (result.pack_dir / "judge_corpus.yaml").is_file()
    assert result.corpus_written == 25
    assert worldgen.validate_corpus(result.pack_dir) is None
    assert "corpus" in result.stages


def test_corpus_progress_message_reports_actual_count(tmp_path):
    """进度里的条数必须是**实测值**——写死 "30 条" 而实际 25 条会让排查白跑一趟。"""
    events: list[dict] = []
    _run(tmp_path, with_corpus=True, on_progress=events.append)
    msgs = [e["message"] for e in events if e["stage"] == "corpus"]
    assert any("25 条结构通过" in m for m in msgs), msgs
    assert not any("30 条结构通过" in m for m in msgs), "日志不得写死条数"


def test_generate_result_exposes_summary(tmp_path):
    """结果对象要能直接被服务端渲染成人读摘要（`GenerateResult.summary`）。"""
    result = _run(tmp_path)
    s = result.summary
    assert "离线测试世界" in s and "林" in s and "初遇" in s


# ---------------------------------------------------------------------------
# 2. 新接缝：进度回调（B2 的 SSE 就建在它上面）
# ---------------------------------------------------------------------------


def test_progress_events_cover_every_stage(tmp_path):
    """进度事件必须覆盖各阶段——Web 工作台的"分块进度"直接吃这些事件。"""
    events: list[dict] = []
    _run(tmp_path, with_corpus=True, on_progress=events.append)

    stages = [e["stage"] for e in events]
    for expected in ("start", "extract", "repair", "validate", "corpus", "smoke", "done"):
        assert expected in stages, f"进度事件缺阶段 {expected}（实际 {sorted(set(stages))}）"
    # 分块粒度：每个块一条事件，服务端据此显示"生成[world] → 生成[npc1] → …"
    labels = [e.get("label") for e in events if e["stage"] == "extract"]
    assert "生成[world]" in labels and "生成[nodes]" in labels
    # 事件必须是可 JSON 序列化的纯数据（要过 SSE）
    json.dumps(events, ensure_ascii=False)


def test_progress_is_optional(tmp_path):
    """不传回调也要能跑（库里调用方不该被迫关心进度）。"""
    _run(tmp_path)  # on_progress 缺省 None
    assert worldgen.validate_pack(tmp_path / "pack") is None


def test_read_sources_emits_truncation_warning(tmp_path):
    """素材超长要发 warn 事件（而不是静默截断）——作者得知道自己给多了。"""
    big = tmp_path / "big.md"
    big.write_text("字" * (worldgen.MAX_SOURCE_CHARS + 100), encoding="utf-8")
    events: list[dict] = []
    text = worldgen.read_sources([str(big)], on_progress=events.append)
    assert len(text) == worldgen.MAX_SOURCE_CHARS
    assert any(e["stage"] == "warn" for e in events)


# ---------------------------------------------------------------------------
# 3. 纯工具函数的边界（它们决定报错质量，而不是功能有无）
# ---------------------------------------------------------------------------


def test_validate_name_rejects_path_and_odd_chars():
    assert worldgen.validate_name("good_name-1") is None
    for bad in ("", "../etc", "a/b", "名字", "a b", "a.b"):
        assert worldgen.validate_name(bad) is not None, f"不该接受 {bad!r}"


def test_read_sources_missing_file_raises_actionable_error(tmp_path):
    """素材不存在 → `WorldgenError`（库里不该 `SystemExit`），理由里带路径。"""
    with pytest.raises(WorldgenError) as ei:
        worldgen.read_sources([str(tmp_path / "nope.md")])
    assert "素材不存在" in str(ei.value) and "nope.md" in str(ei.value)


def test_read_sources_reads_directory(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.md").write_text("甲", encoding="utf-8")
    (d / "b.txt").write_text("乙", encoding="utf-8")
    (d / "c.json").write_text("{}", encoding="utf-8")  # 非 md/txt：不读
    text = worldgen.read_sources([str(d)])
    assert "甲" in text and "乙" in text and "c.json" not in text


def test_parse_json_handles_fences_and_truncation():
    assert worldgen.parse_json('{"a": 1}') == {"a": 1}
    assert worldgen.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert worldgen.parse_json('前言 {"a": 1} 后记') == {"a": 1}
    with pytest.raises(ValueError, match="截断"):
        worldgen.parse_json('{"a": 1')  # 未闭合 = 被输出上限截断
    with pytest.raises(ValueError, match="输出为空"):
        worldgen.parse_json("   ")
    # 非对象（数组/标量）：报"没有 JSON 对象"。它**不是**"截断"——
    # 两者的修复指令不同（前者让模型改结构、后者让它精简），路由错了会白烧一轮。
    with pytest.raises(ValueError, match="没有 JSON 对象"):
        worldgen.parse_json("[1, 2]")


def test_diagnose_matches_failure_shape():
    """三种失败各给一句可执行提醒（这段文本会喂回模型，措辞即修复策略）。"""
    assert "没有输出任何内容" in worldgen.diagnose("")
    assert "截断" in worldgen.diagnose('{"a": 1')
    assert "不是合法 JSON" in worldgen.diagnose("随便说点什么")


def test_repair_sections_routes_by_error_keyword():
    """错误关键词 → 需要重生成的小块。路由错了会"修一个坏另一个"。"""
    assert worldgen.repair_sections("缺少文件: world.yaml") == ["world", "world_extra"]
    assert worldgen.repair_sections("npcs/ 目录下没有任何角色卡") == ["npcs"]
    assert worldgen.repair_sections("行动 'x' 的 check.stat 未声明") == ["schedule", "actions"]
    assert worldgen.repair_sections("主线节点 'n1' 的 completion 不可达") == ["nodes"]
    assert worldgen.repair_sections("事件 'e1' 的 chance 越界") == ["events"]
    assert worldgen.repair_sections("结局 when 引用未声明 flag") == ["endings"]
    # 认不出关键词 → 宽范围重来（宁可多花，也不能装作修好了）
    assert worldgen.repair_sections("某种没见过的错误") == [
        "schedule", "actions", "nodes", "events", "endings",
    ]


def test_smoke_profile_shape(tmp_path):
    result = _run(tmp_path)
    # smoke_profile 是 **YAML**（`worldpack_smoke.py` 用 yaml.safe_load 读它）
    prof = yaml.safe_load((result.pack_dir / "smoke_profile.yaml").read_text(encoding="utf-8"))
    assert prof["picks"] == {"help": 0}
    assert prof["action"] == "work"
    assert prof["days"] == 8
    assert prof["forbidden_scan"]  # 禁表扫词不能为空，否则冒烟等于没扫
    assert len(prof["lines"]) == 3


def test_forbidden_tokens_strips_parenthetical_and_enumeration():
    """禁表词条里的括注与顿号枚举要拆开（"魔法（含仙术）、手机" → 三个词）。"""
    toks = worldgen.forbidden_tokens_from(["魔法（含仙术）、手机", "现代武器等"])
    assert "魔法" in toks and "含仙术" in toks and "手机" in toks
    assert "现代武器" in toks  # 去掉尾部的"等"
    assert "" not in toks


def test_extractor_accepts_injected_llm(tmp_path):
    """`SectionExtractor` 可注入（B2 要能在后台线程里构造它并喂自己的 LLM）。"""
    ext = SectionExtractor(OfflineLLM(), SOURCE, offline=True)
    draft = ext.generate_sections()
    assert set(draft) >= {"world", "npcs", "schedule", "mainline", "events", "endings"}


def test_all_sections_constant_is_the_full_set():
    """全量提取的块集合必须与"修复路由可能返回的块"用同一套名字。"""
    assert set(ALL_SECTIONS) == {
        "world", "world_extra", "npcs", "schedule", "actions", "nodes", "events", "endings",
    }
    routed = set(worldgen.repair_sections("某种没见过的错误"))
    assert routed <= set(ALL_SECTIONS)


# ---------------------------------------------------------------------------
# 4. CLI 真的变薄了（E-5 的验收口径）
# ---------------------------------------------------------------------------


def test_cli_is_a_thin_shell():
    """提示词与管线函数**不得**再出现在 CLI 里。

    否则"管线已提取到引擎侧"只是说法——下一个人仍会从 CLI 里 import，
    而 CLI 一旦被 import 就会执行参数解析路径。
    """
    src = CLI.read_text(encoding="utf-8")
    for leaked in (
        "EXTRACT_WORLD_SYSTEM =",
        "CORPUS_ADV_ONE_SYSTEM =",
        "def _materialize",
        "def _repair_sections",
        "def _smoke_profile",
        "def generate_sections",
    ):
        assert leaked not in src, f"CLI 里仍有管线代码：{leaked}"
    assert "from game_agent import worldgen" in src  # 而是委托给它


def test_cli_offline_run_end_to_end(tmp_path):
    """CLI 契约：`--offline` 一条命令跑通，退出码 0，产出可校验的包。

    （真实执行的端到端，不是 import 一下就算——CLI 是作者唯一的入口。）
    """
    src = tmp_path / "story.md"
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(CLI), str(src), "--name", "cli_probe",
         "--pack-dir", str(out), "--offline", "--with-corpus"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )
    assert r.returncode == 0, f"CLI 退出码 {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "check-worldpack 通过" in r.stdout
    assert "25 条结构通过" in r.stdout  # 离线假 LLM 的实际条数（真实路径是 30）
    # 结束语只出现一次（事件与 CLI 各打印一遍的话，终端会看到两遍）
    assert r.stdout.count("世界包已生成") == 1
    assert worldgen.validate_pack(out / "cli_probe") is None


def test_cli_rejects_bad_name_and_missing_source(tmp_path):
    """两个前置错误各自给出非零退出码与可读理由（作者最常撞的两下）。"""
    src = tmp_path / "story.md"
    src.write_text(SOURCE, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(CLI), str(src), "--name", "../escape", "--offline",
         "--pack-dir", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )
    assert r.returncode == 1 and "非法包名" in r.stdout

    r2 = subprocess.run(
        [sys.executable, str(CLI), str(tmp_path / "nope.md"), "--name", "ok", "--offline",
         "--pack-dir", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )
    assert r2.returncode == 1 and "素材不存在" in r2.stdout

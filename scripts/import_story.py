"""B：素材导入工具（CLI 外壳）——作者上传小说/大纲/设定，生成可过质量门的世界包。

用法：
  uv run python scripts/import_story.py <素材文件...> --name my_world [选项]

**本文件现在是薄壳**（2026-10，E-5）：生成管线已提到 `game_agent/worldgen.py`
（上游 `docs/plan-creator-player.md` §2 缺口 G3）。这里只负责三件 CLI 才该管的事：

1. **参数解析与输入校验**（包名、素材路径、API Key 前置检查）；
2. **把进度打成终端输出**——管线本身不 `print`，它发 `on_progress` 事件，
   这里把它渲染成人读的一行行（Web 工作台则把它转成 SSE，同一份事件两种消费）；
3. **`--live` 的真机门禁编排**——那要 shell 出去跑 `judge_sensitivity.py` /
   `worldpack_smoke.py`，属于运维编排而不是"生成"，故留在 CLI。

流程（素材 → 过检世界包）：
  ① LLM 提取（**粒度原子化**：每个调用只生成一小块——实测 deepseek-v4-flash 思考模式下，
     大任务提示词会触发无界思考烧光输出预算；Judge 类小任务 300+ 次零失败，故每调用
     都切成 Judge 粒度：≤2000 预算 + 短提示词 + temp 0 + 纯文本）；
  ② 物化 + 校验-修复循环：写 YAML → load_worldpack 校验，错误按关键词路由到相关小块
     重生成（≤4 轮，每轮只碰相关块）；
  ③ 语料生成（--with-corpus）：30 条 Judge 语料（对抗/正常分两次调用）+ 结构校验；
  ④ 冒烟 profile：生成 <包>/smoke_profile.yaml（worldpack_smoke.py 自动读取）；
  ⑤ 可选 --live：顺跑 E1 Judge 门禁 + 真机冒烟（约 ¥1-2）。

选项：
  --name      包目录名（必填，只允许字母/数字/_/-）
  --pack-dir  输出根目录（默认 world-packs）
  --rounds    校验-修复最大轮数（默认 4）
  --with-corpus   生成 Judge 语料
  --live      生成后立即跑真机门禁（需 --with-corpus 与 API Key）
  --draft-only    只输出提取草稿 JSON（作者作者确认用），不物化
  --offline   离线回归模式：内嵌"坏草稿→好草稿"假 LLM，测物化/修复/语料结构管线
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from game_agent import worldgen
from game_agent.config import load_settings
from game_agent.judge_corpus import load_corpus
from game_agent.worldgen import GenerateOptions, WorldgenError


# ---------------------------------------------------------------------------
# --live 的门禁编排（只有 CLI 需要；服务端将来走独立的"门禁按钮"）
# ---------------------------------------------------------------------------


def _failed_cases(report: dict) -> tuple[list, list, list]:
    """从门禁报告挑出需要修的用例：漏判 / 误报 / **不可判定**。

    `hit is None` = 该用例无法判定（判官空响应或截断，升级重试后仍不可用）。
    它既不是"漏判"也不是"误报"——不得据此改写语料（会把好用例改坏），
    单独返回由调用方报数并中止自动修复。
    """
    cases = report.get("cases", [])
    missed = [c for c in cases if c["category"] != "normal" and c.get("hit") is False]
    fps = [c for c in cases if c["category"] == "normal" and c.get("hit") is True]
    unknown = [c for c in cases if c.get("hit") is None]
    return missed, fps, unknown


def _latest_report(pack_name: str) -> dict | None:
    candidates = sorted(
        Path("reports").glob("judge_sensitivity_*.json"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("pack") == pack_name:
            return data
    return None


def _run_gate(pack_dir: Path) -> bool:
    r = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("judge_sensitivity.py")),
         "--pack", str(pack_dir)],
        check=False,
    )
    return r.returncode == 0


def _live_gate(pack_dir: Path, ext: worldgen.SectionExtractor, draft: dict) -> int:
    """E1 Judge 门禁 + 真机冒烟，含门禁驱动的语料修复（最多 2 轮）。返回退出码。"""
    print("\n===== 真机质量门 =====")
    corpus_summary = json.dumps({
        "world": {k: draft["world"].get(k) for k in ("name", "era", "forbidden", "player_role", "player_goal")},
        "npcs": {k: {f: v.get(f) for f in ("name", "identity", "personality", "speech_style", "boundaries", "forbidden", "affection_stages")}
                 for k, v in draft.get("npcs", {}).items()},
    }, ensure_ascii=False)

    gate_ok = _run_gate(pack_dir)
    for repair_round in range(2):
        if gate_ok:
            break
        report = _latest_report(draft["world"].get("name"))
        if report is None:
            print("[✗] 门禁失败且未找到门禁报告（无法自动修复语料）")
            return 1
        missed, fps, unknown = _failed_cases(report)
        if not missed and not fps:
            if unknown:
                print(
                    f"[✗] 门禁未通过，但有 {len(unknown)} 条用例**不可判定**"
                    f"（未知 ≠ 失败）——请重跑门禁或检查判官可用性，不自动改语料"
                )
            else:
                print("[✗] 门禁未通过但无逐案失败信息（判据问题，非语料）")
            return 1

        def fix_text(cases: list, kind: str) -> str:
            lines = []
            for c in cases:
                last = next(
                    (r["verdict"] for r in reversed(c.get("rounds", [])) if r.get("verdict")),
                    "（判定为空）",
                )
                lines.append(f"- {c['id']}（{c['category']}）：{last[:100]}")
            return (
                f"<门禁失败用例>\n" + "\n".join(lines) + "\n</门禁失败用例>\n"
                f"这些用例被 Judge {kind}——请重写为更明确、无歧义的版本。"
            )

        print(f"[语料修复轮 {repair_round + 1}] 漏判 {len(missed)} 条 / 误报 {len(fps)} 条")
        rebuilt: dict = {}
        for cat in ("ooc", "setting", "confab"):
            bad = [c for c in missed if c["category"] == cat]
            if bad:
                rebuilt[cat] = ext.extract_json(
                    worldgen.CORPUS_ADV_ONE_SYSTEM.format(cat=cat, desc=worldgen.CORPUS_CAT_DESC[cat]),
                    f"<世界包摘要>\n{corpus_summary}\n</世界包摘要>\n\n"
                    + fix_text(bad, "漏判（应拦却放过）")
                    + f"\n请重写这 {len(bad)} 条 {cat} 类对抗语料（输出 {{\"cases\": [...]}}）。",
                    max_tokens=worldgen.EXTRACT_MAX_TOKENS, purpose=f"import_corpus_fix_{cat}",
                    label=f"语料修复[{cat}]",
                )
        if fps:
            rebuilt["normal"] = ext.extract_json(
                worldgen.CORPUS_NORMAL_ONE_SYSTEM,
                f"<世界包摘要>\n{corpus_summary}\n</世界包摘要>\n\n"
                + fix_text(fps, "误判为违规（应放行）")
                + f"\n请重写这 {len(fps)} 条正常语料（输出 {{\"cases\": [...]}}）。",
                max_tokens=worldgen.EXTRACT_MAX_TOKENS, purpose="import_corpus_fix_normal",
                label="语料修复[normal]",
            )
        old = load_corpus(pack_dir)
        fixed_cases = [c for c in old if c.id not in {b["id"] for b in (missed + fps)}]
        for _cat, batch in rebuilt.items():
            fixed_cases += batch.get("cases", [])

        # 结构校验后落盘（修复批是 dict，旧用例是 JudgeCase——统一转 dict）
        def _as_dict(c) -> dict:
            return c if isinstance(c, dict) else vars(c)

        worldgen.write_corpus(pack_dir, {"cases": [_as_dict(c) for c in fixed_cases]})
        err = worldgen.validate_corpus(pack_dir)
        if err is not None:
            print(f"[✗] 语料修复后结构仍不达标：{err}（请手工补充）")
            return 1
        gate_ok = _run_gate(pack_dir)
    if not gate_ok:
        print("[✗] E1 Judge 门禁在自动修复后仍未通过（手工修语料或重跑）")
        return 1

    print("----- 真机冒烟 -----")
    r = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("worldpack_smoke.py")),
         "--pack", str(pack_dir)],
        check=False,
    )
    if r.returncode != 0:
        print("[✗] 真机冒烟未通过（退出码 1）")
        return 1
    print("[✓] 真机质量门通过（Judge 门禁 + 真机冒烟）")
    return 0


# ---------------------------------------------------------------------------


def _printer(quiet_stages: set[str] | None = None):
    """把管线的进度事件渲染成终端输出（事件驱动 → 人读文本）。

    `quiet_stages` 用于抑制"CLI 自己会用别的方式输出"的阶段——目前只有 `done`：
    结束语由本文件带前导空行打印（与重构前的输出逐字一致），若也放行事件里的那句，
    终端就会看到两遍"世界包已生成"。**事件本身不发前导空行**（那是给人读的排版，
    SSE 消费方不需要），所以两边的职责必须分清。
    """
    skip = quiet_stages or set()

    def on_progress(ev: dict) -> None:
        if ev.get("stage", "") in skip:
            return
        print(ev.get("message", ""))

    return on_progress


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="素材导入：小说/大纲/设定 → 世界包（工具 B）")
    parser.add_argument("sources", nargs="+", help="素材文件（txt/md）或目录")
    parser.add_argument("--name", required=True, help="包目录名（字母/数字/_/-）")
    parser.add_argument("--pack-dir", default="world-packs", help="输出根目录")
    parser.add_argument("--rounds", type=int, default=4, help="校验-修复最大轮数")
    parser.add_argument("--with-corpus", action="store_true", help="生成 30 条 Judge 语料")
    parser.add_argument("--live", action="store_true", help="生成后立即跑真机门禁（需 API Key）")
    parser.add_argument("--draft-only", action="store_true", help="只输出草稿 JSON，不物化")
    parser.add_argument("--offline", action="store_true", help="离线回归模式（内嵌假 LLM）")
    args = parser.parse_args(argv)

    if (bad := worldgen.validate_name(args.name)) is not None:
        print(f"[✗] {bad}")
        return 1
    pack_dir = Path(args.pack_dir) / args.name

    try:
        source_text = worldgen.read_sources(args.sources, on_progress=_printer())
    except WorldgenError as e:
        print(f"[✗] {e}")
        return 1

    if not args.offline and not load_settings().has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（离线回归请加 --offline）")
        return 1

    llm, tracker = worldgen.build_llm(args.offline)
    # `done` 由本文件自己打印（带前导空行，与重构前逐字一致），故抑制事件里的那一句
    on_progress = _printer(quiet_stages={"done"})

    try:
        result = worldgen.generate(
            pack_dir, source_text, llm,
            options=GenerateOptions(
                rounds=args.rounds,
                with_corpus=args.with_corpus,
                draft_only=args.draft_only,
            ),
            offline=args.offline,
            on_progress=on_progress,
        )
    except WorldgenError as e:
        print(f"[✗] {e}")
        return 1

    # 草稿确认模式（作者看完摘要再决定要不要正式生成）
    if args.draft_only:
        print("[草稿] 提取摘要（请作者确认故事理解无误）：")
        print(result.summary)
        return 0

    print("\n[✓] 世界包已生成 → " + str(pack_dir))
    print("提取摘要（请作者确认故事理解无误）：")
    print(result.summary)

    if tracker is not None:
        print("\n" + tracker.cost_report())

    if args.live:
        if not args.with_corpus:
            print("[✗] --live 需要 --with-corpus（先有语料才有门禁）")
            return 1
        ext = worldgen.SectionExtractor(llm, source_text, on_progress=on_progress)
        rc = _live_gate(pack_dir, ext, result.draft)
        if rc != 0:
            return rc

    print("\n下一步（作者可自行复查）：")
    print("  uv run pytest")
    print(f"  uv run python -m game_agent play {pack_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

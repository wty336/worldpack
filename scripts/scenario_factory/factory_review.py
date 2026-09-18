"""出库人读清单：把 JSONL 物证渲染成**人真能读**的 Markdown（2026-09-13）。

**为什么需要**：出库物证是 JSONL —— 机器好读、人难读。而本项目里**只有人能判**的东西不少：
决策 16 的 confab 断言是否"由材料已有事实组合推出"、标签是否**被当成本身所述的那种东西**成立、
叙事是否像人写的。规格预演阶段这些都是我一个人在小批量上看，量一上来（一次 1000 条）
就没有可读的入口了。

**版面按"风险优先级"排**（不是按 id 顺排）：被标签自检点名的、温和元词命中的、近误人名、
长度没兑现的**先顶到最前面**——人读的注意力有限，必须落在最可能出问题的那几条上；
全量索引与逐条详情在后面，供抽查。

用法::

    python -m scripts.factory_review --layer train --module extract
    python -m scripts.factory_review --layer train --module judge --out reports/judge-review.md
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib

from game_agent.evalmeta import file_digest

from .cards import REPO_ROOT
from . import worklog

# 正常样本（负例）的模块名（决策 37）。与 `assemble.NORMAL_MODULE` 同值——
# 这里**有意不 import assemble**：那个模块在导入期就把 LLM 客户端/演绎器拉起来，
# 而清单渲染是纯本地文字活（`docs/factory-review` 的用法是随时可跑、离线可跑）。
NORMAL_MODULE = "judge_normal"

DATA_ROOT = REPO_ROOT / "data" / "route-a"
MODULES = ("extract", "judge", "compress")


def _rel(p: pathlib.Path) -> str:
    """人类可读路径：仓库内给相对路径，否则原样给绝对路径。

    **不能崩**：`--out` 指到仓库外（临时目录、另一块盘）是完全合法的用法，
    而 `Path.relative_to` 在那种情况下抛 `ValueError` —— 清单是给人看的，
    不该因为路径前缀不同就写不出来（守卫 `test_judge_and_compress_shapes` 抓到的就是这个）。
    """
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def load_rows(out_dir: pathlib.Path, layer: str, module: str) -> list[dict]:
    f = out_dir / layer / f"{module}.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def _source_files(out_dir: pathlib.Path, layer: str, module: str) -> list[pathlib.Path]:
    """这份清单的行来自哪些文件（judge 含正常样本那个附加模块）。

    清单头部必须**逐个列出并附 sha256** —— 否则 221 条负例的来源在清单里没有留痕，
    而"清单自证来源"正是这个模块存在的理由之一（决策 37 引入第二个文件后新加）。
    """
    files = [out_dir / layer / f"{module}.jsonl"]
    if module == "judge":
        files.append(out_dir / layer / f"{NORMAL_MODULE}.jsonl")
    return files


def load_module_rows(out_dir: pathlib.Path, layer: str, module: str) -> list[dict]:
    """出库行；**judge 一并带上正常样本**（负例是同一个人读面）。

    `judge.jsonl` 与 `judge_normal.jsonl` 是**两个模块文件**（决策 37：负例走独立附加模块，
    免得稀释缺陷侧的家族配额分母），但人读清单**只有一份** —— 正常样本恰恰是误报率那一侧，
    最需要人眼（门禁判"通过"而人判"其实有毛病"，正是要找的标注错误）。漏掉它们
    等于把 ① 补的那一课排除在复核之外。
    """
    rows = load_rows(out_dir, layer, module)
    if module == "judge":
        rows += load_rows(out_dir, layer, NORMAL_MODULE)
    return rows


def label_violations(out_dir: pathlib.Path, layer: str) -> dict[str, list[str]]:
    """被标签自检点名的条目（**报告-only** 的证据文件，格式兼容旧版单 dict）。"""
    f = out_dir / layer / f"label-check-{layer}.json"
    if not f.exists():
        return {}
    data = json.loads(f.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for run in (data if isinstance(data, list) else [data]):
        for v in run.get("violations", []):
            out.setdefault(v["id"], []).extend(v.get("items", []))
    return out


def dropped(out_dir: pathlib.Path, layer: str) -> dict | None:
    f = out_dir / layer / f"dropped-{layer}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _body(row: dict) -> str:
    """样本的"表面文本"（三模块各有落点，口径同 `assemble._sample_text` 的人读版）。"""
    if row.get("module") in ("judge", "judge_normal"):   # 正常样本（负例）与 judge 同形
        return row.get("narration", "")
    if row.get("module") == "compress":
        return row.get("output", "")
    return row.get("input", "").split("<回合内容>")[-1].replace("</回合内容>", "").strip()


def money_crossovers(rows: list[dict]) -> list[tuple[str, str, list[str]]]:
    """货币穿帮（发现⑩-C）：文本里出现**别的题材**的金额词（标签 + 叙事都查）。

    按**词**去重而不是按题材：仙侠与古代武侠共用「五十两」，按题材去重会把它们互相判成穿帮
    （核对脚本的第一版就是这样，33 条全是假阳性）。
    """
    from .cards import _MONEY_TERM_BY_GENRE

    out = []
    for r in rows:
        own = _MONEY_TERM_BY_GENRE.get(r.get("genre"))
        if not own:
            continue
        text = " ".join(x["text"] for x in r.get("labels", [])) + "\n" + _body(r)
        hit = sorted({t for g, terms in _MONEY_TERM_BY_GENRE.items() if g != r.get("genre")
                      for t in (terms[0],) if t != own[0] and t in text})
        if hit:
            out.append((r["id"], r.get("genre", "?"), hit))
    return out


def qa_stats(rows: list[dict]) -> dict:
    """数据集级 QA 统计（**零成本、确定性**）——放进出库清单，省得每次另写一次性脚本核对。

    这几项都曾是"验收时临时写脚本才发现"的（货币穿帮 20% / 元词命中 / 长度未兑现），
    写进清单后每批自动带出来。
    """
    n_soft = sum(1 for r in rows if r.get("meta_soft"))
    n_conf = sum(1 for r in rows if r.get("name_confusables"))
    short = [r for r in rows if r.get("target_tokens")
             and r.get("realized_chars", 0) < r["target_tokens"] * 0.5]
    from collections import Counter

    return {
        "meta_soft": n_soft,
        "name_confusables": n_conf,
        "money_crossovers": money_crossovers(rows),
        "length_tiers": Counter(r.get("target_tokens") for r in rows),
        "short_of_target": [r["id"] for r in short],
    }


def flags(row: dict, viol: dict[str, list[str]]) -> list[str]:
    """一条样本的**风险观察项**（只报告不判——判定是人读的事）。

    ⚠️ **负例不进这里**：负例（该回合无新事实）是卡面的**正常设计**（正负例 4:1），
    把它当风险项会把 §2 淹没掉 —— 首版就是这样：64 条"风险"里 40 条是负例，
    真正的 1 条标签自检点名彻底看不见（user 一眼看出）。负例在索引的「正/负」列与详情标题上标。
    """
    out = []
    if row["id"] in viol:
        out.append("⚠ 标签自检点名")
    if row.get("meta_soft"):
        out.append("温和元词: " + "/".join(row["meta_soft"]))
    if row.get("name_confusables"):
        out.append("近误人名: " + "/".join(row["name_confusables"]))
    if row.get("target_tokens") and row.get("realized_chars", 0) < row["target_tokens"] * 0.5:
        out.append(f"长度未兑现 {row['realized_chars']}/{row['target_tokens']}")
    if row.get("over_target"):
        out.append("超提示词目标")
    return out


def _kind(row: dict) -> str:
    return "负例" if row.get("expect_empty") else "正例"


def _labels_md(row: dict) -> str:
    return "\n".join(f"- `{x['type']}` **{x['text']}**（重要度 {x['importance']}）"
                     for x in row.get("labels", [])) or "（无标签 —— 负例：该回合不得引入新事实）"


def _detail_md(row: dict, viol: dict[str, list[str]]) -> str:
    mod = row.get("module")
    lines = [f"### `{row['id']}` · {row.get('genre', '?')}"
             + ("　**负例**" if row.get("expect_empty") else "") + "\n"]
    fl = flags(row, viol)
    if fl:
        lines.append("> 观察项：" + "；".join(fl) + "\n")
    if mod == "extract":
        lines.append(f"**已有事实**：{'；'.join(row.get('existing') or []) or '（无）'}\n")
        lines.append("**标签**：\n" + _labels_md(row) + "\n")
    elif mod in ("judge", "judge_normal"):
        lines.append(f"**问题类型**：{row.get('expect', '?')}　**矛盾依据**：{row.get('detail', '')}\n")
        lines.append(f"**说话人角色卡**：\n\n```\n{row.get('speaker_card', '')}\n```\n")
        lines.append(f"**材料**：\n\n```\n{row.get('material', '')}\n```\n")
    else:
        lines.append("**要点（须在摘要中保住）**："
                     + "；".join(p["text"] for p in row.get("preserve_points", [])) + "\n")
    title = {"extract": "回合文本", "judge": "叙事", "judge_normal": "叙事（正常样本）",
             "compress": "摘要"}[mod]
    lines.append(f"**{title}**（{row.get('realized_chars', len(_body(row)))} 字 / "
                 f"卡面目标 {row.get('target_tokens', '?')} token）：\n")
    lines.append("```\n" + _body(row).strip() + "\n```\n")
    if row["id"] in viol:
        lines.append("> 标签自检判**不成立**的项：" + "；".join(viol[row["id"]]) + "\n")
    return "\n".join(lines)


def render(out_dir: pathlib.Path, layer: str, module: str,
           rows: list[dict] | None = None, *, full_name: str = "") -> str:
    """清单主体：总览 → 风险项 → 丢弃明细 → 全量索引 → 标签总表。

    **全文（逐条叙事）拆到附录文件**：200 条的全文是 900 KB / 15000 行，
    与"扫一眼看有没有问题"是两种读法 —— 合成一份会让人读不动（首版就是合成的一份）。
    """
    rows = load_module_rows(out_dir, layer, module) if rows is None else rows
    viol = label_violations(out_dir, layer)
    drop = dropped(out_dir, layer)
    f = out_dir / layer / f"{module}.jsonl"
    head = worklog.read_journal(worklog.journal_path(out_dir, layer, module)).header or {}
    flagged = [r for r in rows if flags(r, viol)]
    n_neg = sum(1 for r in rows if r.get("expect_empty"))
    labels = [(r["id"], x) for r in rows for x in r.get("labels", [])]
    qa = qa_stats(rows)

    srcs = [(f, file_digest(f)) for f in _source_files(out_dir, layer, module) if f.exists()]
    src = ("、".join(f"`{_rel(p)}`（sha256 `{d[:16]}`）" for p, d in srcs) if srcs
           else "（行由调用方直接传入）")
    md = [f"# 出库人读清单：{layer} / {module}\n",
          f"- 生成于 {datetime.date.today().isoformat()}；来源 {src}；**{len(rows)} 条**",
          f"- 产线指纹 `{head.get('factory_version', '（无日志头）')}`"
          f"；模型 {head.get('models', '?')}；采样档 {head.get('sampling', '?')}",
          (f"- 全文（逐条叙事）：`{full_name}`\n" if full_name else ""),
          "## 1. 总览\n",
          f"- 题材：{'、'.join(f'{g} {n}' for g, n in _counts(rows, 'genre'))}",
          f"- 正/负例：**正例 {len(rows) - n_neg} / 负例 {n_neg}**"
          f"（负例 = 该回合**不得**引入新事实，看它有没有偷偷加；负例是正常设计，不算风险项）",
          f"- 风险项命中：**{len(flagged)}** 条（见 §2）；标签自检点名："
          f"{len([r for r in rows if r['id'] in viol])} 条；fact 标签共 {len(labels)} 条（见 §5）",
          f"- QA 统计（零成本确定性核对）：温和元词 {qa['meta_soft']} 条；近误人名 "
          f"{qa['name_confusables']} 条；**货币穿帮 {len(qa['money_crossovers'])} 条**；"
          f"长度档位 {dict(qa['length_tiers'])}，实测不足目标一半 {len(qa['short_of_target'])} 条",
          f"- 丢弃：{('本层 0 丢弃（无 dropped 留档）' if not drop else _drop_line(drop))}\n",
          "## 2. 先看这些（风险优先级）\n"]
    if not flagged:
        md.append("（无 —— 本批没有风险项命中的样本）\n")
    for r in flagged:
        md.append(f"- `{r['id']}` · {r.get('genre', '?')}：{'；'.join(flags(r, viol))}")
    md.append("\n## 3. 丢弃明细\n")
    if not drop:
        md.append("本层 **0 丢弃**：没有卡被门禁丢掉，也没有质检剔除（`dropped-%s.json` 不存在）。\n" % layer)
    else:
        md.append("| 原因 | 条数 | 卡 id |\n| --- | --- | --- |")
        for reason, ids in sorted(drop.get("ids", {}).items(), key=lambda kv: -len(kv[1])):
            detail = drop.get("detail", {}).get(reason, {})
            md.append(f"| {reason} | {len(ids)} | " + "；".join(
                f"`{i}`（{detail.get(i, '')}）" for i in ids[:8])
                + ("…" if len(ids) > 8 else "") + " |")
        md.append("")
    md.append("\n## 4. 全量索引\n")
    md.append("| id | 题材 | 正/负 | 标签数 | 字数 | 风险项 |\n| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        md.append(f"| `{r['id']}` | {r.get('genre', '?')} | {_kind(r)} | {len(r.get('labels', []))}"
                  f" | {r.get('realized_chars', len(_body(r)))}"
                  f" | {'；'.join(flags(r, viol)) or '—'} |")
    md.append("\n## 5. 标签总表\n")
    if not labels:
        md.append("（本模块无 fact 标签）\n")
    else:
        md.append("| id | 类型 | 标签文本 | 重要度 |\n| --- | --- | --- | --- |")
        for rid, x in labels:
            md.append(f"| `{rid}` | {x['type']} | {x['text']} | {x['importance']} |")
    return "\n".join(md) + "\n"


def render_full(out_dir: pathlib.Path, layer: str, module: str,
                rows: list[dict] | None = None) -> str:
    """附录：逐条全文（供抽查）。"""
    rows = load_module_rows(out_dir, layer, module) if rows is None else rows
    viol = label_violations(out_dir, layer)
    md = [f"# 出库全文：{layer} / {module}（{len(rows)} 条）\n",
          "> 这是**附录**（逐条叙事全文）；总览/风险项/索引/标签总表在清单主体里。\n"]
    for r in rows:
        md.append(_detail_md(r, viol))
        md.append("---\n")
    return "\n".join(md) + "\n"


def _counts(rows: list[dict], key: str) -> list[tuple[str, int]]:
    from collections import Counter

    return Counter(r.get(key, "?") for r in rows).most_common()


def _drop_line(drop: dict) -> str:
    return f"{sum(drop.get('counts', {}).values())} 条 —— " + "、".join(
        f"{k} {v}" for k, v in sorted(drop.get('counts', {}).items(), key=lambda kv: -kv[1]))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="出库人读清单（JSONL → Markdown）")
    p.add_argument("--layer", required=True, choices=["train", "dev", "eval"])
    p.add_argument("--module", default="extract", choices=list(MODULES))
    p.add_argument("--out", default=str(DATA_ROOT))
    p.add_argument("--md", default=None, help="输出路径（缺省 reports/<module>-review-<layer>-<date>.md）")
    args = p.parse_args(argv)

    out_dir = pathlib.Path(args.out)
    rows = load_module_rows(out_dir, args.layer, args.module)
    if not rows:
        print(f"[✗] {out_dir}/{args.layer}/{args.module}.jsonl 不存在或为空")
        return 1
    dest = pathlib.Path(args.md) if args.md else (
        REPO_ROOT / "reports" /
        f"{args.module}-review-{args.layer}-{datetime.date.today():%Y%m%d}.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    full = dest.with_name(dest.stem + "-full" + dest.suffix)
    dest.write_text(render(out_dir, args.layer, args.module, rows, full_name=_rel(full)),
                    encoding="utf-8")
    full.write_text(render_full(out_dir, args.layer, args.module, rows), encoding="utf-8")
    print(f"[✓] {_rel(dest)}（{len(rows)} 条，{dest.stat().st_size / 1024:.0f} KB）"
          f" + 全文 {_rel(full)}（{full.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

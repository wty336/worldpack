"""跨类泄漏探针：正常样本（负例）与缺陷样本之间，有没有"不用读叙事就能判"的表面规律。

**为什么需要**：决策 36 是**偶然**发现的 —— confab 卡的材料整段丢掉「关键事实」段，
于是规则"材料没有事实段 ⇒ 虚构事实"在 train 上 **457/457 成立、零误报**。
这类缺陷的形状是：*某个表面特征近乎 100% 决定标签，而生产数据里没有这个特征* ——
训练时完全看不出来，评测时表现为"判官不读叙事"。它不该靠运气再被发现第二次。

**探针**（全部零 LLM 成本、纯本地）：

- **① 单特征可分性**：任一**二元表面特征**若能把两类分开 ≥`LEAK_RULE_MAX`（90%），
  就等于把答案写在了题面上。报出最强的那个特征与它的平衡规则准确率。
  在这个样本量下（每类 ≥200），比例的标准误约 3% ⇒ 90% 远在噪声之外。
- **② 材料结构指纹**：各类"含哪些段"的分布必须一致（决策 36 的原始形态）。
- **③ 同材料跨标签**：同一份材料同时挂着两类标签的组数 —— >0 说明**材料不可能决定标签**
  （天然对照），0 组则提示每条材料的标签都是"一一对应"，需人看一眼。
- **④ 长度分布**：字数中位与卡面兑现率中位（差 >2 倍 ⇒ "长/短 ⇒ 某类"）。
- **⑤ 元叙述**：叙事里议论材料本身（"材料里/场景卡/占位"）的比例，只报数不判。

**不做的事**：不拿"训练个小分类器看准不准"当判据。朴素贝叶斯在本数据上被**类别量比**
污染（缺陷占 83% ⇒ 缺陷侧词表更全，而平滑常数的实现细节会系统性偏向多数类），
实测给出"叙事也判不出类别"的**假结论**（52%）。宁可少一个指标，不要一个方向错的指标。
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
from collections import Counter, defaultdict

from .cards import REPO_ROOT

NORMAL_MODULE = "judge_normal"
DEFECT_MODULE = "judge"
LEAK_RULE_MAX = 0.90      # 单特征规则准确率上限：≥ 这个值即"题面写着答案"
LEN_RATIO_MAX = 2.0       # 两类字数中位之比上限

# 候选二元表面特征（**只看模型真能看到的东西**：材料与叙事；不看 labels/detail 等答案字段）
_MATERIAL_FEATS = {
    "材料含「关键事实」": lambda m, n: "关键事实" in m,
    "材料含 <scene>": lambda m, n: "<scene>" in m,
    "材料含 <identity>": lambda m, n: "<identity>" in m,
    "材料含 <agent_status>": lambda m, n: "<agent_status>" in m,
    "材料含 <在场角色>": lambda m, n: "<在场角色>" in m,
    "材料分节数≥1": lambda m, n: m.count("\n## ") >= 1,
    "材料长度<300 字": lambda m, n: len(m) < 300,
}
_NARRATION_FEATS = {
    "叙事含「」": lambda m, n: "「" in n,
    "叙事含『』": lambda m, n: "『" in n,
    "叙事含空行分段": lambda m, n: "\n\n" in n,
    "叙事长度<400 字": lambda m, n: len(n) < 400,
    "叙事含省略号…": lambda m, n: "…" in n,
}
META_RE = re.compile(r"占位|材料里|材料中|场景卡|角色卡里|设定文档")


def load_family(layer_dir: pathlib.Path) -> tuple[list[dict], list[dict]]:
    """读一层的（缺陷，正常）两组。缺文件即空 —— 探针不能因为"该层还没产负例"就崩。"""
    def read(name: str) -> list[dict]:
        f = layer_dir / f"{name}.jsonl"
        if not f.exists():
            return []
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]

    return read(DEFECT_MODULE), read(NORMAL_MODULE)


def _rule_strength(pos: list[bool], neg: list[bool]) -> tuple[float, int]:
    """二元特征当规则的**平衡准确率**（取两个方向里更强的那个）+ 方向。

    平衡 = (P(特征|A) + P(无特征|B)) / 2，即"两类各占一半"时这条规则有多准。
    单看原始准确率会被 83% 的多数类抬到 0.83 的假高分。
    """
    p = sum(pos) / len(pos) if pos else 0.0
    q = sum(neg) / len(neg) if neg else 0.0
    fwd = (p + (1 - q)) / 2      # 有特征 ⇒ A
    rev = ((1 - p) + q) / 2      # 有特征 ⇒ B
    return (fwd, 1) if fwd >= rev else (rev, -1)


def probe(layer_dir: pathlib.Path) -> list[tuple[str, str, str]]:
    """返回 (级别, 项, 说明) 清单。级别：WARN / INFO。"""
    defects, normals = load_family(layer_dir)
    if not defects or not normals:
        return [("INFO", "样本不足",
                 f"缺陷 {len(defects)} / 正常 {len(normals)} —— 两类都在才算得出可分性")]

    out: list[tuple[str, str, str]] = []

    # ① 单特征可分性
    for fname, feats in (("材料", _MATERIAL_FEATS), ("叙事", _NARRATION_FEATS)):
        for label, fn in feats.items():
            pos = [fn(r["material"], r["narration"]) for r in defects]
            neg = [fn(r["material"], r["narration"]) for r in normals]
            acc, direction = _rule_strength(pos, neg)
            who = "缺陷" if direction > 0 else "正常"
            share_d, share_n = sum(pos) / len(pos), sum(neg) / len(neg)
            level = "WARN" if acc >= LEAK_RULE_MAX else "INFO"
            out.append((level, f"{fname}特征：{label}",
                        f"规则「命中 ⇒ {who}」平衡准确率 {acc:.1%}"
                        f"（缺陷侧命中 {share_d:.1%} / 正常侧 {share_n:.1%}）"))

    # ② 材料结构指纹
    def shape(r: dict) -> tuple:
        m = r["material"]
        return ("关键事实" in m, "<scene>" in m, m.count("\n## "), len(m) // 100 * 100)

    sd, sn = Counter(shape(r) for r in defects), Counter(shape(r) for r in normals)
    top_d, top_n = sd.most_common(1)[0], sn.most_common(1)[0]
    same = top_d[0] == top_n[0]
    out.append(("INFO" if same else "WARN", "材料结构指纹",
                f"缺陷主形态 {top_d[1]}/{len(defects)}（{top_d[0]}）· "
                f"正常主形态 {top_n[1]}/{len(normals)}（{top_n[0]}）"
                + ("（一致）" if same else "（**不一致 ⇒ 决策 36 同款形态**）")))

    # ③ 同材料跨标签
    import hashlib
    def mh(r: dict) -> str:
        return hashlib.sha256(r["material"].encode("utf-8")).hexdigest()

    dm = defaultdict(int)
    for r in defects:
        dm[mh(r)] += 1
    shared = sum(1 for r in normals if mh(r) in dm)
    out.append(("INFO", "同材料跨标签",
                f"{shared}/{len(normals)} 条正常样本的材料也出现在缺陷样本里"
                f"（>0 ⇒ 材料**不可能**独自决定标签，模型必须读叙事）"))

    # ④ 长度分布
    def med(xs: list[int]) -> float:
        s = sorted(xs)
        return s[len(s) // 2] if s else 0

    d_ch = med([r.get("realized_chars", 0) for r in defects])
    n_ch = med([r.get("realized_chars", 0) for r in normals])
    lo, hi = min(d_ch, n_ch), max(d_ch, n_ch)
    ratio = hi / lo if lo else float("inf")
    out.append(("WARN" if ratio > LEN_RATIO_MAX else "INFO", "长度分布",
                f"字数中位 缺陷 {d_ch:.0f} / 正常 {n_ch:.0f}（比 {ratio:.2f}）"))

    # ⑤ 元叙述
    hits = sum(1 for r in defects + normals if META_RE.search(r["narration"]))
    tot = len(defects) + len(normals)
    out.append(("INFO", "元叙述", f"{hits}/{tot} = {hits / tot:.2%} 的叙事议论材料本身"))
    return out


_LIST_RE = re.compile(r"来源 ((?:`[^`]+`（sha256 `[0-9a-f]+`）(?:、)?)+)")
_LIST_ITEM_RE = re.compile(r"`([^`]+)`（sha256 `([0-9a-f]+)`）")


def probe_review_lists(reports_dir: pathlib.Path, layer: str,
                       root: pathlib.Path | None = None) -> list[tuple[str, str, str]]:
    """⑥ 人读清单是否与出库文件**当前**内容对得上（清单头写着"来源 xxx sha256 yyy"）。

    实测教训（2026-09-18）：材料修复（决策 36）与负例补产（决策 37）都改过
    `judge.jsonl`，而旧清单还在，读清单的人会以为看的是现在这份数据 ——
    "清单自证来源"这句承诺必须有人去核对，否则它只是清单里的一行字。

    路径解析**以 `root` 为基准**（缺省仓库根），不跟当前工作目录走：
    清单里存的是仓库相对路径（`factory_review._rel`），换个目录跑就会去比别的文件
    —— 而"比错了文件却报告一致"是这条检查最难看的失败方式。
    """
    from game_agent.evalmeta import file_digest

    root = REPO_ROOT if root is None else pathlib.Path(root)
    stale, checked = [], 0
    for md in sorted(pathlib.Path(reports_dir).glob(f"*-review-{layer}-*.md")):
        if md.name.endswith("-full.md"):
            continue
        m = _LIST_RE.search(md.read_text(encoding="utf-8")[:800])
        if not m:
            continue
        for rel, sha in _LIST_ITEM_RE.findall(m.group(1)):
            p = root / rel.replace("\\", "/")
            if not p.exists():
                stale.append(f"{md.name}（源不存在：{rel}）")
                continue
            checked += 1
            if file_digest(p)[:16] != sha:
                stale.append(f"{md.name}（{p.name} 记 {sha} 实 {file_digest(p)[:16]}）")
    if not checked and not stale:
        return []
    return [(("WARN" if stale else "INFO"), "人读清单来源",
             f"核对 {checked} 份，过期 {len(stale)} 份" + (f"：{'；'.join(stale)}" if stale
                                                          else "（清单与出库文件一致）"))]


def render(layer: str, rows: list[tuple[str, str, str]]) -> str:
    warns = [r for r in rows if r[0] == "WARN"]
    md = [f"## {layer} —— {'⚠ 报警 ' + str(len(warns)) + ' 项' if warns else '无报警 ✓'}\n",
          "| 级别 | 项 | 说明 |", "| --- | --- | --- |"]
    for level, name, detail in rows:
        md.append(f"| {'**WARN**' if level == 'WARN' else 'INFO'} | {name} | {detail} |")
    return "\n".join(md) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="跨类泄漏探针（正常样本 vs 缺陷样本）")
    p.add_argument("--layer", default="all", choices=["train", "dev", "eval", "all"])
    p.add_argument("--out", default=None, help="写 Markdown（缺省只打印）")
    args = p.parse_args(argv)

    layers = ["train", "dev", "eval"] if args.layer == "all" else [args.layer]
    parts, warn_total = [], 0
    for layer in layers:
        rows = probe(REPO_ROOT / "data" / "route-a" / layer)
        rows += probe_review_lists(REPO_ROOT / "reports", layer)
        warn_total += sum(1 for r in rows if r[0] == "WARN")
        parts.append(render(layer, rows))
        print(f"--- {layer}")
        for level, name, detail in rows:
            print(f"  [{level}] {name}: {detail}")
    if args.out:
        dest = pathlib.Path(args.out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        head = (f"# 跨类泄漏探针（{datetime.date.today():%Y-%m-%d}）\n\n"
                "> 决策 36 的常规化检查：正常样本与缺陷样本之间有没有"
                "「不用读叙事就能判」的表面规律。判据见脚本文首。\n\n")
        dest.write_text(head + "\n".join(parts), encoding="utf-8")
        print(f"[✓] {dest}")
    return 1 if warn_total else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""跨集合近似去重守卫：防"评测题泄漏进训练数据"。

依据（2026-09-12 §4.3）：T（训练包）与 E（评测包）按**轴**不相交，但**句子级/事实级
近重复**仍可能漏过去（同一句式换个名字）。这类泄漏会让评测虚高、还测不出来。

口径：汉字二字组 Jaccard ≥ 阈值（默认 0.6，与 `game.py` `_check_repetition` 同思路）。
比较范围：默认扫"评测侧全量"——世界包 judge 语料 + `eval-sets/extract` + `eval-sets/dedup`
（跨来源两两比较）；`--against FILE` 可把将来的训练集（jsonl/yaml 里的文本）并进来一起比。

用法：
    python scripts/near_dup_check.py                       # 只报告
    python scripts/near_dup_check.py --gate                # 有跨来源近重复则退出码 1
    python scripts/near_dup_check.py --against train.jsonl # 训练集 vs 评测集
"""

from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import re
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_THRESHOLD = 0.60
SAME_SOURCE_HIGH = 0.85  # 同来源（同一包/同一集合）内的近重复线：更高，但也必须报

from game_agent.judge_corpus import load_corpus  # noqa: E402


def bigrams(text: str) -> set[str]:
    """汉字二字组（标点/空白/字母数字不参与）。"""
    han = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    return {han[i : i + 2] for i in range(max(0, len(han) - 1))}


def jaccard(a: str, b: str) -> float:
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def collect_eval_items() -> list[dict]:
    """评测侧全部文本（世界包语料 + extract + dedup）。"""
    items: list[dict] = []
    for pack_dir in sorted((REPO_ROOT / "world-packs").iterdir()):
        if not pack_dir.is_dir() or not (pack_dir / "judge_corpus.yaml").exists():
            continue
        for case in load_corpus(pack_dir):
            items.append({"source": f"judge:{pack_dir.name}", "id": case.id,
                          "text": case.narration})

    extract = REPO_ROOT / "eval-sets" / "extract" / "direct.yaml"
    if extract.exists():
        data = yaml.safe_load(extract.read_text(encoding="utf-8"))
        for c in (data["cases"] if isinstance(data, dict) else data):
            items.append({"source": "extract", "id": c["id"], "text": c.get("turn", "")})

    dedup = REPO_ROOT / "eval-sets" / "dedup" / "pairs.yaml"
    if dedup.exists():
        data = yaml.safe_load(dedup.read_text(encoding="utf-8"))
        for p in (data["pairs"] if isinstance(data, dict) else data):
            for field in ("fact", "similar", "candidate", "a", "b"):
                if isinstance(p.get(field), str):
                    items.append({"source": "dedup", "id": f"{p['id']}:{field}",
                                  "text": p[field]})
    return items


def collect_file_items(path: pathlib.Path, source: str = "训练集") -> list[dict]:
    """从 jsonl / yaml 里捞文本字段（训练集将来用）。"""
    items: list[dict] = []
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        rows = data["cases"] if isinstance(data, dict) and "cases" in data else data
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        text = next((row[k] for k in ("narration", "turn", "text", "output", "completion")
                     if isinstance(row.get(k), str)), "")
        if text:
            items.append({"source": source, "id": str(row.get("id", i)), "text": text})
    return items


def find_near_dups(
    items: list[dict],
    threshold: float = DEFAULT_THRESHOLD,
    same_source_threshold: float = SAME_SOURCE_HIGH,
) -> list[tuple]:
    """近重复对。

    - **跨来源**：≥ ``threshold``（默认 0.60）即报 —— 防"评测题泄漏进训练集"；
    - **同来源**：≥ ``same_source_threshold``（默认 0.85）也报 —— 防"同句换人名"式的模板同质
      （2026-09-12 实例：两包 identity 用例相似度 0.84 却被跨来源阈值漏掉）；
    - 例外：``dedup`` 集合的改写对是**设计使然**（同一事实换词 → 应判"重复"），同源一律不比。
    """
    hits = []
    for a, b in itertools.combinations(items, 2):
        same = a["source"] == b["source"]
        if same and a["source"] == "dedup":
            continue
        sim = jaccard(a["text"], b["text"])
        if sim >= (same_source_threshold if same else threshold):
            hits.append((sim, a, b))
    return sorted(hits, key=lambda t: -t[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨集合近似去重（防泄漏）")
    parser.add_argument("--against", default=None, help="另比一个集合（训练集 jsonl/yaml）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--gate", action="store_true", help="有跨来源近重复则退出码 1")
    parser.add_argument("--top", type=int, default=10, help="最多打印几对")
    args = parser.parse_args(argv)

    items = collect_eval_items()
    if args.against:
        items += collect_file_items(pathlib.Path(args.against))
    hits = find_near_dups(items, args.threshold)

    by_source: dict[str, int] = {}
    for it in items:
        by_source[it["source"]] = by_source.get(it["source"], 0) + 1
    print(f"比较 {len(items)} 条文本（{len(by_source)} 个来源，阈值 {args.threshold:.2f}）：")
    for src, n in sorted(by_source.items()):
        print(f"  {src:<28} {n}")

    if not hits:
        print("\n[✓] 无近重复（跨来源 ≥%.2f / 同来源 ≥%.2f）" % (args.threshold, SAME_SOURCE_HIGH))
        return 0
    print(f"\n[!] {len(hits)} 对近重复（跨来源 ≥{args.threshold:.2f} / 同来源 ≥{SAME_SOURCE_HIGH:.2f}，"
          f"前 {args.top}）：")
    for sim, a, b in hits[: args.top]:
        print(f"  {sim:.2f}  {a['source']}:{a['id']}  ↔  {b['source']}:{b['id']}")
        print(f"        A: {a['text'][:56]}")
        print(f"        B: {b['text'][:56]}")
    print("\n处理建议：评测集与训练集之间必须**删一侧**；评测集内部保留（改写对是设计使然）。")
    return 1 if args.gate else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

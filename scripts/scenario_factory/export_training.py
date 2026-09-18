"""训练格式导出（④）：三层出库 JSONL → **生产提示词逐字对齐**的 SFT 数据。

**硬纪律（plan-phase1-data §4.2）**：*"输入模板必须逐字对齐生产调用，否则训练分布与推理分布
不一致"*。所以本脚本**不重写任何模板**：

- extract / compress 的 user 内容**直接取出库样本的 `input` 字段** —— 它在工厂里就是
  由 `assemble.extract_messages` / `assemble.compress_messages`（生产同款）渲染的；
- judge 的 user 内容**调用生产的 `JudgeSystem._judge_messages`** 现场渲染；
- 三条 system 全部取自生产常量 `EXTRACT_SYSTEM` / `JUDGE_SYSTEM` / `COMPRESS_SYSTEM`。

**目标文本（与生产输出契约一致，可被生产解析器吃下）**：

| 模块 | 目标 | 生产解析器 |
| --- | --- | --- |
| extract | `重要性|事实` 行（≤5 条），无值得记的 → `无` | `memory.parse_facts`（`无`/`没有` 是哨兵） |
| judge | 缺陷 → `问题类型：类别：描述`；正常样本 → `通过` | `judge.parse_verdict`（只看首 10 字是否以「通过」开头） |
| compress | 摘要正文 | `compression`（长度是**提示词指导**，不是硬门） |

**层纪律（决策 32：生产批只出 train+dev，eval 单独跑再冻结）**：

- `train` 层 → 训练集（再切 5% 留出，见下）
- `dev` 层 → 域内验证集（**轴值孪生**，决策 19：语体/姓名形态等轴值与 train 不同，
  用作"换一组轴值还灵不灵"的验证，比同分布随机切分更有信息量）
- `eval` 层 → **不进训练**（它是冻结的尺子），只在 manifest 里登记摘要与 tag 供 ⑥ 引用

**每模块留出**：从 train 层按 `(module, seed)` 确定性地切 5%（≥20 条）作 holdout，
既不进训练文件，也不与 dev 的轴值验证混淆。同一 seed 必得同一划分（守卫钉住）。

用法::

    python -m scripts.scenario_factory.export_training              # 写到 data/training/
    python -m scripts.scenario_factory.export_training --check      # 只校验，不写盘
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import random
import sys

from game_agent.compression import COMPRESS_SYSTEM, SUMMARY_MAX_TARGET
from game_agent.evalmeta import file_digest
from game_agent.judge import JUDGE_SYSTEM, JudgeSystem, parse_verdict
from game_agent.memory import EXTRACT_SYSTEM, parse_facts

from .cards import REPO_ROOT

DATA_ROOT = REPO_ROOT / "data" / "route-a"
DEFAULT_OUT = REPO_ROOT / "data" / "training"
MODULES = ("extract", "judge", "compress")
# judge 家族两个模块文件合并成同一训练任务（正常样本就是 judge 的负例）
SOURCE_FILES = {"extract": ("extract",), "judge": ("judge", "judge_normal"),
                "compress": ("compress",)}
HOLDOUT_RATE = 0.05
HOLDOUT_MIN = 20
HOLDOUT_SEED = 20260918
# 计划 §5：混比 extract : judge : compress ≈ 4 : 3 : 2（dedup 0 / reflect 第一批 0）。
# ⚠️ 这是**采样权重**，不是数据量配额 —— 数据量由工厂配额（judge 家族 confab≥40% 等）定；
# 训练侧按权重抽，让每个 epoch 的任务配比对齐规格。
TARGET_MIX = {"extract": 4, "judge": 3, "compress": 2}
EVAL_TAG = "eval-route-a-20260918"


# ---------------------------------------------------------------------------
# 目标文本
# ---------------------------------------------------------------------------

def target_of(module: str, row: dict) -> str:
    """该模块的目标文本（生产输出契约）。"""
    if module == "extract":
        labels = row.get("labels") or []
        if row.get("expect_empty") or not labels:
            return "无"
        return "\n".join(f"{int(x['importance'])}|{x['text']}" for x in labels)
    if module == "judge":
        if row.get("category") == "normal":
            return "通过"
        return f"{row['expect']}：{row['detail']}"
    if module == "compress":
        return row["output"]
    raise ValueError(f"未知模块 {module}")


def messages_of(module: str, row: dict) -> list[dict]:
    """生产同款 (system, user) + 目标作为 assistant 轮。模板一律取自生产代码。"""
    if module == "extract":
        system, user = EXTRACT_SYSTEM, row["input"]
    elif module == "judge":
        # JudgeSystem 只把 llm 存起来，渲染本身不用它 ⇒ 传 None 即可复用**生产那个函数**
        system = JUDGE_SYSTEM
        user = JudgeSystem(None)._judge_messages(row["narration"], row["material"])[1]["content"]
    elif module == "compress":
        system = COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)
        user = row["input"]
    else:
        raise ValueError(f"未知模块 {module}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": target_of(module, row)}]


# ---------------------------------------------------------------------------
# 读取与校验
# ---------------------------------------------------------------------------

def load_layer(layer: str) -> dict[str, list[dict]]:
    """读一层的三个模块（judge 合并正常样本）。缺文件 = 空（不崩）。"""
    out: dict[str, list[dict]] = {}
    for module, files in SOURCE_FILES.items():
        rows: list[dict] = []
        for name in files:
            f = DATA_ROOT / layer / f"{name}.jsonl"
            if f.exists():
                rows += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()
                         if x.strip()]
        out[module] = rows
    return out


def verify_target(module: str, row: dict, target: str) -> None:
    """目标必须**能被生产解析器吃下**（这是"输出契约一致"的可执行形式）。"""
    if module == "extract":
        got = parse_facts(target)
        want = [] if (row.get("expect_empty") or not row.get("labels")) else \
            [(x["text"], float(x["importance"])) for x in row["labels"]]
        if got != want:
            raise AssertionError(f"{row['id']}: 目标解析回读不一致\n  目标={target!r}\n"
                                 f"  解析={got}\n  期望={want}")
        if len(got) > 5:
            raise AssertionError(f"{row['id']}: 超过 5 条事实")
    elif module == "judge":
        passed, _ = parse_verdict(target)
        want = row.get("category") == "normal"
        if passed is not want:
            raise AssertionError(f"{row['id']}: parse_verdict={passed} 期望 {want}（{target!r}）")
    elif module == "compress":
        if not target.strip():
            raise AssertionError(f"{row['id']}: 空摘要")
    # 三条 system 必须与生产常量逐字相同（模板漂移就在这儿挡住）
    msgs = messages_of(module, row)
    want_system = {"extract": EXTRACT_SYSTEM, "judge": JUDGE_SYSTEM,
                   "compress": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)}[module]
    if msgs[0]["content"] != want_system:
        raise AssertionError(f"{row['id']}: system 与生产常量不一致")
    if msgs[1]["content"] != (row["input"] if module != "judge" else
                              JudgeSystem(None)._judge_messages(
                                  row["narration"], row["material"])[1]["content"]):
        raise AssertionError(f"{row['id']}: user 与生产模板不一致")


def split_rows(rows: list[dict], module: str, rate: float = HOLDOUT_RATE
               ) -> tuple[list[dict], list[dict]]:
    """确定性切分（(module, seed) 决定）：返回 (训练, 留出)。至少留 `HOLDOUT_MIN` 条。"""
    ids = sorted(r["id"] for r in rows)
    k = max(HOLDOUT_MIN, int(round(len(ids) * rate)))
    picked = set(random.Random(f"{HOLDOUT_SEED}-{module}").sample(ids, min(k, len(ids))))
    train = [r for r in rows if r["id"] not in picked]
    hold = [r for r in rows if r["id"] in picked]
    return train, hold


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def export(out_dir: pathlib.Path, *, check_only: bool = False, echo=print) -> dict:
    train_layer, dev_layer = load_layer("train"), load_layer("dev")
    eval_ids = {r["id"] for rows in load_layer("eval").values() for r in rows}

    files: dict[str, list[dict]] = {}
    manifest: dict = {
        "generated": datetime.date.today().isoformat(),
        "spec": "plan-phase1-data §4.2（输入模板逐字对齐生产）/ §5（混比 4:3:2）",
        "mix_weights": TARGET_MIX,
        "holdout": {"rate": HOLDOUT_RATE, "min": HOLDOUT_MIN, "seed": HOLDOUT_SEED},
        "targets": {
            "extract": "`重要性|事实` 行（≤5 条）；无可记的 → `无`（memory.parse_facts 哨兵）",
            "judge": "缺陷 → `问题类型：类别：描述`；正常样本 → `通过`（judge.parse_verdict）",
            "compress": f"摘要正文（提示词目标 {SUMMARY_MAX_TARGET} 字，非硬门）",
        },
        "prompts": {
            "extract_system_sha256": file_digest_str(EXTRACT_SYSTEM),
            "judge_system_sha256": file_digest_str(JUDGE_SYSTEM),
            "compress_system_sha256": file_digest_str(
                COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)),
        },
        "sources": {}, "splits": {}, "layers": {
            "train": "训练集（再切 5% 留出）",
            "dev": "域内验证集（轴值孪生，决策 19）",
            "eval": "**不进训练** —— 冻结的尺子（决策 14/32），仅供 ⑥ 引用",
        },
        "eval_tag": EVAL_TAG,
    }

    for module in MODULES:
        train_rows, hold_rows = split_rows(train_layer[module], module)
        parts = {"train": train_rows, "holdout": hold_rows, "dev": dev_layer[module]}
        n_checked = 0
        for split, rows in parts.items():
            out_rows = []
            for r in rows:
                verify_target(module, r, target_of(module, r))
                n_checked += 1
                out_rows.append({
                    "id": r["id"], "module": module, "layer": split, "genre": r.get("genre"),
                    "messages": messages_of(module, r),
                    # `over_target`：compress 摘要超过提示词目标（**偏好项不是硬门**，发现④）——
                    # 带进 meta 让训练侧自己决定要不要过滤，而不是在导出时替它决定。
                    "meta": {k: r[k] for k in ("expect", "category", "over_target",
                                               "target_tokens", "long_input",
                                               "realized_chars") if k in r},
                })
            leak = {x["id"] for x in out_rows} & eval_ids
            if leak:
                raise AssertionError(f"{module}/{split}: **eval 层样本混进来了**：{sorted(leak)[:3]}")
            files[f"{module}.{split}.jsonl"] = out_rows
            manifest["splits"].setdefault(module, {})[split] = len(out_rows)
        if {r["id"] for r in train_rows} & {r["id"] for r in hold_rows}:
            raise AssertionError(f"{module}: 留出与训练集有交集")
        manifest["splits"][module]["holdout_ids"] = sorted(r["id"] for r in hold_rows)

    for module, names in SOURCE_FILES.items():
        for layer in ("train", "dev", "eval"):
            for name in names:
                f = DATA_ROOT / layer / f"{name}.jsonl"
                if f.exists():
                    manifest["sources"][str(f.relative_to(REPO_ROOT)).replace("\\", "/")] = {
                        "sha256": file_digest(f), "rows": sum(
                            1 for x in f.read_text(encoding="utf-8").splitlines() if x.strip())}

    # 数据集整体摘要：训练侧全部文件（按文件名排序）拼接后的 sha256
    h = hashlib.sha256()
    for name in sorted(files):
        for row in files[name]:
            h.update(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            h.update(b"\n")
    manifest["dataset_sha256"] = h.hexdigest()

    total = sum(len(v) for v in files.values())
    over = sum(1 for r in files.get("compress.train.jsonl", []) if r["meta"].get("over_target"))
    n_comp = len(files.get("compress.train.jsonl", []))
    manifest["notes"] = {
        "compress_over_target": f"{over}/{n_comp} 条训练摘要超提示词目标（偏好项，非硬门）",
        "mix": "4:3:2 是**采样权重**（计划 §5），数据量本身由工厂配额定；"
               "train 层自然混比见 splits，训练侧按权重抽即可",
        "dev": "域内验证集（轴值孪生）—— 可直接作 validation，也可与 train 合并做最终一轮",
    }
    echo(f"[导出] {total} 条 —— " + "、".join(
        f"{m}: 训练 {manifest['splits'][m]['train']} / 留出 "
        f"{manifest['splits'][m]['holdout']} / 验证 {manifest['splits'][m]['dev']}"
        for m in MODULES))
    echo(f"[校验] 目标回读全过（{total} 条）；eval 层 {len(eval_ids)} 条 id 已确认未混入")
    if check_only:
        echo("[--check] 未写盘")
        return manifest

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        (out_dir / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8", newline="\n")
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    echo(f"[✓] {out_dir}（9 个 jsonl + manifest.json；dataset_sha256 "
         f"{manifest['dataset_sha256'][:16]}）")
    return manifest


def file_digest_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="训练格式导出（生产提示词逐字对齐）")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--check", action="store_true", help="只校验不写盘")
    args = p.parse_args(argv)
    try:
        export(pathlib.Path(args.out), check_only=args.check)
    except AssertionError as e:
        print(f"[✗] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

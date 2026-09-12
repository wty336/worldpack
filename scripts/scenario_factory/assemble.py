"""数据集装配（spec §9.1）：样本构建三分支 + 质量门/门禁/配额/去重/出库。

标签纪律：labels/expect/preserve_points 全部取自卡面，永不从文本反推（spec §2）。

**输入模板必须逐字对齐生产调用**（spec §4.2 开头的硬纪律，也是 spec §9.2「五模块输入契约
继承不动」的落地）——提示词一律从引擎 import，**不得另写**，否则训练分布 ≠ 推理分布：

- extract  模板引自 `game.py:_extract_facts`（同一行格式见 `extract_eval.py:159-166`）
- compress 模板引自 `game.py:_compress_history`

契约由守卫测试 `test_*_input_matches_production_template` 钉住，防两处漂移。

compress 拒绝采样三档（spec §6）：off=单次直出；long=仅长输入档 n=4；all=全量 n=4。
程序先杀（硬杀）两项：虚构（摘要「」词不在历史）/缺要点；**"超长"是偏好项不是杀** ——
生产端 `game.py:_compress_history` 不检查摘要长度（`SUMMARY_MAX_TARGET` 是提示词指导），
故候选里优先选未超目标的，全超才取最短者并标记 `over_target`（发现④，2026-09-13 验收）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import sys
from collections import Counter
from dataclasses import dataclass, field as dc_field

from game_agent.budgets import COMPRESS_MAX_TOKENS, complete_checked
from game_agent.compression import COMPRESS_SYSTEM, SUMMARY_MAX_TARGET
from game_agent.evalmeta import file_digest
from game_agent.memory import EXTRACT_SYSTEM
from game_agent.worldpack import WorldPack
from scripts.rubric_judge import select

from .cards import REPO_ROOT, ScenarioCard, card_seed, generate_card, layer_of
from .materialize import MaterializeError, build_material, load_pack
from .verbalize import verbalize_card

# card_hook_check 不是包成员（脚本层）：注入 scripts/ 后 import
# （脚本层先例见 scripts/diag_turn.py:17；tests 层不用此法）
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from card_hook_check import CARD_FIELDS, _grams  # noqa: E402

# 工厂 confab 门禁的重合粒度（发现⑥）：`card_hook_check.HOOK_N=2` 是**评测语料**的筛查口径，
# 当产线过滤器会在真实叙事上误杀 87%（撞的全是「自己」「的原因」这类虚词）；见 `hook_gate` docstring。
FACTORY_HOOK_N = 4

SAMPLE_VERSION = "route-a-v1"
LONG_INPUT_TOKENS = 8000   # spec §6「长输入档」阈值（计划口径；spec 原话为"20K 级"）
CANDIDATES_N = 4
SUMMARY_TEMPERATURE = 0.7  # 候选需多样性（spec §6 的 temp 0.8 档）；评委仍 temp=0


def extract_messages(card: ScenarioCard, turn: str) -> list[dict]:
    """生产同款 extract 输入（逐步对齐 `game.py:_extract_facts`）。"""
    existing = "；".join(card.existing)
    return [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user",
         "content": f"已有事实：{existing}\n\n<回合内容>\n{turn}\n</回合内容>"},
    ]


def compress_messages(card: ScenarioCard, history: str) -> list[dict]:
    """生产同款 compress 输入（逐步对齐 `game.py:_compress_history`）。"""
    return [
        {"role": "system", "content": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)},
        {"role": "user",
         "content": f"<旧摘要>\n{card.old_summary}\n</旧摘要>\n\n<新增历史>\n"
                    f"{history}\n</新增历史>"},
    ]


@dataclass
class BuildResult:
    sample: dict | None
    dropped_reason: str | None = None


def build_extract_sample(llm, card: ScenarioCard) -> BuildResult:
    r = verbalize_card(llm, card, purpose="verbalize_extract")
    if r.dropped:
        return BuildResult(None, "演绎丢弃")
    return BuildResult({
        "id": card.card_id, "module": "extract", "version": SAMPLE_VERSION,
        "genre": card.axes.genre,
        # 生产同款 user 段（含 `已有事实：` 与 `<回合内容>` 包裹）——不是裸文本
        "input": extract_messages(card, r.text)[1]["content"],
        "existing": list(card.existing),
        "expect_empty": not card.facts,   # facts 空 = 该回合无新事实 = 标签「无」（负例）
        "labels": [{"type": f.type, "text": f.text, "importance": f.importance}
                   for f in card.facts],
        **_length_fields(card, r.text),
    })


def build_judge_sample(llm, card: ScenarioCard) -> BuildResult:
    try:
        material = build_material(card)
    except MaterializeError as e:
        return BuildResult(None, f"材料装配失败: {e}")
    r = verbalize_card(llm, card, purpose="verbalize_judge")
    if r.dropped:
        return BuildResult(None, "演绎丢弃")
    c = card.corruptions[0]
    sample = {
        "id": card.card_id, "module": "judge", "version": SAMPLE_VERSION,
        "genre": card.axes.genre, "pack": card.pack, "material": material,
        "narration": r.text, "expect": c.expect, "category": c.category,
        "speaker": card.material.present[0],
        **_length_fields(card, r.text),
    }
    # **出厂门禁接在产线上**（原稿定义了 hook_gate 却从未调用 = 门禁不存在）：
    # confab 撞说话人角色卡会多开一条「设定矛盾」通路，拦截率虚高、跨包不可比。
    if hooked := hook_gate(sample, load_pack(card.pack)):
        return BuildResult(None, f"confab 撞卡: {'/'.join(hooked)}")
    return BuildResult(sample)


def _fabrication_hit(summary: str, history: str) -> str | None:
    """程序先杀①：摘要的「」引用词不在历史中 → 虚构。"""
    for w in re.findall(r"「([^」]+)」", summary):
        if w not in history:
            return w
    return None


def _program_kill(summary: str, history: str, card: ScenarioCard) -> str | None:
    """程序先杀（**硬杀**：只杀生产端真的不能接受的东西）。

    硬杀项 = 缺要点 / 虚构。**"超长"不在这里** —— 见 `_over_target()`：
    生产端 `game.py:_compress_history` 不检查摘要长度（`SUMMARY_MAX_TARGET` 的注释写明
    是"字符，**提示词指导**"），原先按 800 硬杀属**比生产更严**的误杀（发现④，2026-09-13）。
    """
    if w := _fabrication_hit(summary, history):
        return f"虚构:{w}"
    for p in card.preserve_points:  # 程序先杀②：缺要点
        if not any(a in summary for a in p.anchors):
            return f"缺要点:{p.text}"
    return None


def _over_target(summary: str) -> bool:
    """是否超过提示词目标长度（**偏好项，不是杀**）：候选里优先选没超的，全超才退而取最短。"""
    return len(summary) > SUMMARY_MAX_TARGET


def _summarize_once(llm, history: str, card: ScenarioCard) -> str:
    """生产同款 compress 调用（含 `COMPRESS_MAX_TOKENS=4000` 预算；截断即弃）。"""
    text, finish = complete_checked(llm, compress_messages(card, history),
                                    purpose="compress", max_tokens=COMPRESS_MAX_TOKENS,
                                    temperature=SUMMARY_TEMPERATURE)
    if finish != "stop":
        raise ValueError("摘要截断")
    return text


def _sampling_on(card: ScenarioCard, sampling: str) -> bool:
    return sampling == "all" or (
        sampling == "long" and card.history_spec.target_tokens >= LONG_INPUT_TOKENS)


def build_compress_sample(llm, card: ScenarioCard, *, sampling: str = "off"
                          ) -> BuildResult:
    hist = verbalize_card(llm, card, purpose="verbalize_compress")
    if hist.dropped:
        return BuildResult(None, "历史演绎丢弃")
    # **单一素材真源**：模型看到的就是这一段（含 <旧摘要>/<新增历史> 包裹），
    # 故样本 input、程序先杀的虚构素材、评委的【材料】三处都用它 —— 早先版本只有
    # hist.text（裸历史），造成三处后果：① 样本 input 与生产模板不一致（spec §4.2 硬纪律）；
    # ② 增量合并档的旧摘要根本不进样本（25% 的卡，模型学不到"读旧摘要→合并"）；
    # ③ 虚构判定以裸历史为素材，旧摘要里的「」引用词会被误判成编造 → 候选被误杀。
    source = compress_messages(card, hist.text)[1]["content"]
    n = CANDIDATES_N if _sampling_on(card, sampling) else 1
    cands, over, killed = [], [], []
    for _ in range(n):
        try:
            s = _summarize_once(llm, hist.text, card)
        except ValueError as e:
            killed.append(str(e))
            continue
        if why := _program_kill(s, source, card):   # 硬杀优先：超长不豁免 虚构/缺要点
            killed.append(why)
            continue
        (over if _over_target(s) else cands).append(s)
    if cands:
        best = select(llm, candidates=cands, material=source,
                      preserve_points=card.preserve_points) if len(cands) > 1 else 0
        chosen, over_target = cands[best], False
    elif over:
        # 全部超长 → 取最短者并标记（原先直接丢卡 = 比生产严的误杀，见 _over_target）
        chosen, over_target = min(over, key=len), True
    else:
        return BuildResult(None, f"候选全杀: {killed}")
    return BuildResult({
        "id": card.card_id, "module": "compress", "version": SAMPLE_VERSION,
        "genre": card.axes.genre,
        # 生产同款 user 段（与 extract 侧对称：extract 也存带模板包裹的 user 段）
        "input": source, "output": chosen,
        # 超了提示词目标但生产端会接受 → 出库但**留痕**（训练数据审计/回填成本都靠这个字段）
        "over_target": over_target,
        "sampling": sampling, "candidates": len(cands) + len(over), "killed": killed,
        **_length_fields(card, hist.text),
        "preserve_points": [{"text": p.text, "anchors": p.anchors}
                            for p in card.preserve_points],
    })


# ---------------------------------------------------------------------------
# 质量门与出厂门禁（spec §9.2：card_hook_check 复用不重写）
# ---------------------------------------------------------------------------

GATE_MAX_DROP_RATE = 0.30   # 丢弃率超阈 → 批作废（先停产线，不硬凑量）
QUALITY_SAMPLE_RATE = 0.20  # §7.4 质检员抽检比例（常驻关卡）


@dataclass
class BatchStats:
    built: int = 0
    dropped: int = 0
    reasons: Counter = dc_field(default_factory=Counter)
    # 丢弃留档（2026-09-13 验收后补）：原先只记"丢了几张"，事后无法回答"丢的是谁"——
    # 发现④⑥ 的诊断都卡在这里（要么重跑花钱，要么只能猜）。现在按原因记 id，随批落盘。
    dropped_ids: dict[str, list[str]] = dc_field(default_factory=dict)

    def drop(self, reason: str, card_id: str) -> None:
        key = reason.split(":")[0]
        self.dropped += 1
        self.reasons[key] += 1
        self.dropped_ids.setdefault(key, []).append(card_id)


def hook_gate(sample: dict, pack: WorldPack) -> list[str]:
    """confab 出厂门禁：narration × 说话人角色卡（`CARD_FIELDS`）的**逐字引用** → 返回撞词。

    依据 `card_hook_check.py` 文首实证：confab 撞上说话人角色卡（底线/禁忌/说话风格）会让
    判官多一条「设定矛盾」短路 —— 拦截率虚高、跨包不可比（实测同族用例可被抬到 88%）。
    本门禁只查 **confab**；词面之外的部分（语义撞卡、决策 16 的"断言不得由材料已有事实
    组合推出"）**不可程序化**，走人读清单 `manual_review_row()`。

    **判据换粒度（发现⑥，2026-09-13 实测）**：原先用 `card_hook_check.HOOK_N=2`（任一 **2 字**
    重合即撞卡）—— 那是**评测语料**的"必要条件筛查"口径，当作**产线过滤器**完全不可用：
    在 30 条真实 confab 叙事上撞卡 **26/30 = 87%**，撞的全是虚词二字组（`自己`×6、`直接`×3、
    `具体`、`的原因`、`自己是`…），于是 confab 卡被成批误杀（出库幸存率仅 **23%** vs 卡面 34%）。
    改判据为 **≥`FACTORY_HOOK_N` 字连续重合**（≈ 逐字引用角色卡；同批实测 4-gram 撞 0/30、
    3-gram 仍抓 `的原因`/`自己是` 这类噪声）。`card_hook_check.py` 自身**不动**——它的 2-gram 口径
    服务于评测语料的既有校准，两者用途不同，各自留档。
    """
    if sample.get("category") != "confab":
        return []
    spec = pack.npcs.get(sample["speaker"])
    card_text = (" ".join(str(spec.model_dump().get(f)) for f in CARD_FIELDS)
                 if spec else "")
    return hook_overlap(sample["narration"], card_text)


def hook_overlap(narration: str, card_text: str,
                 n: int = FACTORY_HOOK_N) -> list[str]:
    """叙事与角色卡的 **n 字连续重合**（规范化复用 `card_hook_check._grams`，只改粒度）。"""
    nar, card = _grams(narration, n), _grams(card_text, n)
    return sorted(nar & card)


def quality_gate(stats: BatchStats) -> str | None:
    """批级质量门：丢弃率超阈 → 返回原因（调用方作废该批，先停产线不硬凑量）。"""
    total = stats.built + stats.dropped
    if total and stats.dropped / total > GATE_MAX_DROP_RATE:
        return f"丢弃率 {stats.dropped}/{total} 超 {GATE_MAX_DROP_RATE:.0%}"
    return None


def manual_review_row(sample: dict) -> dict:
    """confab 人读清单行（决策 16 的**不可程序化**检查）：

    「断言不得由材料已有事实组合推出」只能人读 —— 逐行核对 material 与 narration。
    随交付，不替代抽检。
    """
    return {"id": sample["id"], "material": sample["material"],
            "narration": sample["narration"], "expect": sample["expect"]}


# ---------------------------------------------------------------------------
# 配额 / 去重 / 出库（spec §8：三层种子空间；eval 冻结纪律）
# ---------------------------------------------------------------------------

DATA_ROOT = REPO_ROOT / "data" / "route-a"
PREFIX_N = 64                # 前缀指纹长度（字符）：防跨层泄漏与近复用
# ⚠️ 已知风险（实施时盯住丢弃率）：演绎文本开头常同形（同一 VERBALIZE_SYSTEM + 同题材），
# 64 字前缀可能把**不同样本**判成重复；而 dup 计入 dropped → 可能把丢弃率推过
# GATE_MAX_DROP_RATE，形成"越像越丢、越丢越像"的反馈。若"前缀去重"占 dropped 的比例
# 异常高（>1/3），改用整文 hash 作精确去重 + 二字组 Jaccard 判近重复
# （复用 scripts/near_dup_check.py 口径）。
GENRE_MIN_SHARE = 0.10       # 层内每题材 ≥10%（plan-phase1-data.md §4.4 轴矩阵按层计数）
LONG_MIN_SHARE = 0.20        # compress 长输入档 ≥20%（决策 18：合成/真实分列计数）
LENGTH_TOLERANCE = 0.5       # 实测长度至少兑现卡面目标的一半，才算"这一档真的产出来了"（发现⑤）
LAYER_BASE = {"train": 10000, "dev": 20000, "eval": 30000}


def _length_fields(card: ScenarioCard, text: str) -> dict:
    """长度三件套（发现⑤）：卡面目标 / **实测**字数 / 是否真的算长输入样本。

    原先 `long_input` 只比**卡面** `history_spec.target_tokens`，于是"20K 长输入档"名义达标、
    实测最长一次演绎输出只有 **4,323 token**（出库历史 404~4,127 字 ≈ 目标的两成）。
    口径：`compression.est_tokens()` 是"字符即保守上界"，故与卡面 `target_tokens`（模型 token）
    用同一把尺子比，按"实测字数 ≥ 目标 × `LENGTH_TOLERANCE`"判兑现。
    """
    target = card.history_spec.target_tokens
    realized = len(text)
    return {
        "target_tokens": target,
        "realized_chars": realized,
        "long_input": target >= LONG_INPUT_TOKENS and realized >= target * LENGTH_TOLERANCE,
    }


def fingerprint(text: str) -> str:
    return hashlib.sha256(text[:PREFIX_N].encode("utf-8")).hexdigest()[:16]


def _sample_text(s: dict) -> str:
    """跨模块取"表面文本"：extract/compress 用 `input`，judge 用 `narration`。

    （原稿只取 `s["input"]`，而 `build_judge_sample` **不产出 input 键** → judge 一跑就 KeyError。）
    """
    return s.get("input") or s.get("narration") or ""


def dedup(samples: list[dict], seen: set[str]) -> tuple[list[dict], int]:
    out, dup = [], 0
    for s in samples:
        fp = fingerprint(_sample_text(s))
        if fp in seen:
            dup += 1
            continue
        seen.add(fp)
        out.append(s)
    return out, dup


def quota_gaps(samples: list[dict]) -> list[str]:
    """配额缺口清单（空=达标）。只报告不硬杀：缺口由产线补产，不是丢样本的理由。"""
    gaps: list[str] = []
    by_mod: dict[str, list[dict]] = {}
    for s in samples:
        by_mod.setdefault(s["module"], []).append(s)
    for mod, rows in by_mod.items():
        for g, n in Counter(r.get("genre", "?") for r in rows).items():
            if n / len(rows) < GENRE_MIN_SHARE:
                gaps.append(f"{mod}/{g}: {n}/{len(rows)} < {GENRE_MIN_SHARE:.0%}")
    comp = by_mod.get("compress", [])
    if comp:
        long_n = sum(1 for r in comp if r.get("long_input"))
        if long_n / len(comp) < LONG_MIN_SHARE:
            nominal = [r for r in comp if r.get("target_tokens", 0) >= LONG_INPUT_TOKENS]
            detail = ""
            if nominal:  # 卡面属长档却没兑现 → 把两个数都摆出来（否则读报告的人只看到"长输入 0%"）
                worst = max(r.get("realized_chars", 0) for r in nominal)
                detail = (f"——卡面属长档 {len(nominal)}/{len(comp)} 张但**实测未兑现**"
                          f"（目标 {max(r.get('target_tokens', 0) for r in nominal):,} token，"
                          f"实测最长 {worst} 字；发现⑤）")
            gaps.append(f"compress 长输入档: {long_n}/{len(comp)} < {LONG_MIN_SHARE:.0%}"
                        f"（合成/真实须分列计数，决策 18）{detail}")
    return gaps


def drop_flagged(samples: list[dict], bad_ids: list[str]) -> tuple[list[dict], int]:
    """按 id 剔除质检不合格样本 → `(保留, 实际剔除条数)`。

    **为什么要返回实际条数**：`id` 曾在本层内不唯一（发现①），一个被点名的 id 会
    同时删掉三个模块的副本 —— 实测 train/eval 各**实剔 3 条而日志只报 1 条**，
    于是「90 张卡 − 丢弃 18 = built 72」与「出库 69」怎么都对不上账。
    id 唯一性已由 `card_seed` + `write_layer` 硬校验保证；这里仍报实际数，是为了
    让同类回归**立刻可见**（日志与账目同源，而不是各说各话）。
    """
    bad = set(bad_ids)
    kept = [s for s in samples if s["id"] not in bad]
    return kept, len(samples) - len(kept)


def write_layer(layer: str, samples: list[dict], out_dir: pathlib.Path) -> dict:
    """出库：{layer}/{module}.jsonl + manifest.json（sha256/计数/冻结标志）。"""
    ids = [s["id"] for s in samples]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:  # 发现①：撞 id 会让下游按 id 对齐/剔除**连坐**，故在出库口直接拒写
        raise ValueError(
            f"层内 id 重复 {len(dup)} 个（前 5：{dup[:5]}）——出库拒写。"
            "id 是数据集唯一键；请检查是否有人绕开 card_seed 直接拼 seed（发现①）")
    layer_dir = out_dir / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    by_mod: dict[str, list[dict]] = {}
    for s in samples:
        by_mod.setdefault(s["module"], []).append(s)
    manifest = {"layer": layer, "version": SAMPLE_VERSION,
                "written_at": datetime.date.today().isoformat(),
                "frozen": layer == "eval", "modules": {}}
    for mod, rows in sorted(by_mod.items()):
        f = layer_dir / f"{mod}.jsonl"
        f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                     encoding="utf-8")
        # 出库摘要走 file_digest（换行归一化）：Windows 下 write_text 会把 \n 落成 CRLF，
        # 裸 read_bytes() 摘要会让同一份数据集在不同平台得到两个 sha（与 eval-sets 同因）
        manifest["modules"][mod] = {
            "count": len(rows), "file": str(f.relative_to(out_dir)),
            "sha256": file_digest(f),
        }
    (layer_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if layer == "eval":  # 冻结纪律（spec §8 + plan-phase1-data.md §3.3 扩展）
        with (layer_dir / "OPEN_LOG.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- {manifest['written_at']} WRITE 出库 "
                     f"{sum(m['count'] for m in manifest['modules'].values())} 条；"
                     "此后每次打开（读取用于决策）须在此追加一行计数。\n")
        print("[eval 层已冻结] 请打 git tag：git tag eval-route-a-YYYYMMDD")
    return manifest


def _build_module(llm, module: str, count: int, seed_base: int, sampling: str,
                  stats: BatchStats, seen: set[str]) -> list[dict]:
    builders = {"extract": build_extract_sample, "judge": build_judge_sample}
    out = []
    for i in range(count):
        # seed 走 card_seed（层内按模块错开）：三模块共用 seed_base+i 会让 card_id 撞车（发现①）
        card = generate_card(card_seed(seed_base, i, module), i, module)
        if module == "compress":
            r = build_compress_sample(llm, card, sampling=sampling)
        else:
            r = builders[module](llm, card)
        if r.sample is None:
            stats.drop(r.dropped_reason, card.card_id)   # 留档：丢的是哪张卡（诊断用）
            continue
        out.append(r.sample)
        stats.built += 1
    rows, dup = dedup(out, seen)
    if dup:  # 去重丢的是**样本**（id 即卡 id）→ 同样留档
        kept_ids = {s["id"] for s in rows}
        for s in out:
            if s["id"] not in kept_ids:
                stats.drop("前缀去重", s["id"])
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="场景卡工厂出库（三层种子空间，spec §8）")
    p.add_argument("--layer", required=True, choices=["train", "dev", "eval"])
    p.add_argument("--extract", type=int, default=0)
    p.add_argument("--judge", type=int, default=0)
    p.add_argument("--compress", type=int, default=0)
    p.add_argument("--sampling", default="off", choices=["off", "long", "all"])
    p.add_argument("--seed-base", type=int, default=None,
                   help="缺省按层取 10000/20000/30000")
    p.add_argument("--out", default=str(DATA_ROOT))
    p.add_argument("--dry-run", action="store_true",
                   help="只出卡做配额预演，不调 LLM、不出库（零成本）")
    args = p.parse_args(argv)

    seed_base = args.seed_base if args.seed_base is not None else LAYER_BASE[args.layer]
    if layer_of(seed_base) != args.layer:
        print(f"[✗] seed 与层不一致（seed={seed_base} 属 {layer_of(seed_base)} 层）——"
              "三层空间隔离，spec §8")
        return 1
    out_dir = pathlib.Path(args.out)

    if args.dry_run:  # 配额预演：零 API 成本，先看轴分布再决定产多少
        for mod, n in (("extract", args.extract), ("judge", args.judge),
                       ("compress", args.compress)):
            if not n:
                continue
            genres = Counter(generate_card(card_seed(seed_base, i, mod), i, mod).axes.genre
                             for i in range(n))
            long_n = sum(1 for i in range(n)
                         if generate_card(card_seed(seed_base, i, mod), i, mod
                                          ).history_spec.target_tokens >= LONG_INPUT_TOKENS)
            print(f"[dry-run] {mod}: {n} 卡，题材 {dict(genres)}，长输入 {long_n}/{n}")
        return 0

    from game_agent.config import load_settings
    from game_agent.llm import LLMClient
    from game_agent.usage import UsageTracker
    from scripts.rubric_judge import quality_sample

    # usage 记账：**必须显式建 tracker 并传进 LLMClient** —— 落盘只发生在
    # `LLMClient._record_usage`，`complete_checked` 本身**不接触** UsageTracker。
    # 走 `from_settings` 同时带来：模型路由（judge/compress 档）+ 侧信道关思考。
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（.env）")
        return 1
    tracker = UsageTracker("reports/usage-route-a.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)

    stats = BatchStats()
    seen: set[str] = set()
    seen_file = out_dir / "seen_fingerprints.json"  # 跨层去重真源：train 先产，dev/eval 复用
    if seen_file.exists():
        seen |= set(json.loads(seen_file.read_text(encoding="utf-8")))
    samples: list[dict] = []
    for mod, n in (("extract", args.extract), ("judge", args.judge),
                   ("compress", args.compress)):
        if n:
            samples += _build_module(llm, mod, n, seed_base, args.sampling, stats, seen)
    if why := quality_gate(stats):
        print(f"[✗] 质量门未过：{why}——批作废，先停产线")
        return 1
    # §7.4 质检员（常驻关卡，Task 7 Step 3b）：抽检自然度，<1 剔除重造
    bad_ids = quality_sample(llm, samples, rate=QUALITY_SAMPLE_RATE)
    if bad_ids:
        samples, removed = drop_flagged(samples, bad_ids)
        extra = removed - len(bad_ids)
        print(f"[质检] 自然度 <1 剔除 {removed} 条（须重造，点名 {len(bad_ids)} 个 id"
              + (f"，**按 id 连坐多剔 {extra} 条**" if extra else "") + f"）：{bad_ids[:10]}")
    for g in quota_gaps(samples):
        print(f"[配额缺口] {g}")
    manifest = write_layer(args.layer, samples, out_dir)
    if stats.dropped_ids:  # 丢弃留档：事后要能回答"丢的是哪张卡"（发现④⑥ 的诊断口子）
        drop_file = out_dir / args.layer / f"dropped-{args.layer}.json"
        drop_file.write_text(json.dumps(stats.dropped_ids, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"[丢弃留档] {drop_file.relative_to(out_dir)}（{stats.dropped} 张，按原因分列）")
    seen_file.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    confab = [s for s in samples if s.get("category") == "confab"]
    if confab:  # 决策 16：人读清单随批交付
        mr = out_dir / args.layer / f"confab-manual-review-{args.layer}.jsonl"
        mr.write_text("\n".join(json.dumps(manual_review_row(s), ensure_ascii=False)
                                for s in confab) + "\n", encoding="utf-8")
        print(f"[人读清单] {mr}（{len(confab)} 条 confab，决策 16 待人工复核）")
    print(f"[✓] {args.layer} 出库 "
          f"{sum(m['count'] for m in manifest['modules'].values())} 条"
          f"（丢弃 {stats.dropped}：{dict(stats.reasons)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

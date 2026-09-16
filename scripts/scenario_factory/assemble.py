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
from scripts.rubric_judge import (QUALITY_SAMPLE_SEED, label_check, quality_sample,
                                  sample_positions, select)

from . import worklog
from .cards import (REPO_ROOT, LONG_INPUT_TOKENS, ScenarioCard, card_seed,
                    generate_card, layer_of)
from .materialize import MaterializeError, build_material, load_pack
from .verbalize import _meta_soft, _name_confusables, verbalize_card

# card_hook_check 不是包成员（脚本层）：注入 scripts/ 后 import
# （脚本层先例见 scripts/diag_turn.py:17；tests 层不用此法）
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from card_hook_check import CARD_FIELDS, _grams  # noqa: E402

# 工厂 confab 门禁的重合粒度（发现⑥）：`card_hook_check.HOOK_N=2` 是**评测语料**的筛查口径，
# 当产线过滤器会在真实叙事上误杀 87%（撞的全是「自己」「的原因」这类虚词）；见 `hook_gate` docstring。
FACTORY_HOOK_N = 4

SAMPLE_VERSION = "route-a-v1"
# LONG_INPUT_TOKENS 的单一真源在 `cards.py`（卡空间定义档位；长档只对 compress 开放，2026-09-13 实测重定）
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


LABEL_GATE_ATTEMPTS = 2   # 标签门禁：首演 + 重演一次（发现⑧ 的根治）


def _label_gate(llm, *, module: str, card: ScenarioCard, text: str,
                material: str = "", speaker_card: str = "") -> str | None:
    """**生成时**标签门禁：叙事/历史是否**真的**承载了标签所述的内容。返回 None = 通过。

    为什么要在生成时判，而不是只靠事后抽检：发现⑧ 实测 judge 批 **36/87 = 41%** 的「问题类型」
    在语义上并不成立（ooc 17/31、setting 10/26、confab 9/30），而项目**精编语料**的 ooc 判不成立
    **0/40** ⇒ 不是尺子偏严。根因是程序侧只做**字面**检查（anchor 在位／新值在原值去），
    标签却是**语义**的（"问题类型：OOC"）。事后抽检只能"发现问题 + 丢样本"，
    生成时门禁能**当场重演**把样本救回来。

    未判定（空响应 / JSON 解析失败）**不算通过**（"空 = 未知 ≠ 通过"）→ 返回"标签未判定"。
    """
    from scripts.rubric_judge import check_judge_label, check_labels

    if module == "judge":
        c = card.corruptions[0]
        r = check_judge_label(llm, expect=c.expect, detail=c.detail, speaker_card=speaker_card,
                              material=material, narration=text)
    else:
        labels = ([f"{f.type}：{f.text}" for f in card.facts] if module == "extract"
                  else [p.text for p in card.preserve_points])
        if not labels:            # extract 负例卡（该回合无新事实）→ 没有标签可校
            return None
        r = check_labels(llm, labels=labels, text=text)
    if not r["checked"]:
        return "标签未判定"
    return f"标签不成立: {r['violations'][0][:40]}" if r["violations"] else None


def _verbalize_until_label_ok(llm, card: ScenarioCard, *, module: str, purpose: str,
                              drop_label: str, material: str = "", speaker_card: str = ""):
    """演绎 → 标签门禁 →（不通过则）重演一次。返回 `(VerbalizeResult, 丢弃原因|None)`。"""
    r, why = None, None
    for _ in range(LABEL_GATE_ATTEMPTS):
        r = verbalize_card(llm, card, purpose=purpose)
        if r.dropped:
            # 把**为什么**带上（元叙述/缺 anchor/截断）——聚合键仍取 ":" 前的部分，
            # 而 `dropped-{layer}.json` 的 detail 会留全（2026-09-13 预演后加的诊断口子）
            detail = "/".join(r.violations[:2])
            return r, (f"{drop_label}: {detail}" if detail else drop_label)
        why = _label_gate(llm, module=module, card=card, text=r.text,
                          material=material, speaker_card=speaker_card)
        if why is None:
            return r, None
    return r, why


def build_extract_sample(llm, card: ScenarioCard) -> BuildResult:
    r, why = _verbalize_until_label_ok(llm, card, module="extract",
                                       purpose="verbalize_extract", drop_label="演绎丢弃")
    if why:
        return BuildResult(None, why)
    return BuildResult({
        "id": card.card_id, "module": "extract", "version": SAMPLE_VERSION,
        "genre": card.axes.genre,
        # 生产同款 user 段（含 `已有事实：` 与 `<回合内容>` 包裹）——不是裸文本
        "input": extract_messages(card, r.text)[1]["content"],
        "existing": list(card.existing),
        "expect_empty": not card.facts,   # facts 空 = 该回合无新事实 = 标签「无」（负例）
        "labels": [{"type": f.type, "text": f.text, "importance": f.importance}
                   for f in card.facts],
        # 发现⑩ 的批次级观察项（只记不杀）：温和元词命中、近误人名
        "meta_soft": _meta_soft(r.text),
        "name_confusables": _name_confusables(card, r.text),
        **_length_fields(card, r.text),
    })


def build_judge_sample(llm, card: ScenarioCard) -> BuildResult:
    try:
        material = build_material(card)
    except MaterializeError as e:
        return BuildResult(None, f"材料装配失败: {e}")
    pack = load_pack(card.pack)
    speaker_card = _speaker_card_text(pack, card.material.present[0])
    # 标签门禁对 judge 尤其关键：`ooc` 类在程序侧**没有任何校验**（发现⑧），
    # 「问题类型」是否真的成立只能靠语义判定 ⇒ 生成时判 + 不通过就重演。
    r, why = _verbalize_until_label_ok(llm, card, module="judge", purpose="verbalize_judge",
                                       drop_label="演绎丢弃", material=material,
                                       speaker_card=speaker_card)
    if why:
        return BuildResult(None, why)
    c = card.corruptions[0]
    sample = {
        "id": card.card_id, "module": "judge", "version": SAMPLE_VERSION,
        "genre": card.axes.genre, "pack": card.pack, "material": material,
        "narration": r.text, "expect": c.expect, "category": c.category,
        "speaker": card.material.present[0],
        # 发现⑧：judge 的标签校验需要「矛盾依据 + 说话人角色卡」——
        # `ooc` 类在工厂路径上没有任何程序校验（锚点校验与 OOC 无关），故随样本交付这两项，
        # 交给 §7.5 标签自检（同时也是审计材料：人眼能一眼看出该样本在考什么）。
        "detail": c.detail, "speaker_card": speaker_card,
        "meta_soft": _meta_soft(r.text),                  # 发现⑩：温和元词（只记不杀）
        "name_confusables": _name_confusables(card, r.text),   # 发现⑩-D：近误人名
        **_length_fields(card, r.text),
    }
    # **出厂门禁接在产线上**（原稿定义了 hook_gate 却从未调用 = 门禁不存在）：
    # confab 撞说话人角色卡会多开一条「设定矛盾」通路，拦截率虚高、跨包不可比。
    if hooked := hook_gate(sample, pack):
        return BuildResult(None, f"confab 撞卡: {'/'.join(hooked)}")
    return BuildResult(sample)


def _speaker_card_text(pack: WorldPack, speaker: str) -> str:
    """说话人角色卡的四个字段拼成一段（与 `hook_gate` 的比对口径同源：`CARD_FIELDS`）。"""
    spec = pack.npcs.get(speaker)
    if spec is None:
        return ""
    return "\n".join(f"{f}：{spec.model_dump().get(f)}" for f in CARD_FIELDS)


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
    hist, why = _verbalize_until_label_ok(llm, card, module="compress",
                                          purpose="verbalize_compress",
                                          drop_label="历史演绎丢弃")
    if why:
        return BuildResult(None, why)
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
        "meta_soft": _meta_soft(hist.text),               # 发现⑩：温和元词（只记不杀）
        **_length_fields(card, hist.text),
        "preserve_points": [{"text": p.text, "anchors": p.anchors}
                            for p in card.preserve_points],
    })


# ---------------------------------------------------------------------------
# 质量门与出厂门禁（spec §9.2：card_hook_check 复用不重写）
# ---------------------------------------------------------------------------

GATE_MAX_DROP_RATE = 0.30   # 丢弃率超阈 → 批作废（先停产线，不硬凑量）
QUALITY_SAMPLE_RATE = 0.20  # §7.4 质检员抽检比例（常驻关卡）
LABEL_CHECK_RATE = 0.20     # §7.5 标签自检抽检比例（发现①⑦ 的防复发层）


@dataclass
class BatchStats:
    built: int = 0
    dropped: int = 0
    reasons: Counter = dc_field(default_factory=Counter)
    # 丢弃留档（2026-09-13 验收后补）：原先只记"丢了几张"，事后无法回答"丢的是谁"——
    # 发现④⑥ 的诊断都卡在这里（要么重跑花钱，要么只能猜）。现在按原因记 id，随批落盘。
    dropped_ids: dict[str, list[str]] = dc_field(default_factory=dict)
    # 完整原因（含撞词/缺项细节）：聚合键会把 `confab 撞卡: 甲乙/丙丁` 截成 `confab 撞卡` ✗，
    # 于是下一轮想修"撞的是哪些词"就无从下手（2026-09-13 规模预演：judge 丢弃率 30%，
    # 最大单项是 confab 撞卡 12.5%，却看不到撞词）→ 明细随批落盘。
    dropped_detail: dict[str, dict[str, str]] = dc_field(default_factory=dict)

    def drop(self, reason: str, card_id: str) -> None:
        key = reason.split(":")[0]
        self.dropped += 1
        self.reasons[key] += 1
        self.dropped_ids.setdefault(key, []).append(card_id)
        self.dropped_detail.setdefault(key, {})[card_id] = reason


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
# judge 的**家族配额**（plan-phase1-data.md §4.2.2 / §4.5，2026-09-12 按实测缺口定）：
# confab 必须占 ≥40% —— 因为"judge 训练量的分配必须向该家族倾斜，否则平均分会被 ooc/setting 盖住"，
# 而 confab 正是学生模型最弱的一课（同难口径实测漏 ~70%）。
# ⚠️ 这道门**原先不存在**（`quota_gaps` 只查题材与 compress 长档）→ 生产批 train judge 实测
# confab 仅 29.6% 而无人报警（2026-09-14 发现）。教训：**规格里写了配额，就必须有代码在检查它**。
JUDGE_CATEGORY_FLOORS = {"confab": 0.40, "setting": 0.25, "ooc": 0.20}
JUDGE_PER_GENRE_CLASS_MIN = 5    # §4.5：每题材每类 ≥5
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
    jud = by_mod.get("judge", [])
    if jud:
        cats = Counter(r.get("category", "?") for r in jud)
        for c, floor in JUDGE_CATEGORY_FLOORS.items():
            n = cats.get(c, 0)
            if n / len(jud) < floor:
                gaps.append(
                    f"judge 家族 {c}: {n}/{len(jud)} = {n / len(jud):.1%} < {floor:.0%}"
                    "（plan-phase1-data §4.2.2：confab≥40% setting≥25% ooc≥20% ——"
                    f"confab 是学生最弱的一课，欠配会被 ooc/setting 盖住平均分；"
                    f"当前 {dict(cats)}）")
        for (g, c), n in sorted(Counter((r.get("genre", "?"), r.get("category", "?"))
                                        for r in jud).items()):
            if n < JUDGE_PER_GENRE_CLASS_MIN:
                gaps.append(f"judge 覆盖 {g}/{c}: {n} < {JUDGE_PER_GENRE_CLASS_MIN}（§4.5 每题材每类 ≥5）")
    return gaps


def write_side_file(path: pathlib.Path, payload: dict) -> None:
    """写"侧证据文件"（标签自检清单等）：**自动建父目录**。

    为什么需要：标签自检写在 `write_layer` **之前**，而首次用全新的 `--out` 时层目录还不存在
    → `FileNotFoundError` **把整批跑完的成果全丢掉**（2026-09-13 规模预演实测踩到：
    26.8 分钟的调用、无任何产出）。此前每次跑 `data/route-a/{layer}/` 都已存在，故一直没露头。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def write_layer(layer: str, samples: list[dict], out_dir: pathlib.Path,
                progress: dict | None = None) -> dict:
    """出库：{layer}/{module}.jsonl + manifest.json（sha256/计数/冻结标志）。

    `progress`（2026-09-13 断点续跑改造）：发布可以**分模块、分片**进行，所以"这份数据集
    是不是最终态"必须能从 manifest 里读出来 —— 否则按 300/1000 发布出来的半成品，
    与全量成品长得一模一样（下游只会看到"就这么点数据"）。
    """
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
                "frozen": False, "modules": {}}
    if progress:
        manifest["progress"] = progress
        manifest["complete"] = all(p["pending"] == 0 for p in progress.values())
    # **eval 冻结纪律**：只有"已声明目标全部达成"的 eval 才配叫 `frozen`。
    # 2026-09-14 实测踩到：分阶段构建 eval（先产 200 张看质量）时，中间态被写成
    # `frozen=True` + `complete=True`，而它只有 198/1000 条 —— 读的人会把它当成那杆"尺子"，
    # 而尺子的完整性是决策的前提（决策 14）。
    # 无 `progress` 时（直接调用/测试）无从判断，沿用旧行为，不误伤。
    manifest["frozen"] = bool(layer == "eval"
                              and (progress is None or manifest.get("complete", False)))
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
    if manifest["frozen"]:  # 冻结纪律（spec §8 + plan-phase1-data.md §3.3 扩展）
        with (layer_dir / "OPEN_LOG.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- {manifest['written_at']} WRITE 出库 "
                     f"{sum(m['count'] for m in manifest['modules'].values())} 条；"
                     "此后每次打开（读取用于决策）须在此追加一行计数。\n")
        print("[eval 层已冻结] 请打 git tag：git tag eval-route-a-YYYYMMDD")
    return manifest


def _journal_entry(card: ScenarioCard, i: int, r, seen: set[str]) -> dict:
    """一枚日志行。**前缀去重在这里判**（口径与旧 `dedup` 一致：先产出的留、后产出的丢）。

    为什么去重必须挪到产出当时：样本一旦入日志就"已完成"，续跑不会再产它；若去重仍留到
    最后统一做，那被丢掉的那张卡在续跑时会被当成"没去过重"或"已丢"——两种口径都会算错账。
    """
    if r.sample is None:
        return {"i": i, "status": worklog.DROPPED, "card_id": card.card_id,
                "reason": r.dropped_reason}
    rows, dup = dedup([r.sample], seen)   # 去重口径复用 dedup（单一真源，不另写一份）
    if dup:
        return {"i": i, "status": worklog.DROPPED, "card_id": card.card_id,
                "reason": "前缀去重"}
    return {"i": i, "status": worklog.KEPT, "card_id": card.card_id, "sample": rows[0]}


def card_category(module: str, seed_base: int, i: int) -> str:
    """卡面类别（judge 独有）——**与 `build_judge_sample` 同源**（`card.corruptions[0].category`）。

    类别是 `(seed, seq, module)` 的确定性函数，所以**零成本**就能知道某个索引是哪一类；
    这正是"定向补产"能把配额补上而不浪费任何一次调用的前提。
    """
    card = generate_card(card_seed(seed_base, i, module), i, module)
    return card.corruptions[0].category if card.corruptions else ""


def _filter_by_category(indices: list[int], module: str, seed_base: int,
                        only: set[str]) -> tuple[list[int], list[int]]:
    """按卡面类别分流 → `(本次要产的, 本次跳过的)`。`only` 为空 = 全产（默认行为）。

    为什么"跳过"要显式落日志（`skipped` 状态）而不是留着 pending：
    ① 留着 pending → `complete` 永远 False，半成品标志失真；
    ② 记成 dropped → 丢弃率（质量指标）与 30% 质量门被"主动跳过"污染。
    """
    if not only:
        return list(indices), []
    keep, skip = [], []
    for i in indices:
        (keep if card_category(module, seed_base, i) in only else skip).append(i)
    return keep, skip


def _produce(llm, module: str, indices: list[int], seed_base: int, sampling: str,
             journal: worklog.Journal, seen: set[str]) -> int:
    """按索引产卡并**每张卡立刻入日志**（中断只丢正在产的那一张）。返回本次新增条数。"""
    builders = {"extract": build_extract_sample, "judge": build_judge_sample}
    for i in indices:
        # seed 走 card_seed（层内按模块错开）：三模块共用 seed_base+i 会让 card_id 撞车（发现①）
        card = generate_card(card_seed(seed_base, i, module), i, module)
        if module == "compress":
            r = build_compress_sample(llm, card, sampling=sampling)
        else:
            r = builders[module](llm, card)
        journal.append(_journal_entry(card, i, r, seen))
    return len(indices)


def _stats_from_journals(journals: dict[str, worklog.Journal]) -> BatchStats:
    """批统计**从日志重算**（不是"本次跑了多少"）。

    否则分片续跑会把丢弃率算成本次那一小片的（300 张里丢 3 张 = 1%，而整层可能是 22%）——
    质量门就形同虚设。`rejected`（质检剔除）计入 built：它确实产出来了，剔除另计
    （与旧版 `drop_flagged` 的账法一致：built − 剔除 = 出库）。
    """
    stats = BatchStats()
    for j in journals.values():
        for e in j.entries_in_order():
            if e["status"] in (worklog.KEPT, worklog.REJECTED):
                stats.built += 1
            elif e["status"] == worklog.SKIPPED:
                continue          # 定向跳过的索引：**不是丢弃**（丢弃率与质量门都不得计入）
            else:
                stats.drop(e.get("reason") or "未记原因", e["card_id"])
    return stats


def _judge_quality(llm, journals: dict[str, worklog.Journal]) -> list[str]:
    """§7.4 质检员（常驻关卡）：只判**还没判过的**，判定写回日志（返回点名剔除的 id）。

    为什么判定要入日志：旧版每次发布都对整层重抽 20% —— 分片发布时累计重判（10 片 ≈ 多花
    ¥11），更要命的是同一条样本**这次过、下次不过**，出库内容随发布次数抖动。
    """
    pool = [(m, e) for m in sorted(journals) for e in journals[m].unjudged()]
    if not pool:
        return []
    samples = [e["sample"] for _, e in pool]
    # 抽检位置与 quality_sample **同源**（`sample_positions` 是唯一真源）：不这样就没法记"判过哪几条"
    pos = set(sample_positions(len(samples), QUALITY_SAMPLE_RATE, QUALITY_SAMPLE_SEED))
    bad = set(quality_sample(llm, samples, rate=QUALITY_SAMPLE_RATE))
    for n, (mod, e) in enumerate(pool):
        judged = n in pos
        # **判据用 `id in bad` 而不是 `judged and id in bad`**：`bad` 就是"评委说这条不行"，
        # 与抽样位置无关（`judged` 只用于报表"抽检了几条"）。位置与判定不一致时（例如替身/口径
        # 变更），以**评委的话**为准 —— 否则一条被判坏的样本会因为"没抽到它"而进数据集。
        fail = samples[n]["id"] in bad
        upd = {**e, "sampled": judged, "status": worklog.REJECTED if fail else worklog.KEPT,
               "quality": (0 if fail else 2) if judged else 1}   # 0 不通过 / 1 未抽检 / 2 通过
        if fail:
            upd["reason"] = "质检自然度<1"
        journals[mod].append(upd)
    print(f"[质检] 抽检 {len(pos)}/{len(samples)} 条（口径 {QUALITY_SAMPLE_RATE:.0%}，"
          f"未抽检 {len(samples) - len(pos)} 条）→ 自然度 <1 剔除 {len(bad)} 条；"
          "判定已入日志（续跑/再发布不重判）")
    return sorted(bad)


def _layer_seen_file(out_dir: pathlib.Path, layer: str) -> pathlib.Path:
    return out_dir / "seen" / f"{layer}.json"


def _write_layer_seen(out_dir: pathlib.Path, layer: str,
                      journals: dict[str, worklog.Journal]) -> int:
    """本层指纹落盘（**纯函数于本层日志**）→ 跨层去重的durable真源。

    为什么**按层分文件**（而不是旧版那个全局 `seen_fingerprints.json`）：全局文件里删不掉某一层
    的指纹，于是 `--fresh` 重做时——重造出的样本文本与旧版**前 64 字往往一样**（同一张卡 + 同一
    演绎提示词）——会被自己的旧指纹当成重复**全灭**。分文件后，本层重做 = 本层指纹随日志一起
    重算，别的层不受影响。

    ⚠️ 迁移：旧的 `data/route-a/seen_fingerprints.json` 不再被读取（它记的是**上一代**数据集
    的指纹，拿它去卡新数据集只会造成"越像越丢"）；随旧数据集一起删掉即可。
    """
    fps = sorted({fingerprint(_sample_text(e["sample"]))
                  for j in journals.values() for e in j.entries.values() if "sample" in e})
    f = _layer_seen_file(out_dir, layer)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(fps), encoding="utf-8")
    return len(fps)


def _seen_from_disk(out_dir: pathlib.Path, layer: str,
                    pending: dict[str, set[int]]) -> set[str]:
    """跨层去重真源 = **别的层**的指纹文件 ∪ 各层日志里已产样本的指纹。

    两条都必须：
    - **别的层**：本层重做时不能拿自己的旧指纹卡自己（见 `_write_layer_seen`）；
    - **排除本次要重产的索引**（`pending`）：质检剔除的样本重造时，它自己的前缀指纹还在日志里
      —— 不排除就会把重造版当成"重复"再丢一次，那张卡永远造不出来。
    """
    seen: set[str] = set()
    for other in ("train", "dev", "eval"):
        if other != layer:
            f = _layer_seen_file(out_dir, other)
            if f.exists():
                seen |= set(json.loads(f.read_text(encoding="utf-8")))
        for mod in ("extract", "judge", "compress"):
            j = worklog.read_journal(worklog.journal_path(out_dir, other, mod))
            skip = pending.get(mod, set()) if other == layer else set()
            seen |= {fingerprint(_sample_text(e["sample"]))
                     for i, e in j.entries.items()
                     if i not in skip and "sample" in e
                     and e["status"] in (worklog.KEPT, worklog.REJECTED)}
    return seen


def _label_side(out_dir: pathlib.Path, layer: str) -> pathlib.Path:
    return out_dir / layer / f"label-check-{layer}.json"


def _label_runs(side: pathlib.Path) -> list[dict]:
    """读标签自检证据文件。**兼容旧格式**（旧版是单个 dict，每次覆盖 → 分片发布时证据被冲掉）。"""
    if not side.exists():
        return []
    data = json.loads(side.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def _label_recheck(llm, out_dir: pathlib.Path, layer: str, samples: list[dict]) -> dict:
    """§7.5 标签自检（**报告-only**）：只查"还没被抽检过的那批"，证据按次**追加**。

    旧版每次发布覆盖证据文件，分片发布时前一版的证据直接消失、累计覆盖量也无从统计。
    """
    side = _label_side(out_dir, layer)
    covered = {i for r in _label_runs(side) for i in r.get("produced_ids", [])}
    fresh = [s for s in samples if s["id"] not in covered]
    if not fresh:
        return {}
    lc = label_check(llm, fresh, rate=LABEL_CHECK_RATE)
    lc.update({"at": datetime.date.today().isoformat(), "produced": len(fresh),
               "produced_ids": [s["id"] for s in fresh]})
    write_side_file(side, _label_runs(side) + [lc])
    return lc


def _progress(journals: dict[str, worklog.Journal], targets: dict[str, int]) -> dict:
    """每模块的发布进度（进 manifest）。**没声明目标也没产出的模块不进表** ——
    列一行 `target 0 / pending 0` 会让读的人以为"这个模块已完成"，而它其实还没开始。"""
    out = {}
    for mod, j in sorted(journals.items()):
        target = targets.get(mod, 0)
        if not target and not j.entries:
            continue
        out[mod] = {"target": target, "kept": len(j.kept()),
                    "dropped": len(j.done) - len(j.kept()) - len(j.skipped),
                    "skipped": len(j.skipped),
                    "rejected": len(j.rejected), "pending": len(j.pending(target)) if target else 0}
    return out


def publish_layer(llm, *, layer: str, out_dir: pathlib.Path, journals: dict[str, worklog.Journal],
                  targets: dict[str, int]) -> dict | None:
    """发布一层：质检判定（幂等）→ 质量门 → 标签自检 → 配额 → 出库 → 留档。

    **发布是幂等的**：内容全部来自日志（不是"本次产了多少"），故分模块/分片跑多次发布会得到
    同一份数据集（旧版会把 manifest 覆盖成"只剩本次模块"，`extract.jsonl` 还在盘上却无人认领）。
    """
    stats = _stats_from_journals(journals)
    if why := quality_gate(stats):
        print(f"[✗] 质量门未过：{why}——批作废，先停产线")
        return None
    bad_ids = _judge_quality(llm, journals)                    # 判定写回日志 → 再发布不重判
    samples = [s for j in journals.values() for s in j.built_samples()]
    if bad_ids:
        samples, removed = drop_flagged(samples, bad_ids)
        extra = removed - len(bad_ids)
        print(f"[质检] 自然度 <1 剔除 {removed} 条（须重造，点名 {len(bad_ids)} 个 id"
              + (f"，**按 id 连坐多剔 {extra} 条**" if extra else "") + f"）：{bad_ids[:10]}")
    if lc := _label_recheck(llm, out_dir, layer, samples):
        print(f"[标签自检] 本批 {lc['produced']} 条抽检 {lc['checked']} 条 → 报告不一致 "
              f"{len(lc['violations'])} 条（**仅报告不剔除**，证据 {_label_side(out_dir, layer).name}）；"
              f"未判定 {len(lc['unknown'])} 条（**未知 ≠ 通过**）；跳过 {lc['skipped']} 条（无标签可校）")
    for g in quota_gaps(samples):
        print(f"[配额缺口] {g}")
    progress = _progress(journals, targets)
    manifest = write_layer(layer, samples, out_dir, progress=progress)
    if not manifest.get("complete"):
        print(f"[未完成] 本层仍有待产卡：{ {m: p['pending'] for m, p in progress.items() if p['pending']} }"
              "——**重跑同一命令即可续跑**（已产的不重产）")
    if stats.dropped_ids:  # 丢弃留档：事后要能回答"丢的是哪张卡"（发现④⑥ 的诊断口子）
        drop_file = out_dir / layer / f"dropped-{layer}.json"
        drop_file.write_text(json.dumps(
            {"counts": {k: len(v) for k, v in stats.dropped_ids.items()},
             "ids": stats.dropped_ids,
             "detail": stats.dropped_detail},          # 完整原因（含撞词/缺项）——修下一轮靠它
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[丢弃留档] {drop_file.relative_to(out_dir)}（{stats.dropped} 张，按原因分列）")
    n_fp = _write_layer_seen(out_dir, layer, journals)
    print(f"[去重真源] seen/{layer}.json（{n_fp} 条指纹，供 dev/eval 跨层去重）")
    confab = [s for s in samples if s.get("category") == "confab"]
    if confab:  # 决策 16：人读清单随批交付
        mr = out_dir / layer / f"confab-manual-review-{layer}.jsonl"
        mr.write_text("\n".join(json.dumps(manual_review_row(s), ensure_ascii=False)
                                for s in confab) + "\n", encoding="utf-8")
        print(f"[人读清单] {mr}（{len(confab)} 条 confab，决策 16 待人工复核）")
    return manifest


def print_status(out_dir: pathlib.Path, layer: str, seed_base: int, sampling: str,
                 targets: dict[str, int]) -> None:
    """`--status`：只读进度（零 API 成本）——十来小时的批，随时要能回答"跑到哪了"。"""
    print(f"[进度] {out_dir}/{layer}")
    for mod in ("extract", "judge", "compress"):
        j = worklog.read_journal(worklog.journal_path(out_dir, layer, mod))
        if not j.entries and not j.header:
            if mod in targets:
                print(f"  {mod:<9} 未开始（目标 {targets[mod]}）")
            continue
        want = _journal_header(layer, mod, seed_base, sampling)
        note = worklog.header_mismatch(j.header, _header_check(want, mod, producing=False))
        target = targets.get(mod, 0)
        pend = len(j.pending(target)) if target else "-"
        print(f"  {mod:<9} 已产 {len(j.done):>5}（保留 {len(j.kept()):>5} / 丢弃 "
              f"{len(j.done) - len(j.kept()) - len(j.skipped):>4} / 质检剔除 {len(j.rejected):>3}"
              + (f" / 定向跳过 {len(j.skipped)}" if j.skipped else "") + f"）"
              f" 待产 {pend}" + (f"  ⚠ 头不一致：{note}" if note else ""))
        if j.corrupt:
            print(f"            ⚠ 日志有 {j.corrupt} 行解析不出（**不算已完成**，会被重产）")


def _model_names() -> list[str]:
    """日志头里的模型三元组（主/judge/compress 档）：换模型续跑 = 两种分布混一份数据集。"""
    from game_agent.config import load_settings

    s = load_settings()
    return [s.model, s.judge_model, s.compress_model]


def factory_version() -> str:
    """**生成器指纹**：提示词 + 卡生成器源码 + 关键旋钮（断点续跑日志头的守卫）。

    口径：宁可**过度失效**（改了 `verbalize.py` 的注释也会让旧日志作废、重做要花钱），
    也不能**失效不足**（两代产线的样本混进同一份数据集，出库时**看不出来**）。
    真要接着旧日志跑，只能显式 `--fresh` —— 账要认在明处。

    `cards.py` / `verbalize.py` 收**源码摘要**而不是几个常量：卡由 `(seed, i, module)` 确定性
    生成（`generate_card` 的 docstring 就是这条契约），改模板/名字池/轴分布都会让同一个 `i`
    变出**另一张卡**，而那不在任何常量里。
    """
    from game_agent.compression import COMPRESS_SYSTEM, SUMMARY_MAX_TARGET
    from game_agent.memory import EXTRACT_SYSTEM
    from scripts.rubric_judge import label_check_version, prompt_version

    from . import cards as cards_mod
    from . import verbalize as verbalize_mod

    knobs = {
        "sample_version": SAMPLE_VERSION,
        "extract_system": EXTRACT_SYSTEM,
        "compress_system": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET),
        "verbalize_system": verbalize_mod.VERBALIZE_SYSTEM,
        "prompt_version": prompt_version(),
        "label_check_version": label_check_version(),
        "hook_n": FACTORY_HOOK_N, "prefix_n": PREFIX_N,
        "length_tolerance": LENGTH_TOLERANCE, "candidates_n": CANDIDATES_N,
        "summary_temperature": SUMMARY_TEMPERATURE,
        "long_input_tokens": LONG_INPUT_TOKENS,
        "cards_py": file_digest(cards_mod.__file__),
        "verbalize_py": file_digest(verbalize_mod.__file__),
    }
    return hashlib.sha256(json.dumps(knobs, ensure_ascii=False, sort_keys=True).encode(
        "utf-8")).hexdigest()[:16]


def _journal_header(layer: str, module: str, seed_base: int, sampling: str) -> dict:
    return {"journal": worklog.JOURNAL_VERSION, "layer": layer, "module": module,
            "seed_base": seed_base, "sampling": sampling,
            "factory_version": factory_version(), "models": _model_names()}


def _make_llm():
    """建生产 LLM（usage 记账 + 侧信道路由）；未配 key → `None`。测试的替身注入点。"""
    from game_agent.config import load_settings
    from game_agent.llm import LLMClient
    from game_agent.usage import UsageTracker

    # usage 记账：**必须显式建 tracker 并传进 LLMClient** —— 落盘只发生在
    # `LLMClient._record_usage`，`complete_checked` 本身**不接触** UsageTracker。
    # 走 `from_settings` 同时带来：模型路由（judge/compress 档）+ 侧信道关思考。
    settings = load_settings()
    if not settings.has_api_key:
        return None
    return LLMClient.from_settings(settings, [],
                                   tracker=UsageTracker("reports/usage-route-a.jsonl"))


def targets_file(out_dir: pathlib.Path, layer: str) -> pathlib.Path:
    return out_dir / layer / worklog.WORK_DIR / "targets.json"


def read_targets(out_dir: pathlib.Path, layer: str) -> dict:
    f = targets_file(out_dir, layer)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def update_targets(out_dir: pathlib.Path, layer: str, targets: dict[str, int],
                   *, persist: bool = True) -> dict:
    """层目标（**粘性**：取历史最大值，落盘）。

    为什么要落盘：发布可以分模块、分片进行，"这份数据是不是最终态"不能由**本次命令行写了多少**
    决定 —— 否则 `--extract 300`（将来要做 1000）发布出来的 300 条会被标成 `complete`，
    而它与成品长得一模一样。目标写在盘上后，`--status` 不带参数也能回答"还差多少"。

    `persist=False` 只给 `--status` 用（只读命令不写盘）。
    """
    cur = read_targets(out_dir, layer)
    for mod, n in targets.items():
        cur[mod] = max(int(cur.get(mod, 0)), int(n))
    if persist:
        f = targets_file(out_dir, layer)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cur, ensure_ascii=False, indent=2, sort_keys=True),
                     encoding="utf-8")
    return cur


def _header_check(want: dict, module: str, producing: bool) -> dict:
    """校验用的期望头（**只比对该模块真正在乎的字段**）。

    `sampling` 只对 compress 有意义（`_sampling_on` 只被 `build_compress_sample` 用：
    长档跑 n=4 拒绝采样）。若对 extract/judge 也比它，就会出现两种坏结果：
    ① 不带 `--sampling` 查进度时误报"换代"；② **忘了带标志的续跑被误拒**
    （而 extract 的产出与 sampling 毫无关系）。
    """
    drop = {"sampling"} if (module != "compress" or not producing) else set()
    return {k: v for k, v in want.items() if k not in drop}


def _load_journals(out_dir: pathlib.Path, layer: str, seed_base: int, sampling: str,
                   targets: dict[str, int], fresh: bool) -> tuple[dict, str | None]:
    """装载本层三个模块的日志（**三个都装**：发布要出整层，不只出本次请求的模块）。

    - 请求产出的模块：校验完整头（compress 含 sampling，见 `_header_check`）
    - 其余模块：只校验 `factory_version`/模型/种子段 —— 采样档不影响可比性；
      而 `factory_version`/模型/种子段不一致必须拒绝（否则会把两代产线一起发布出去）。
    """
    journals: dict[str, worklog.Journal] = {}
    for mod in ("extract", "judge", "compress"):
        jp = worklog.journal_path(out_dir, layer, mod)
        producing = mod in targets
        if producing and fresh and jp.exists():
            was = len(worklog.read_journal(jp).done)
            jp.unlink()
            print(f"[重做] {layer}/{mod}：清掉进度（{was} 张已产样本作废，"
                  "**已花的调用费不可回收**）")
        j = worklog.read_journal(jp)
        want = _journal_header(layer, mod, seed_base, sampling)
        if (j.header or j.entries) and (
                why := worklog.header_mismatch(j.header, _header_check(want, mod, producing))):
            return {}, (f"{layer}/{mod} 的进度与当前产线不一致：{why}\n"
                        "    —— 续跑会把**两代产线**的样本混进同一份数据集（出库时看不出来）。\n"
                        "    要么把代码改回去，要么 --fresh 重做（已产样本作废，账要认）。")
        journals[mod] = worklog.Journal.open(out_dir, layer, mod, want)
    return journals, None


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
    p.add_argument("--status", action="store_true",
                   help="只打印进度（零成本）：已产/丢弃/质检剔除/还差多少")
    p.add_argument("--fresh", action="store_true",
                   help="重做：清掉本次请求模块的进度（已产样本作废）")
    p.add_argument("--no-publish", action="store_true",
                   help="只产卡入日志，不质检不出库（攒够了再跑一次发布）")
    p.add_argument("--only-category", default="",
                   help="定向补产：只产这些类别的卡（judge 专有，逗号分隔如 confab,setting）；"
                        "其余索引记为 skipped（**不算丢弃**，丢弃率与质量门不受影响）")
    args = p.parse_args(argv)

    seed_base = args.seed_base if args.seed_base is not None else LAYER_BASE[args.layer]
    if layer_of(seed_base) != args.layer:
        print(f"[✗] seed 与层不一致（seed={seed_base} 属 {layer_of(seed_base)} 层）——"
              "三层空间隔离，spec §8")
        return 1
    out_dir = pathlib.Path(args.out)
    only = {c.strip() for c in args.only_category.split(",") if c.strip()}
    if bad := only - set(JUDGE_CATEGORY_FLOORS):
        print(f"[✗] --only-category 只认 {'/'.join(sorted(JUDGE_CATEGORY_FLOORS))}，收到 {sorted(bad)}")
        return 1
    # `requested` = **本次显式请求**（唯一驱动产出的东西：说好只跑一部分，就不能顺带把别的模块也跑了）
    requested = {mod: n for mod, n in (("extract", args.extract), ("judge", args.judge),
                                       ("compress", args.compress)) if n}
    if only and "judge" not in requested:
        print("[✗] --only-category 只对 judge 有意义（类别是 judge 卡独有的轴）")
        return 1
    # `targets` = 层目标（粘性，取历史最大值）：只用于**账目**（manifest 的 complete / 待产数），
    # **不驱动产出** —— 说好只跑一部分，就不能因为盘上还有个更大的目标而顺带把别的模块也跑了
    targets = update_targets(out_dir, args.layer, requested, persist=False)

    if args.dry_run:  # 配额预演：零 API 成本，先看轴分布再决定产多少
        for mod, n in sorted(requested.items()):
            genres = Counter(generate_card(card_seed(seed_base, i, mod), i, mod).axes.genre
                             for i in range(n))
            long_n = sum(1 for i in range(n)
                         if generate_card(card_seed(seed_base, i, mod), i, mod
                                          ).history_spec.target_tokens >= LONG_INPUT_TOKENS)
            print(f"[dry-run] {mod}: {n} 卡，题材 {dict(genres)}，长输入 {long_n}/{n}")
        return 0

    if args.status:
        print_status(out_dir, args.layer, seed_base, args.sampling, targets)
        return 0

    journals, err = _load_journals(out_dir, args.layer, seed_base, args.sampling,
                                   requested, args.fresh)
    if err:
        print(f"[✗] {err}")
        return 1
    targets = update_targets(out_dir, args.layer, requested)   # 落盘（粘性目标）

    pending = {m: j.pending(requested[m]) for m, j in journals.items() if m in requested}
    for m in sorted(journals):
        j = journals[m]
        if m not in requested and not j.entries:
            continue
        print(f"[{m}] 已完成 {len(j.done)}/{targets.get(m, 0)}"
              + (f"，质检待重造 {len(j.rejected)}" if j.rejected else "")
              + (f"，本次新增 {len(pending[m])}" if m in requested else ""))
    n_new = sum(len(v) for v in pending.values())
    if not n_new and args.no_publish:
        print("[✓] 目标已全部产出（--no-publish：未发布）")
        return 0

    llm = _make_llm()
    if llm is None:
        print("[✗] 未配置 DEEPSEEK_API_KEY（.env）")
        return 1

    seen = _seen_from_disk(out_dir, args.layer, {m: set(v) for m, v in pending.items()})
    try:
        for mod in ("extract", "judge", "compress"):
            if not pending.get(mod):
                continue
            keep, skipped = _filter_by_category(pending[mod], mod, seed_base, only)
            for i in skipped:   # 先落 skipped：中断也不影响"这批索引已处理过"这个事实
                journals[mod].append({"i": i, "status": worklog.SKIPPED,
                                      "card_id": f"sc-{card_seed(seed_base, i, mod)}-{i:04d}",
                                      "reason": f"定向补产：本次只要 {'/'.join(sorted(only))}"})
            if skipped:
                print(f"[{mod}] 定向跳过 {len(skipped)} 个索引（本次只要 "
                      f"{'/'.join(sorted(only))}；**不算丢弃**，逐个已入日志）")
            if not keep:
                continue
            _produce(llm, mod, keep, seed_base, args.sampling, journals[mod], seen)
            print(f"[{mod}] 本次产出 {len(keep)} 张（**已逐张入日志**，随时可中断）")
    except KeyboardInterrupt:
        n_done = sum(len(j.done) for j in journals.values())
        print(f"\n[中断] 已落盘 {n_done} 张（{worklog.WORK_DIR}/ 下的工作日志）——"
              "**重跑同一命令即从断点续跑**，已产的不重产。")
        return 130

    if args.no_publish:
        print(f"[✓] 本次新增 {n_new} 张已入日志（--no-publish：未质检、未出库）")
        return 0
    manifest = publish_layer(llm, layer=args.layer, out_dir=out_dir, journals=journals,
                             targets=targets)
    if manifest is None:
        return 1
    stats = _stats_from_journals(journals)
    print(f"[✓] {args.layer} 出库 "
          f"{sum(m['count'] for m in manifest['modules'].values())} 条"
          f"（丢弃 {stats.dropped}：{dict(stats.reasons)}；complete={manifest.get('complete')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

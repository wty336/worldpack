"""rubric 评委（spec §7）：选优 / 打分 / 成对比较 / 探针校准 / 自然度质检，一个入口多种模式。

纪律（spec §7.3）：
- 评委一律 **temp=0**（判定类；演绎才是 0.8~1.0）；
- 成对比较 = 匿名 + **位置交换 ×3 重复 = 每对 6 次**调用（spec §10.2 口径）；
- 探针检出口径——打分制 = 探针的**对应维度**落 0 分档；成对比较 = 探针（坏）与干净样本配对须判负；
- 评委只评形态，**永不输出标签**（标签在卡上）。

预算：`MAX_TOKENS = budgets.MIN_CALL_TOKENS`（**单一真源**，不写死数字）。
思考模型会把推理链算进 `max_tokens`，给 200 会被思考吃光 → 空判词（retro §5.3 那批 bug）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import random
import re
import sys

from game_agent.budgets import MIN_CALL_TOKENS, complete_checked

from scripts.scenario_factory.cards import PreservePoint

DIMS = ("保真", "简洁", "结构", "流畅")
SCORE_TEMPERATURE = 0.0
PAIRWISE_REPEATS = 3  # ×2 位置 = 6 次调用
# 评委单次预算：**不得低于 budgets.MIN_CALL_TOKENS（500）**——硬规则（budgets.py:20）。
# 走单一真源、不写死数字，避免两处漂移。
MAX_TOKENS = MIN_CALL_TOKENS

RUBRIC_SYSTEM = (
    "你是压缩摘要的质量评委。对照【材料】与【必保全要点】，按四维量规给【待评摘要】打 0/1/2 分，"
    "只输出一行 JSON，不要任何额外文字。\n"
    # 发现② 修复（2026-09-13 实跑取证）：原措辞写"要点全在"→ 判官按 **anchor 字面还在** 就放行。
    # 实测：探针删掉了含「沈砚」的两条承诺句（"答应把沈砚转交给灰雀号"），只因别处还提「灰雀号」，
    # 判官仍给保真 2 → 探针检出 67% < 90% 门、整批作废。判据改为"**仅凭摘要能否复原该要点**"。
    # 又一轮实测（同日晚）：写"完全无法复原"仍被读成可以部分给分（要点整条被删仍给 1）
    # → 0 分档改为**判存在性**："有要点在摘要中整体缺失"，与探针的"整条删掉"严格对齐。
    "保真：2=每个要点都能**仅凭摘要复原**（关键专名与数字都在）且无材料外事实；"
    "1=要点都在，但其中 1 个丢了关键专名或数字（只能部分复原）；或 1 处轻微走样；"
    "0=**有要点在摘要中整体缺失**，或出现材料外事实。\n"
    "简洁：2=无复述冗余；1=轻微冗余；0=大段照搬或兜圈。\n"
    "结构：2=时间与因果清楚；1=轻微跳跃；0=支离破碎。\n"
    "流畅：2=自然书面中文；1=有语病不妨碍理解；0=难以卒读。\n"
    '输出格式：{"保真": 0, "简洁": 0, "结构": 0, "流畅": 0}'
)
PAIRWISE_SYSTEM = (
    "你是压缩摘要评委。对照【材料】与【必保全要点】，按保真>简洁>结构>流畅的优先级"
    "判断候选 A / 候选 B 谁更好。只输出一行 JSON："
    '{"winner": "A"} 或 {"winner": "B"} 或 {"winner": "tie"}。'
)

PROBE_KINDS = ("删要点", "注入虚构", "打乱结构")
_PROBE_DIM = {"删要点": "保真", "注入虚构": "保真", "打乱结构": "结构"}
# **探针分级**（发现② 的真根因，2026-09-13）：spec §7.2 明写 `保真` = "摘要是否引入源材料**不存在**的内容"，
# 并给出互补分工——"**规则管要点在不在，rubric 管多出来的坏东西**"。而 §7.3 的探针表把
# 「删要点」（考**覆盖**）挂在 `保真` 维上、要求判 0 分：**要求 rubric 判一个它设计上不判的东西**。
# 实测三批佐证：删要点探针 0/3 命中，而落入 rubric 真度量的两个坏法（注入虚构→保真、结构崩坏→结构）
# 命中 3/4。spec §11 风险表对这一类早有处方——"探针坏法分级，按级分别要求检出率"，
# 故按级：**显性级**（缺陷落在被度量的维度上）计入 ≥90% 门；**覆盖级**只作诊断（并同时用
# 程序口径验证：坏本里该要点的 anchor 与 4 字串**可离线证明**已消失 = 程序先杀会拦住它）。
_PROBE_GRADE = {"注入虚构": "explicit", "打乱结构": "explicit", "删要点": "coverage"}
_FABRICATED = ["北冥真人", "天外星舰", "幽冥鬼市"]  # 必不在任何材料内的虚构专名


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", text or "", re.S)
        if not m:
            raise ValueError(f"评委输出不含 JSON: {(text or '')[:80]!r}")
        return json.loads(m.group(0))


def _complete(llm, system: str, user: str, *, purpose: str) -> str:
    text, finish = complete_checked(
        llm, [{"role": "system", "content": system}, {"role": "user", "content": user}],
        purpose=purpose, max_tokens=MAX_TOKENS, temperature=SCORE_TEMPERATURE)
    if finish != "stop":
        raise ValueError(f"评委输出截断: finish={finish}")
    return text


def _points_text(preserve_points: list[PreservePoint]) -> str:
    return "；".join(p.text for p in preserve_points) or "（无）"


def score(llm, *, summary: str, material: str,
          preserve_points: list[PreservePoint],
          purpose: str = "rubric_score") -> dict[str, int]:
    """打分模式：四维各 0/1/2。评委输出异常 → ValueError（调用方计批次异常）。

    `purpose` 只作 usage 记账标签（成本回填要能拆开"评测打分"与"拒绝采样选优"），
    不改变路由（`model_for()` 只认 judge/compress，其余回退主模型）。
    """
    user = (f"【材料】\n{material}\n\n【必保全要点】\n{_points_text(preserve_points)}"
            f"\n\n【待评摘要】\n{summary}")
    d = _extract_json(_complete(llm, RUBRIC_SYSTEM, user, purpose=purpose))
    out = {k: int(d[k]) for k in DIMS}
    if any(v not in (0, 1, 2) for v in out.values()):
        raise ValueError(f"维度分越界: {out}")
    return out


def select(llm, *, candidates: list[str], material: str,
           preserve_points: list[PreservePoint]) -> int:
    """选优模式（拒绝采样用，spec §6）：总分最高者，同分取先。"""
    best, best_i = -1, 0
    for i, c in enumerate(candidates):
        total = sum(score(llm, summary=c, material=material, purpose="rubric_select",
                          preserve_points=preserve_points).values())
        if total > best:
            best, best_i = total, i
    return best_i


def _judge_once(llm, *, first: str, second: str, material: str,
                preserve_points: list[PreservePoint]) -> str:
    user = (f"【材料】\n{material}\n\n【必保全要点】\n{_points_text(preserve_points)}"
            f"\n\n【候选 A】\n{first}\n\n【候选 B】\n{second}")
    return str(_extract_json(
        _complete(llm, PAIRWISE_SYSTEM, user, purpose="rubric_pairwise")).get("winner"))


def pairwise(llm, *, a: str, b: str, material: str,
             preserve_points: list[PreservePoint]) -> str:
    """成对模式：返回 'A' | 'B' | 'tie'。匿名 + 位置交换 ×3 重复，多数票；平票 → tie。"""
    votes = {"A": 0, "B": 0, "tie": 0}
    for _ in range(PAIRWISE_REPEATS):
        w1 = _judge_once(llm, first=a, second=b, material=material,
                         preserve_points=preserve_points)
        votes[w1 if w1 in votes else "tie"] += 1
        w2 = _judge_once(llm, first=b, second=a, material=material,
                         preserve_points=preserve_points)
        votes[{"A": "B", "B": "A"}.get(w2, "tie")] += 1  # 换位的胜者映射回原始
    top = max(votes.values())
    winners = [k for k, v in votes.items() if v == top]
    return winners[0] if len(winners) == 1 else "tie"


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？；\n])", text) if s]


def make_probe(summary: str, preserve_points: list[PreservePoint], *,
               kind: str, rng: random.Random) -> str:
    """探针模式·程序改坏（spec §7.3）：返回一份**已知缺陷**样本。

    **"删要点"必须删干净**（发现② 修复）：原实现只删「第一个 anchor」命中的句子，
    而实测摘要里同一要点常有**多处**提及（"答应把沈砚转交给灰雀号"在人物关系与承诺两节各写一遍），
    于是删一处、留一处 → 判官按残留的另一个 anchor（`灰雀号`）放行 → 探针**形同没坏**，
    却被计入"漏检"。现在删掉命中**任一 anchor** 的句子；**一处都删不掉就报错**（无效探针 ≠ 漏检，
    由 `run_eval` 剔出统计并留痕——"空 = 未知 ≠ 通过"）。
    """
    if kind == "删要点":
        # **删到"该要点的痕迹全无"**（发现② 的第三次加固）：只删 anchor 句子不够——
        # 实测 `sc-22001-0001` 的要点是"欠密码本五十两，约定中秋前归还"，删掉金额与日期句后，
        # 另一条要点（密码本债主）仍承载"存在这笔债"→ 判官给保真 1（丢了数字）**是合理的**，
        # 而探针却期望 0 → 必然漏检。故改成按**要点文本的 ≥4 字串**一起删（含 anchor），
        # 删完由 `_点还原不了` 校验：要点文本的任何 4 字串都不再出现 = 可离线证明的"整条缺失"。
        point = preserve_points[0]
        marks = {point.text[i:i + 4] for i in range(max(0, len(point.text) - 3))}
        sentences = _sentences(summary)
        kept = [s for s in sentences
                if not any(a in s for a in point.anchors) and not any(m in s for m in marks)]
        if len(kept) == len(sentences):
            raise ValueError(
                f"删要点探针无效：要点 {point.anchors} 的 anchor 与 4 字串都不在摘要里（无法制造已知缺陷）")
        return "".join(kept) if kept else "（本案要点已删）"
    if kind == "注入虚构":
        return summary + f"后来{rng.choice(_FABRICATED)}现身，接管了一切。"
    if kind == "打乱结构":
        # 加固（发现②③ 耦合）：出库摘要是 **Markdown 分节要点表**，单纯打乱句子顺序仍可能被读成
        # "条目独立、结构尚可"——而干净材料上 `结构` 维本就 **26/26 全满分**（饱和），
        # 探针若不给到"支离破碎"就**注定漏检**。故连同骨架一起去掉：Markdown 标记剥除 + 顺序打散 +
        # 合并成一段流水文字（人类一眼即判 0 分档）。
        parts = [re.sub(r"[#*>\-\s]+", "", s) for s in _sentences(summary)]
        parts = [p for p in parts if p]
        if len(parts) < 2:
            return re.sub(r"[#*>\-\s]+", "", summary)[::-1]
        shuffled = parts[:]
        while shuffled == parts:
            rng.shuffle(shuffled)
        return "".join(shuffled)
    raise ValueError(f"未知探针坏法: {kind}")


def _probe_kinds(n: int, *, rng: random.Random) -> list[str]:
    """给 n 个探针位分配坏法：**保证至少 1 个显性级**。

    为什么不用 `rng.choice` 逐位随机：3 抽全是覆盖级（删要点）的概率不低，那时门**无从判定**
    （`probe_detection=None` → 批无效）——等于因为抽样运气废掉一整批。显性级打底、覆盖级只做点缀。
    """
    explicit = [k for k in PROBE_KINDS if _PROBE_GRADE[k] == "explicit"]
    coverage = [k for k in PROBE_KINDS if _PROBE_GRADE[k] == "coverage"]
    kinds = [explicit[i % len(explicit)] for i in range(n)]   # 显性打底
    for i in range(1, n):                                     # 富余位掺覆盖级（第 0 位留给显性）
        if coverage and rng.random() < 0.5:
            kinds[i] = coverage[i % len(coverage)]
    rng.shuffle(kinds)
    return kinds


def probe_detected(scores: dict[str, int], kind: str) -> bool:
    """打分制检出口径（spec §7.3）：探针的对应维度分落入 0 分档。"""
    return scores.get(_PROBE_DIM[kind]) == 0


# ---- §7.4 数据质检员（第三处评委用法）：抽检自然度，防演绎器退化回模板 ----
NATURALNESS_SYSTEM = (
    "你是数据质检员。只判断这段中文文本的**自然度/模板味**，不评判内容对错。"
    "2=像人写的自然叙事；1=略有拼凑感但不刺眼；0=明显填空式模板或复读机。"
    '只输出一行 JSON：{"自然度": 0}'
)


def naturalness(llm, *, text: str) -> int:
    """抽检单条文本的自然度（0~2）。§7.4：<1 剔除并重造。"""
    d = _extract_json(_complete(llm, NATURALNESS_SYSTEM, f"【待检文本】\n{text}",
                                purpose="rubric_quality"))
    v = int(d["自然度"])
    if v not in (0, 1, 2):
        raise ValueError(f"自然度分越界: {v}")
    return v


def quality_sample(llm, samples: list[dict], *, rate: float = 0.20,
                   seed: int = 20260912) -> list[str]:
    """按 rate 抽检合成样本，返回自然度 <1 的 id（调用方剔除并重造）。**常驻关卡**，非一次性。"""
    if not samples:
        return []
    rng = random.Random(seed)
    k = min(max(1, math.ceil(len(samples) * rate)), len(samples))
    bad: list[str] = []
    for i in sorted(rng.sample(range(len(samples)), k=k)):  # 升序消费，结果与顺序无关
        s = samples[i]
        if naturalness(llm, text=s.get("input") or s.get("narration") or "") < 1:
            bad.append(s["id"])
    return bad


# ---- §7.5 标签自检（第四处评委用法）：卡面标签 × 文本的**语义一致性** ----
# 为什么需要（2026-09-13，发现①⑦ 的防复发层）：① 与 ⑦ 是同一形态——**不报错、但产出坏标签**
# （① id 撞车让质检剔除连坐；⑦ 名字池不分语义槽 → `preserve_points` 声称的内容在材料里根本不成立），
# 一天内让决策 14 的 eval 例外条款触发了两次。程序侧只查得出"anchor 在不在"（字面），
# 查不出"这条事实在文本里是否**被当成本身所述的那种东西**成立"——那正是演绎器会悄悄改掉的地方
# （把「旧书店」写成一家书店、把「密码本」写成债主）。故补这一道**语义抽检**。
LABEL_CHECK_SYSTEM = (
    "你是标签校验员。给定【事实标签】若干条与【文本】，逐条判断该事实在文本中**是否成立**。\n"
    "成立 = 文本把该事实当作**其字面所述的那种东西**呈现"
    "（如「玩家的装备名为「黄铜齿轮」」就要文本里真有一件叫黄铜齿轮的装备）；\n"
    "不成立 = 文本里找不到该事实的依据，**或把它重新解释成了别的东西**"
    "（如把「旧书店」写成一家书店、把「密码本」写成债主）。\n"
    '只输出一行 JSON：{"成立": true, "不成立项": []}；不成立时把不成立的事实原文放进「不成立项」。'
)


def check_labels(llm, *, labels: list[str], text: str) -> dict:
    """一次调用校验「一组标签 ↔ 一段文本」。返回 `{"checked": bool, "violations": [...]}`。

    `checked=False` = **未判定**（空响应 / JSON 解析失败 / 截断）——按"空 = 未知 ≠ 通过"，
    调用方**不得**当成通过；`label_check()` 会把它单独计数上报。
    """
    if not labels:
        return {"checked": True, "violations": []}
    user = ("【事实标签】\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(labels))
            + f"\n\n【文本】\n{text}")
    try:
        d = _extract_json(_complete(llm, LABEL_CHECK_SYSTEM, user, purpose="label_check"))
    except (ValueError, KeyError, TypeError):
        return {"checked": False, "violations": []}
    bad = [str(x) for x in (d.get("不成立项") or [])]
    if not bool(d.get("成立", True)) and not bad:
        bad = ["（标签校验员判「不成立」但未列出条目）"]
    return {"checked": True, "violations": bad}


LABEL_CHECK_RATE = 0.20
LABEL_CHECK_SEED = 20260913


def _checkable_labels(sample: dict) -> list[str]:
    """哪些模块有可校验的标签（其余模块**跳过**，跳过 ≠ 通过）：

    · extract：`labels`（该回合应当提炼出的事实）必须在 input 叙事里成立；
    · compress：`preserve_points`（必保全要点）必须在 input 的历史里成立；
    · judge：其标签是"问题类型"，依据由 `verbalize` 的 anchors 反向校验**程序**保证 → 不重复校验。
    """
    if sample.get("module") == "extract":
        return [f"{x['type']}：{x['text']}" for x in sample.get("labels", [])]
    if sample.get("module") == "compress":
        return [p["text"] for p in sample.get("preserve_points", [])]
    return []


def label_check(llm, samples: list[dict], *, rate: float = LABEL_CHECK_RATE,
                seed: int = LABEL_CHECK_SEED) -> dict:
    """抽检「标签 ↔ 文本」语义一致性 → `{"checked", "skipped", "violations", "unknown"}`。

    抽样口径与 `quality_sample` 一致（同一 seed 约定、升序消费 → 结果与顺序无关）。
    """
    checkable = [s for s in samples if _checkable_labels(s)]
    out = {"checked": 0, "skipped": len(samples) - len(checkable),
           "violations": [], "unknown": [], "prompt_version": label_check_version()}
    if not checkable:
        return out
    rng = random.Random(seed)
    k = min(max(1, math.ceil(len(checkable) * rate)), len(checkable))
    for i in sorted(rng.sample(range(len(checkable)), k=k)):
        s = checkable[i]
        r = check_labels(llm, labels=_checkable_labels(s), text=s.get("input", ""))
        if not r["checked"]:
            out["unknown"].append(s["id"])
            continue
        out["checked"] += 1
        if r["violations"]:
            out["violations"].append({"id": s["id"], "module": s["module"],
                                      "items": r["violations"]})
    return out


# ---------------------------------------------------------------------------
# 轨道 2 批跑（spec §7.2/§7.3：掺探针 → 打分 → 检出率门）
# ---------------------------------------------------------------------------

PROBE_MIN_RATE = 0.10   # 探针掺入 ≥10%
DETECT_MIN = 0.90       # 检出率 <90% → 当批成绩全部作废
PROBE_SEED = 20260912


def prompt_version() -> str:
    """rubric 提示词指纹（spec §6.4/§9.2：报告必须自证口径）。评委提示词一改即换新值。

    **不含** `LABEL_CHECK_SYSTEM`（标签自检是另一条侧信道，指纹见 `label_check_version()`）——
    否则新增校验器会把已记录的 X 校准指纹（`37330a304299a075`）牵连作废。
    """
    return hashlib.sha256(
        (RUBRIC_SYSTEM + PAIRWISE_SYSTEM + NATURALNESS_SYSTEM).encode("utf-8")
    ).hexdigest()[:16]


def label_check_version() -> str:
    """标签自检提示词的指纹（与 `prompt_version` 分开记，互不牵连）。"""
    return hashlib.sha256(LABEL_CHECK_SYSTEM.encode("utf-8")).hexdigest()[:16]


def budget_policy() -> str:
    """预算策略指纹（budgets.py 的 sha 前 12 位）——改常量即整套重测（spec §9.2 继承项）。"""
    import game_agent.budgets as _b
    return "budgets.py@" + hashlib.sha256(
        pathlib.Path(_b.__file__).read_bytes()).hexdigest()[:12]


def probe_positions(n: int, rate: float, seed: int = PROBE_SEED) -> list[int]:
    """探针抽样位置（**与 run_eval 同源**：测试据此构造夹具、报告据此留痕）。"""
    if n <= 0:
        return []
    k = min(max(1, math.ceil(n * rate)), n)
    return sorted(random.Random(seed).sample(range(n), k=k))


def _restore_points(sample: dict) -> list[PreservePoint]:
    return [PreservePoint(text=p["text"], anchors=p["anchors"])
            for p in sample.get("preserve_points", [])]


def run_eval(llm, samples: list[dict], *, probe_rate: float = PROBE_MIN_RATE,
             seed: int = PROBE_SEED, meta: dict | None = None) -> dict:
    """对 compress 样本批跑打分轨。探针替换法：被抽中的样本以其改坏版送入评委，
    成绩只计入探针检出统计，不混入干净样本的分数分布（防污染报告口径）。

    `meta`：调用方注入溯源指纹（prompt_version / budget_policy / endpoint / judge_model），
    满足 spec §6.4 的报告 schema —— **缺指纹的报告不予采信**。
    """
    probe_idx = set(probe_positions(len(samples), probe_rate, seed))
    kind_rng = random.Random(seed + 1)   # 与位置抽样**解耦**：改其一不影响另一
    kinds = _probe_kinds(len(probe_idx), rng=kind_rng)
    rows, probes, invalid = [], [], []
    slot = 0
    for i, s in enumerate(samples):
        pps = _restore_points(s)
        if i in probe_idx:
            kind = kinds[slot]
            slot += 1
            try:
                bad = make_probe(s["output"], pps, kind=kind, rng=kind_rng)
            except ValueError as e:      # 探针没改成 = **无效**，不是"判官漏检"（发现②）
                invalid.append({"id": s["id"], "kind": kind, "reason": str(e)})
                continue
            sc = score(llm, summary=bad, material=s["input"], preserve_points=pps)
            probes.append({"id": s["id"], "kind": kind, "grade": _PROBE_GRADE[kind],
                           "detected": probe_detected(sc, kind)})
        else:
            sc = score(llm, summary=s["output"], material=s["input"],
                       preserve_points=pps)
            rows.append({"id": s["id"], **sc})
    # 门只压**显性级**（覆盖级的道理见 `_PROBE_GRADE` 注释）
    explicit = [p for p in probes if p["grade"] == "explicit"]
    coverage = [p for p in probes if p["grade"] == "coverage"]
    if explicit:
        det = sum(p["detected"] for p in explicit) / len(explicit)
        valid = det >= DETECT_MIN
        note = None
    else:  # 一个显性探针都没有 → 检出率**未知**，不能当通过（"空 = 未知 ≠ 通过"）
        det, valid = None, False
        note = "无显性探针（本次只抽到覆盖级/全部构造失败）→ 检出率未知，批无效"
    return {"scores": rows, "probes": probes, "probes_invalid": invalid,
            "probe_detection": det,
            "probe_coverage_detection": (sum(p["detected"] for p in coverage) / len(coverage)
                                        if coverage else None),
            "probe_grades": {"explicit": len(explicit), "coverage": len(coverage)},
            "probe_indices": sorted(probe_idx),
            "dim_stats": _dim_stats(rows), "validity_note": note,
            "batch_valid": valid, **(meta or {})}


def _dim_stats(rows: list[dict]) -> dict:
    """四维分档计数 + **饱和标记**（发现③）：三维全满分 = 这批材料上评委没有区分度。

    根因不是评委坏了，而是出库样本本为"程序先杀幸存者"（超长/虚构/缺要点已在上游被杀）——
    幸存者天然干净。把饱和**报出来**（而不是让报告只显示一串 2），下游才知道
    成对比较/选优在这批上会退化成随机。
    """
    out: dict[str, dict] = {}
    for d in DIMS:
        counts = {str(v): sum(1 for r in rows if r.get(d) == v) for v in (0, 1, 2)}
        n = len(rows)
        out[d] = {**counts, "mean": round(sum(r.get(d, 0) for r in rows) / n, 2) if n else None,
                  "saturated": bool(n) and counts["2"] == n}
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="rubric 轨道 2 批跑（spec §7）")
    p.add_argument("--samples", required=True, help="compress.jsonl（出库产物）")
    p.add_argument("--probe-rate", type=float, default=PROBE_MIN_RATE)
    p.add_argument("--report", default=None, help="报告输出路径（json）")
    args = p.parse_args(argv)
    from game_agent.config import load_settings
    from game_agent.endpoint import fingerprint_for
    from game_agent.llm import LLMClient
    from game_agent.usage import UsageTracker

    samples = [json.loads(line) for line in
               pathlib.Path(args.samples).read_text(encoding="utf-8").splitlines() if line]
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（.env）")
        return 1
    tracker = UsageTracker("reports/usage-rubric.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    rep = run_eval(llm, samples, probe_rate=args.probe_rate, meta={
        "prompt_version": prompt_version(),
        "budget_policy": budget_policy(),
        "judge_model": llm.model_for("aux"),
        "endpoint": fingerprint_for(settings, "aux"),
        "sample_set": str(args.samples),
    })
    out = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.report:
        pathlib.Path(args.report).write_text(out, encoding="utf-8")
    det = rep["probe_detection"]
    det_txt = f"{det:.0%}" if det is not None else "未知（无显性探针）"
    cov = rep.get("probe_coverage_detection")
    cov_txt = f"{cov:.0%}" if cov is not None else "—"
    print(f"探针检出率 {det_txt}（门 {DETECT_MIN:.0%}，只压**显性级**"
          f"{rep['probe_grades']}）· 覆盖级诊断 {cov_txt}（rubric 不管覆盖，见 _PROBE_GRADE）"
          f"；有效样本 {len(rep['scores'])} 条 · prompt_version={rep['prompt_version']}")
    if rep.get("probes_invalid"):
        print(f"[探针无效] {len(rep['probes_invalid'])} 个构造失败（**不计入漏检**）："
              f"{[p['id'] for p in rep['probes_invalid']]}")
    saturated = [d for d, s in rep["dim_stats"].items() if s["saturated"]]
    if saturated:  # 发现③：饱和必须报出来，否则报告只显示一串 2、看不出评委没有区分度
        print(f"[维度饱和] {'/'.join(saturated)} 全部满分（{len(rep['scores'])} 条）——"
              "这批材料上该维**无区分度**（出库样本是程序先杀的幸存者），成对比较/选优会退化")
    if not rep["batch_valid"]:
        print(f"[✗] 批无效：{rep.get('validity_note') or '探针检出率不达标'}——"
              "当批成绩全部作废（报告已带 batch_valid=false 留痕），修评委提示词后整批重评")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

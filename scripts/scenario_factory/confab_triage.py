"""confab 人读清单的 LLM 预分诊（决策 16 的第一道筛）。

**要人判的那一条**是"断言的虚构事实**不得由材料已有事实组合推出**" ——
即：叙事把某件事当作既成事实说出来，而材料里**没有任何依据**（含别称、同义、
或可由材料已有事实直接推出的信息）。

**为什么不能直接丢给 LLM 了事**：这是**标签审计**（审计员查别人的活），不是生成任务 ——
LLM 在这种位置上天然顺从"你已经标成虚构事实了，那就是虚构事实"。故分两段：

**第一段：程序判死（零成本，code owns truth）**

- `锚点在材料里` —— confab 直接不成立（校验③ 漏网）⇒ 硬缺陷
- `锚点不在叙事里` —— "缺席证据"通路根本不存在 ⇒ 硬缺陷
- `硬元叙述词`（`verbalize.META_HARD`，发现⑩ 那一类：在念卡面 JSON）⇒ 硬缺陷
- `锚点附近有疑问/听说/否定标记` —— 可能是**问**而不是**断言** ⇒ 可疑

**第二段：LLM 只判语义**（中性提问 + **反顺从装置**）

问法刻意**不说**"这是编造的"，只问两件事：叙事是否把 X 当作既成事实说出？
材料里有没有依据？并在回答"有依据"时**要求逐字引用材料原文** ——
引不出出处就不算有依据（给不出的"是"是最廉价的一种"是"）。

**结论映射**：`材料有依据=是` 或 `断言=否` ⇒ 可疑（进人读）；两者都干净 ⇒ 低风险（可跳过）。

产出：
- `reports/confab-triage-<date>.md` —— 人读清单（硬缺陷 / 可疑在前，附理由与依据）
- `data/route-a/<layer>/confab-triage-<layer>.jsonl` —— 机器可读判定，**append-only、可续跑**
  （已判过的 id 跳过；`--dry-run` 只跑第一段，零成本看清楚要花多少钱）

用法::

    python -m scripts.scenario_factory.confab_triage --layer train --dry-run   # 零成本预演
    python -m scripts.scenario_factory.confab_triage --layer train --limit 30  # 首批看质量
    python -m scripts.scenario_factory.confab_triage --layer all               # 全跑（可续）
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
from collections import Counter

from game_agent.budgets import MIN_CALL_TOKENS

from . import worklog
from .cards import REPO_ROOT
from .verbalize import META_HARD

DATA_ROOT = REPO_ROOT / "data" / "route-a"
TRIAGE_LOG = "confab-triage"          # 日志名（`.work/confab-triage.jsonl`）
ANCHOR_RE = re.compile(r"「(.+?)」")
# "这不是断言"的**强**句式：必须**锚在锚点本身上**。
#
# 第一版是"锚点 ±24 字窗口里出现 不|没|？|吗|到底"——**实测 90% 命中、全是假阳性**：
# 中文叙事里 `没声响`/`连停顿都没有`/`用不着`/`都不算小` 遍地都是，而 `？` 往往是
# **别人在问这个锚点**（如"你认识那个什么'寒鸦'吗？"—— 预设它存在，未必不是断言）。
# 教训同发现⑥：把"必要条件筛查"当"产线过滤器"用，只会得到一堵噪声墙。
# 收紧后只认**紧贴锚点**的疑问/否定/听说框架。引号要**全收**：真实数据里
# `‘寒鸦’`（弯单引号）与 `「白鸮」` 都出现，只认直角引号会漏掉一半（守卫实测抓到的）。
_Q = "[「『\"'‘’“”]?"       # 可选开引号
_QE = "[」』\"'‘’“”]?"      # 可选闭引号
# ⚠️ 锚点占位用 `__ANCHOR__` 替换，**不用 `str.format`** —— 模板里的 `{0,4}` 量词会被
# format 当成字段名（实测 KeyError: '0,4'）。正则里全是花括号，别让格式化碰它。
_A = "__ANCHOR__"
_HEDGE_PATTERNS = (
    ("什么" + _Q + _A + "[^。！？\n]{0,4}[吗嘛？?]", "以『什么 X…吗』的方式提及（可能是问，不是断言）"),
    # `？` 才是**回声疑问**（"「三万元？」你不…"= 当场质疑）；`吗/嘛` 多数是**语气词**
    # （"「银铃」嘛，我上回就瞧见了"= 断言）—— 实测 86 条命中里 33 条是语气词，纯假阳性。
    (_Q + _A + _QE + r"\s*[？?]", "锚点后紧跟疑问语气（回声疑问，可能是当场质疑）"),
    ("是不是" + _Q + _A, "『是不是 X』—— 疑问"),
    (r"(听说|据说|难道)[^。！？\n]{0,6}" + _Q + _A, "『听说/据说/难道 X』—— 转述或反问"),
    (r"不(是|叫|认识|知道|记得)[^。！？\n]{0,4}" + _Q + _A, "『不是/不认识 X』—— 否定"),
    (r"没(有|听说过|见过|这)[^。！？\n]{0,4}" + _Q + _A, "『没有/没听说 X』—— 否定"),
    (r"从未[^。！？\n]{0,4}" + _Q + _A, "『从未 X』—— 否定"),
)

CONFAB_TRIAGE_SYSTEM = (
    "你是数据审计员，任务是核对一段叙事与它的材料是否自洽。\n"
    "给定【材料】（当前游戏状态与已确立的事实）与【叙事】（一段回合文本），"
    "以及从叙事中摘出的一个说法 X，请回答三个问题：\n"
    "① 材料里**有没有** X 的依据？依据包括：材料直接写了、写了别称或同义说法、"
    "或可由材料已有事实直接推出。\n"
    "② 叙事是否把 X **当作既成事实**说出来？（询问、听说、转述他人、否定、假设，都不算断言）\n"
    "③ 依据是什么？\n"
    '只输出一行 JSON：{"依据": "必填、不得为空：你在材料里找到/没找到什么", '
    '"断言": "是|否", "材料有依据": "是|否", '
    '"材料出处": "若材料有依据，逐字引用材料原文片段（否则留空)"}\n'
    "注意：`依据` 必须写实际内容；空字符串视为无效输出。\n"
    # ↓ 实测陷阱（2026-09-18）：19/20 条"材料有依据"全是**数额巧合** —— 锚点是"五十两"，
    #   而材料状态栏写着「银两 50」（**玩家当前持有**）。模型把"他手里有 50 两"读成了
    #   "他欠 50 两"的依据。数额相同**不构成债务/交易的依据**，必须直说。
    "特别注意：材料『玩家属性』里的数值（银两/信用点/灵石）是玩家**当前持有**，"
    "**不等于**叙事里说的欠款、交易或承诺 —— **数额相同不构成依据**。"
    "判断债务/承诺类说法时，要看材料里有没有那笔**债务/约定本身**（债权人、事由、期限），"
    "而不是看数额对不对得上。\n"
)


# ---------------------------------------------------------------------------
# 第一段：程序判死
# ---------------------------------------------------------------------------

def anchor_of(detail: str) -> str | None:
    """从 `detail`（"把「白鸮」这件事当作既成事实断言——材料里从未有过"）取断言锚点。"""
    m = ANCHOR_RE.search(detail or "")
    return m.group(1) if m else None


def hedge_kind(narration: str, anchor: str, window: int = 24) -> str | None:
    """锚点是否被**疑问/转述/否定框架**包着（返回框架说明；None = 没命中）。

    只在锚点出现处的**紧邻窗口**里匹配（±`window` 字），且模式**锚在锚点本身上** ——
    这样 `都没声响` 这类与锚点无关的否定词不会误触发（第一版的教训，见文件头常量注释）。
    """
    pats = [(re.compile(p.replace(_A, re.escape(anchor))), why) for p, why in _HEDGE_PATTERNS]
    for m in re.finditer(re.escape(anchor), narration):
        lo, hi = max(0, m.start() - window), min(len(narration), m.end() + window)
        seg = narration[lo:hi]
        for pat, why in pats:
            if pat.search(seg):
                return why
    return None


def _hedged(narration: str, anchor: str) -> bool:
    return hedge_kind(narration, anchor) is not None


def hard_checks(row: dict, anchor: str | None) -> list[str]:
    """程序能判死的（返回非空即**硬缺陷**，不必花钱问 LLM）。"""
    out = []
    if not anchor:
        return ["detail 里取不出断言锚点（人读确认标签写的是什么）"]
    if anchor in row["material"]:
        out.append(f"断言锚点「{anchor}」**出现在材料里** —— confab 不成立（校验③ 漏网）")
    if anchor not in row["narration"]:
        out.append(f"断言锚点「{anchor}」**不在叙事里** —— 缺席证据通路不存在")
    meta = [t for t in META_HARD if t in row["narration"]]
    if meta:
        out.append("硬元叙述词命中：" + "/".join(meta))
    return out


def suspect_signals(row: dict, anchor: str | None) -> list[str]:
    out = []
    if anchor and anchor in row["narration"]:
        why = hedge_kind(row["narration"], anchor)
        if why:
            out.append(f"锚点「{anchor}」：{why}")
    if row.get("meta_soft"):
        out.append("温和元词：" + "/".join(row["meta_soft"]))
    if row.get("name_confusables"):
        out.append("近误人名：" + "/".join(row["name_confusables"]))
    return out


# ---------------------------------------------------------------------------
# 第二段：LLM 判定
# ---------------------------------------------------------------------------

def llm_verdict(llm, row: dict, anchor: str) -> dict:
    """中性提问 + 要求引用出处。返回 `{"断言","材料有依据","依据","材料出处"}`。"""
    from scripts.rubric_judge import _complete, _extract_json

    user = (f"【材料】\n{row['material']}\n\n【叙事】\n{row['narration']}\n\n"
            f"【说法 X】{anchor}")
    try:
        d = _extract_json(_complete(llm, CONFAB_TRIAGE_SYSTEM, user, purpose="confab_triage"))
    except (ValueError, KeyError, TypeError):
        return {"断言": "未判定", "材料有依据": "未判定", "依据": "输出解析失败",
                "材料出处": ""}
    stated = str(d.get("断言", "")).strip()
    backed = str(d.get("材料有依据", "")).strip()
    quote = str(d.get("材料出处", "")).strip()
    why = str(d.get("依据", "")).strip()
    # **反顺从装置**：声称"有依据"却引不出材料原文 ⇒ 不算有依据（降级为"未判定"）
    if backed == "是" and (not quote or quote not in row["material"]):
        backed = "未判定"
    # `依据` 必填（首版模型稳定返回空串 ⇒ 报告里"模型读作："后面什么都没有）
    if not why:
        why = "（模型未给依据）"
    return {"断言": stated or "未判定", "材料有依据": backed or "未判定",
            "依据": why[:80], "材料出处": quote[:120]}


def classify(hard: list[str], signals: list[str], v: dict | None) -> tuple[str, str]:
    """(档位, 一行理由)。档位：hard / suspect / clean。"""
    if hard:
        return "hard", "；".join(hard)
    reasons = list(signals)
    if v is not None:
        if v["断言"] == "否":
            reasons.append(f"叙事**没把 X 当断言**说（模型读作：{v['依据']}）")
        if v["材料有依据"] == "是":
            reasons.append(f"材料**有依据**：{v['依据']}（出处：{v['材料出处'][:40]}）")
        if v["断言"] == "未判定" or v["材料有依据"] == "未判定":
            reasons.append(f"未判定（{v['依据']}）—— **空 = 未知 ≠ 通过**")
    return ("suspect", "；".join(reasons)) if reasons else ("clean", "标签成立：叙事断言 X，材料无依据")


# ---------------------------------------------------------------------------
# 续跑日志（append-only；键是**样本 id**，不是卡索引 —— 故不复用 worklog.Journal）
# ---------------------------------------------------------------------------

def log_path(layer: str) -> pathlib.Path:
    return DATA_ROOT / layer / worklog.WORK_DIR / f"{TRIAGE_LOG}.jsonl"


def pins_path(layer: str) -> pathlib.Path:
    """人读指定放在**层目录**（不是 `.work/`）。

    `.work/` 是**机器草稿**（产线日志，`.gitignore` 忽略之）；pins 是**人的判断**，
    必须进版本库 —— 放在 `.work/` 里它会随一次 `git clean` 或换个克隆就消失，
    而它存在的全部理由就是"人的判断要留得住"（与 `confab-manual-review-*.jsonl` 同级）。
    """
    return DATA_ROOT / layer / f"{TRIAGE_LOG}-pins.txt"


def read_pins(layer: str) -> dict[str, str]:
    """**人读指定**：`id<TAB>说明` 若干行（`#` 开头为注释）。

    为什么需要它（2026-09-18 实测）：sc-32676 的判定在两次运行里从"有依据"翻成"无依据"
    —— 模型在**边界个案**上不稳定，而"模型说不清"恰恰是最该人读的那一类。
    没有 pins，下一次重生成就会把它悄悄算成"低风险"，人读意见被抹掉。
    决策 16 本来就是"只有人能判"的通道，仪器必须留得住人的判断。
    """
    path = pins_path(layer)
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sid, _, note = line.partition("\t")
        if sid.strip():
            out[sid.strip()] = note.strip() or "人读指定"
    return out


def read_verdicts(layer: str) -> dict[str, dict]:
    """已判定的行（后写覆盖先写）。坏行跳过（"空 = 未知 ≠ 通过"：宁可重判一次）。"""
    path = log_path(layer)
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "id" in rec:
            out[rec["id"]] = rec
    return out


def append_verdict(layer: str, entry: dict) -> None:
    worklog.append_entry(log_path(layer), {"triage": 1}, entry)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def confab_rows(layer: str) -> list[dict]:
    """出库的 confab 行（`judge.jsonl` 是**真源**：人读清单由它派生，且只有它带 `detail`）。"""
    f = DATA_ROOT / layer / "judge.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()
            if x.strip() and json.loads(x).get("category") == "confab"]


def review_ids(layer: str) -> set[str]:
    """人读清单里的 id（用来核对"清单与出库一致"，两边不一致就是清单过期）。"""
    f = DATA_ROOT / layer / f"confab-manual-review-{layer}.jsonl"
    if not f.exists():
        return set()
    return {json.loads(x)["id"] for x in f.read_text(encoding="utf-8").splitlines() if x.strip()}


def triage_layer(layer: str, llm, *, limit: int | None = None, dry_run: bool = False,
                 redo: set[str] | None = None, echo=print) -> dict:
    rows = confab_rows(layer)
    if not rows:
        echo(f"[{layer}] 无 confab 行")
        return {"layer": layer, "rows": 0}
    mismatch = review_ids(layer) ^ {r["id"] for r in rows}
    if mismatch:
        echo(f"[{layer}] ⚠ 人读清单与出库 confab 行**不一致**（{len(mismatch)} 个 id 只在一侧）"
             f"—— 清单可能过期，先重生成清单再分诊")
    done = read_verdicts(layer)
    redo = redo or set()
    todo = [r for r in rows if r["id"] not in done or r["id"] in redo]
    if limit:
        todo = todo[:limit]

    staged: list[dict] = []
    n_llm = 0
    for r in todo:
        anchor = anchor_of(r.get("detail", ""))
        hard = hard_checks(r, anchor)
        signals = suspect_signals(r, anchor)
        v = None
        if not hard and not dry_run:
            v = llm_verdict(llm, r, anchor)
            n_llm += 1
        level, why = classify(hard, signals, v)
        old = done.get(r["id"])
        entry = {"id": r["id"], "layer": layer, "genre": r.get("genre"), "anchor": anchor,
                 "level": level, "why": why, "verdict": v,
                 # 重判翻转要留痕：**模型在边界个案上不稳定**（实测 sc-32676 在"外号推理"
                 # 这条上因一次无关的提示词改动就从"有依据"翻成"无依据"）。
                 # 一次不稳定的判定**不算干净的判定** ⇒ 报告里单列，交人读。
                 "prev_level": (old or {}).get("level"), "flipped": bool(old and old["level"] != level)}
        staged.append(entry)
        if not dry_run:
            append_verdict(layer, entry)

    counts = Counter(e["level"] for e in staged)
    echo(f"[{layer}] confab {len(rows)} 条（已判 {len(done)}）→ 本次处理 {len(staged)}："
         f"硬缺陷 {counts['hard']} / 可疑 {counts['suspect']} / 低风险 {counts['clean']}"
         + ("（--dry-run 未调用 LLM）" if dry_run else f"（LLM 调用 {n_llm} 次）"))
    return {"layer": layer, "rows": len(rows), "staged": staged, "counts": counts,
            "calls": n_llm, "dry_run": dry_run}


def refresh_layer(layer: str, *, echo=print) -> dict:
    """**只重跑第一段**：用日志里已有的 LLM 判定重算硬判据与句式信号（**零成本**）。

    为什么需要：粗筛口径是会改的（实测两轮：先 90% 假阳性 → 收紧；再发现 `吗/嘛`
    是语气词而非疑问 → 再收紧）。若每次都得重跑 1388 次 LLM 判定（≈¥2.7 / 25 分钟），
    调口径的代价就高得没人愿意调了 —— 而"口径不对"正是这套仪器的头号风险。
    """
    done = read_verdicts(layer)
    out = []
    for sid, old in done.items():
        anchor = old.get("anchor")
        # 行内容从出库文件取（日志只存了 id 与判定，没有材料/叙事）
        row = _row_by_id(layer, sid)
        if row is None:          # 出库文件里已无此 id（例如层被重产）→ 保留旧判定不动
            out.append(old)
            continue
        hard = hard_checks(row, anchor)
        signals = suspect_signals(row, anchor)
        level, why = classify(hard, signals, old.get("verdict"))
        out.append({**old, "level": level, "why": why,
                    "prev_level": old.get("level"), "flipped": old.get("level") != level})
    for e in out:
        append_verdict(layer, e)
    flipped = sum(1 for e in out if e.get("flipped"))
    n = Counter(e["level"] for e in out)
    echo(f"[{layer}] 重算第一段 {len(out)} 条（零 LLM）：硬缺陷 {n['hard']} / "
         f"可疑 {n['suspect']} / 低风险 {n['clean']}；档位变化 {flipped}")
    return {"layer": layer, "rows": len(out), "counts": n, "flipped": flipped}


def _row_by_id(layer: str, sid: str) -> dict | None:
    for r in confab_rows(layer):
        if r["id"] == sid:
            return r
    return None


def all_verdicts(layers: list[str]) -> list[dict]:
    """所有判定，**并叠加人读指定（pins）**。

    在**读侧**叠加而不是写侧：① 人读意见立刻生效，不必为改一行注释重跑 LLM；
    ② pins 是人的判断，机器日志是机器的判断，两者各有真源、不互相覆盖。
    """
    out = []
    for layer in layers:
        pins = read_pins(layer)
        for e in read_verdicts(layer).values():
            note = pins.get(e["id"])
            if note:
                e = {**e, "level": "suspect", "pinned": True,
                     "why": f"**人读指定**：{note}（机器判：{e['level']}；{e['why']}）"}
            out.append(e)
    return out


def render_md(layers: list[str], results: list[dict]) -> str:
    today = datetime.date.today().isoformat()
    rows = all_verdicts(layers)
    by = Counter((r["layer"], r["level"]) for r in rows)
    md = [f"# confab 人读清单 · LLM 预分诊（{today}）\n",
          "> 决策 16 要人判的那条：「断言的虚构事实不得由材料已有事实组合推出」。",
          "> 本表是**第一道筛**：程序先判死（锚点在材料里 / 不在叙事里 / 硬元词），",
          "> 剩下的才问 LLM（中性提问 + 要求引用材料出处）。**分诊不替代人读**，",
          "> 它只是把 1388 行压到人真读得完的量。\n",
          "## 1. 总览\n",
          "| 层 | confab 条数 | 硬缺陷 | 可疑（需人读） | 低风险（可跳过） |",
          "| --- | --- | --- | --- | --- |"]
    for layer in layers:
        n = dict(results_n(results, layer))
        tot = sum(n.values())
        md.append(f"| {layer} | {tot} | {n.get('hard', 0)} | **{n.get('suspect', 0)}** | "
                  f"{n.get('clean', 0)} |")
    md.append("")
    n_pin = sum(1 for r in rows if r.get("pinned"))
    md.append(f"**人读指定 {n_pin} 条**（`confab-triage-pins.txt`：机器判『干净』也照样列出 ——"
              f"模型在边界个案上不稳定，人的判断压过机器）。\n")
    for level, title in (("hard", "2. 硬缺陷（程序判死，标签不成立）"),
                         ("suspect", "3. 可疑（**请人读这些**）")):
        sel = [r for r in rows if r["level"] == level]
        md.append(f"## {title} —— {len(sel)} 条\n")
        if not sel:
            md.append("（无）\n")
            continue
        md.append("| id | 层 | 题材 | 断言锚点 | 理由 |")
        md.append("| --- | --- | --- | --- | --- |")
        for r in sorted(sel, key=lambda x: (x["layer"], x["id"])):
            md.append(f"| `{r['id']}` | {r['layer']} | {r.get('genre', '?')} | "
                      f"{r.get('anchor') or '（取不出）'} | {r['why'][:200]} |")
        md.append("")
    flipped = [r for r in rows if r.get("flipped")]
    md.append(f"## 5. 判定翻转（重判后档位变了）—— {len(flipped)} 条\n")
    md.append("> 提示词改动后重判导致档位变化 = **模型在这几条上不稳定**。"
              "一次不稳定的判定不算干净的判定 ⇒ 建议人读。\n")
    if not flipped:
        md.append("（无）\n")
    else:
        md.append("| id | 层 | 断言锚点 | 变化 | 现行理由 |")
        md.append("| --- | --- | --- | --- | --- |")
        for r in sorted(flipped, key=lambda x: (x["layer"], x["id"])):
            md.append(f"| `{r['id']}` | {r['layer']} | {r.get('anchor') or '?'} | "
                      f"{r.get('prev_level')} → **{r['level']}** | {r['why'][:150]} |")
        md.append("")

    md.append("## 6. 低风险（可跳过，供抽查）\n")
    clean = [r for r in rows if r["level"] == "clean"]
    md.append(f"共 {len(clean)} 条，随机抽 10 条列在这里；全量在 "
              f"`data/route-a/<layer>/confab-triage-<layer>.jsonl`。\n")
    md.append("| id | 层 | 断言锚点 | LLM 依据 |")
    md.append("| --- | --- | --- | --- |")
    for r in clean[:: max(1, len(clean) // 10)][:10]:
        v = r.get("verdict") or {}
        md.append(f"| `{r['id']}` | {r['layer']} | {r.get('anchor') or '?'} | "
                  f"{str(v.get('依据', ''))[:80]} |")
    md.append("\n---\n")
    md.append("**怎么读这张表**：可疑档不是「标签错了」，而是「**只有人能判**的那一类」 ——"
              "模型可能只是把『问了一句』读成『断言』，也可能材料里真有依据（同义/可推出）。"
              "硬缺陷档才是程序能定死的错。\n")
    return "\n".join(md) + "\n"


def results_n(results: list[dict], layer: str) -> list[tuple[str, int]]:
    """该层的档位计数 —— **以日志为准并叠加 pins**（不是本次跑批的增量）。

    分批复核是常态（先 30 条看质量、再续跑），若按"本次处理"计数，报告的总览表会把
    第一批漏掉（457 条只报 427 条）—— 而报告是给人读的最终交付物，必须报总数。
    """
    return list(Counter(e["level"] for e in all_verdicts([layer])).items())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="confab 人读清单的 LLM 预分诊（决策 16 第一道筛）")
    p.add_argument("--layer", default="all", choices=["train", "dev", "eval", "all"])
    p.add_argument("--limit", type=int, default=None, help="每层最多处理多少条（首批试跑）")
    p.add_argument("--dry-run", action="store_true", help="只跑程序判死那一段（零成本）")
    p.add_argument("--redo", default="", help="重判这些 id（逗号分隔）——提示词改过之后补判用")
    p.add_argument("--refresh", action="store_true",
                   help="只重算第一段（用日志里已有的 LLM 判定）——零成本调粗筛口径")
    p.add_argument("--out", default=None, help="报告路径（缺省 reports/confab-triage-<date>.md）")
    args = p.parse_args(argv)

    layers = ["train", "dev", "eval"] if args.layer == "all" else [args.layer]
    if args.refresh:
        results = [refresh_layer(layer) for layer in layers]
        dest = pathlib.Path(args.out) if args.out else (
            REPO_ROOT / "reports" / f"confab-triage-{datetime.date.today():%Y%m%d}.md")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_md(layers, results), encoding="utf-8")
        print(f"[✓] {dest}（零 LLM 调用）")
        return 0
    llm = None
    if not args.dry_run:
        from .assemble import _make_llm
        llm = _make_llm()
        if llm is None:
            print("[✗] 未配置 API key ⇒ 只能 --dry-run")
            return 1
    results = [triage_layer(layer, llm, limit=args.limit, dry_run=args.dry_run,
                            redo={x.strip() for x in args.redo.split(",") if x.strip()})
               for layer in layers]
    dest = pathlib.Path(args.out) if args.out else (
        REPO_ROOT / "reports" / f"confab-triage-{datetime.date.today():%Y%m%d}.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_md(layers, results), encoding="utf-8")
    print(f"[✓] {dest}")
    print(f"    预算参考：单次判定 ≈ {MIN_CALL_TOKENS} token 出参上限；"
          f"全量 {sum(r['rows'] for r in results)} 条 ≈ "
          f"{sum(r['rows'] for r in results) * 0.002:.1f} 元（按 judge 侧信道实测单价估）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

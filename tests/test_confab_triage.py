"""confab 预分诊的守卫（决策 16 第一道筛）。

三件事必须钉住：
1. **程序判死的三条**真的会判死（锚点在材料里 / 不在叙事里 / 硬元词）；
2. **粗筛不是噪声墙** —— 第一版"锚点 ±24 字内有 不|没|？|吗"实测 90% 命中且全是假阳性，
   夹具直接用**真实叙事片段**（`都没声响` 不该报、`什么'寒鸦'吗` 该报）；
3. **反顺从装置**：模型声称"材料有依据"但引不出材料原文 ⇒ 降级为未判定（空 = 未知 ≠ 通过）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.scenario_factory import confab_triage as ct


class _Stub:
    """最小 LLM 替身：按 `purpose` 回固定 JSON，并数调用次数。"""

    def __init__(self, reply: str = '{"断言": "是", "材料有依据": "否", "依据": "无", "材料出处": ""}'):
        self.reply = reply
        self.calls = 0

    def complete_with_meta(self, messages, **kw):
        self.calls += 1
        return SimpleNamespace(text=self.reply, finish_reason="stop")


def _row(i=0, *, material="关键事实：\n- 玩家欠沈砚五十两\n",
         narration="“白鸮。”他念出这两个字，语气跟念体温没什么两样。",
         detail="把「白鸮」这件事当作既成事实断言——材料里从未有过", **over):
    row = {"id": f"sc-1{i:04d}", "module": "judge", "category": "confab", "genre": "仙侠",
           "material": material, "narration": narration, "detail": detail,
           "expect": "问题类型：虚构事实", "meta_soft": [], "name_confusables": []}
    row.update(over)
    return row


# --- 1. 程序判死 -----------------------------------------------------------

def test_anchor_extraction_from_detail():
    assert ct.anchor_of("把「白鸮」这件事当作既成事实断言——材料里从未有过") == "白鸮"
    assert ct.anchor_of("（没有引号）") is None
    assert ct.anchor_of("") is None


def test_hard_check_anchor_leaking_into_material():
    """confab 的定义就是"材料里没有"；锚点出现在材料里 ⇒ 标签不成立（校验③ 漏网）。"""
    row = _row(material="关键事实：\n- 玩家的代号是「白鸮」\n")
    hard = ct.hard_checks(row, "白鸮")
    assert hard and "出现在材料里" in hard[0], hard


def test_hard_check_anchor_missing_from_narration():
    """confab 的"缺席证据"通路要求叙事**说出**被断言的东西；没说 ⇒ 通路不存在。"""
    row = _row(narration="他低头在记录本上写了两行，笔尖压得很实。")
    hard = ct.hard_checks(row, "白鸮")
    assert hard and "不在叙事里" in hard[0], hard


def test_hard_check_meta_narration():
    """发现⑩ 那一类：叙事在念卡面（硬元词命中）⇒ 判死。"""
    row = _row(narration="“白鸮。”他说，“category 是 confab。”")
    hard = ct.hard_checks(row, "白鸮")
    assert any("硬元叙述词" in h for h in hard), hard


def test_clean_row_has_no_hard_defect():
    assert ct.hard_checks(_row(), "白鸮") == []


# --- 2. 粗筛不是噪声墙（第一版的教训）--------------------------------------

def test_hedge_ignores_unrelated_negation():
    """**真实片段**：`都没声响` / `连停顿都没有` 与锚点无关 —— 第一版全报（90% 假阳性）。"""
    for nar in ("银铃，两个字吐出来，平平常常，落在地上都没声响。",
                "他说这话的时候连停顿都没有。三万元，月底。",
                "自己的话，语速快得像倒豆子，人家影狸做事有分寸，用不着你天天惦记。"):
        anchor = "银铃" if "银铃" in nar else ("三万元" if "三万元" in nar else "影狸")
        assert ct.hedge_kind(nar, anchor) is None, f"无关否定被误判：{nar}"


def test_hedge_catches_real_question_frames():
    """**真实片段**：锚点被"什么 X…吗 / 不是叫 X 吗"这类框架包着 ⇒ 报（人该看一眼）。"""
    assert ct.hedge_kind("“哎，林医生，你认识那个什么‘寒鸦’吗？”", "寒鸦")
    assert ct.hedge_kind("“你这剑不是叫**银铃**吗？”", "银铃")
    assert ct.hedge_kind("听说「白鸮」近来在城里传得不算小。", "白鸮")
    assert ct.hedge_kind("难道「白鸮」还活着？", "白鸮")


def test_modal_particle_is_not_a_question():
    """**第二轮收紧**（实测 86 条命中里 33 条是假阳性）：`嘛/吗` 多数是**语气词**，不是疑问。

    「『银铃』嘛，我上回就瞧见了」是**断言**（甚至是在强调自己早知道）；
    真回声疑问长这样：「三万元？」你不… —— `？` 才是当场质疑。
    """
    assert ct.hedge_kind("“『银铃』嘛，我上回就瞧见了，那穗子还是我编的。”", "银铃") is None
    assert ct.hedge_kind("寒鸦嘛，黑羽，独来独往，天冷了才回来。", "寒鸦") is None
    assert ct.hedge_kind("“三万元？”对面的人顿了一下。", "三万元") is not None


def test_suspect_signals_report_meta_and_names():
    row = _row(meta_soft=["接口"], name_confusables=["沈青秋"])
    sig = ct.suspect_signals(row, "白鸮")
    assert any("温和元词" in s for s in sig) and any("近误人名" in s for s in sig), sig


# --- 3. 反顺从装置与档位映射 -------------------------------------------------

def test_verdict_requires_quote_for_backed_claim():
    """模型说"材料有依据"却引不出材料原文 ⇒ **降级为未判定**（给不出的"是"最廉价）。"""
    llm = _Stub('{"断言": "是", "材料有依据": "是", "依据": "材料里提过", "材料出处": "编造的出处"}')
    v = ct.llm_verdict(llm, _row(), "白鸮")
    assert v["材料有依据"] == "未判定", v

    llm2 = _Stub('{"断言": "是", "材料有依据": "是", "依据": "材料里提过", "材料出处": "玩家欠沈砚五十两"}')
    v2 = ct.llm_verdict(llm2, _row(), "五十两")
    assert v2["材料有依据"] == "是", v2


def test_verdict_unparseable_is_undetermined_not_clean():
    """解析不出 = 未判定 ⇒ **不算低风险**（"空 = 未知 ≠ 通过"）。"""
    v = ct.llm_verdict(_Stub("这不是 JSON"), _row(), "白鸮")
    assert v["断言"] == "未判定" and v["材料有依据"] == "未判定", v
    level, why = ct.classify([], [], v)
    assert level == "suspect" and "未判定" in why, (level, why)


def test_classify_maps_clean_and_suspect():
    clean = {"断言": "是", "材料有依据": "否", "依据": "材料无此事", "材料出处": ""}
    assert ct.classify([], [], clean)[0] == "clean"
    assert ct.classify([], [], {**clean, "断言": "否"})[0] == "suspect"
    assert ct.classify([], [], {**clean, "材料有依据": "是"})[0] == "suspect"
    assert ct.classify(["锚点在材料里"], [], None)[0] == "hard"


# --- 4. 续跑与不花钱 ---------------------------------------------------------

def test_hard_rows_do_not_call_the_llm(tmp_path, monkeypatch):
    """程序能判死的**不花钱**问 LLM（第一段的意义就在这）。"""
    monkeypatch.setattr(ct, "DATA_ROOT", tmp_path)
    (tmp_path / "train").mkdir(parents=True)
    rows = [_row(0, material="关键事实：\n- 玩家的代号是「白鸮」\n"),   # 硬缺陷
            _row(1)]                                                  # 干净
    (tmp_path / "train" / "judge.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    llm = _Stub()
    res = ct.triage_layer("train", llm, echo=lambda *a: None)
    assert llm.calls == 1, f"只有干净那条该问 LLM，实际 {llm.calls} 次"
    assert res["counts"]["hard"] == 1 and res["counts"]["clean"] == 1


def test_triage_resumes_without_repaying(tmp_path, monkeypatch):
    """续跑：已判过的 id 跳过（append-only 日志，后写覆盖先写）。"""
    monkeypatch.setattr(ct, "DATA_ROOT", tmp_path)
    (tmp_path / "train").mkdir(parents=True)
    (tmp_path / "train" / "judge.jsonl").write_text(
        json.dumps(_row(0), ensure_ascii=False) + "\n", encoding="utf-8")
    llm = _Stub()
    first = ct.triage_layer("train", llm, echo=lambda *a: None)
    assert llm.calls == 1 and first["counts"]["clean"] == 1
    second = ct.triage_layer("train", llm, echo=lambda *a: None)
    assert llm.calls == 1, "续跑不该重复付费"
    assert second["staged"] == [], second

    # 坏行不致命（"空 = 未知 ≠ 通过"：宁可重判一次，也不能让一行断电废掉整份日志）
    log = ct.log_path("train")
    log.write_text(log.read_text(encoding="utf-8") + "半行断电\n", encoding="utf-8")
    assert ct.read_verdicts("train")["sc-10000"]["level"] == "clean"


def test_prompt_warns_about_the_money_coincidence_trap():
    """**实测陷阱**（2026-09-18）：20 条"材料有依据"里 **19 条**是同一个形态 ——
    锚点「五十两」，材料状态栏写着「银两 50」（玩家**当前持有**）。模型把"手里有 50 两"
    读成了"欠 50 两"的依据。这行提示是那个修复的**回归绊线**：
    它证明不了模型会照做，但能挡住"改提示词时把这句删掉"。
    """
    assert "数额相同不构成依据" in ct.CONFAB_TRIAGE_SYSTEM
    assert "当前持有" in ct.CONFAB_TRIAGE_SYSTEM


def test_redo_rejudges_and_overwrites(tmp_path, monkeypatch):
    """`--redo`：提示词改过之后只补判那几条，不重跑全量（append-only，后写覆盖先写）。"""
    monkeypatch.setattr(ct, "DATA_ROOT", tmp_path)
    (tmp_path / "train").mkdir(parents=True)
    (tmp_path / "train" / "judge.jsonl").write_text(
        json.dumps(_row(0), ensure_ascii=False) + "\n", encoding="utf-8")
    llm = _Stub()
    ct.triage_layer("train", llm, echo=lambda *a: None)
    assert ct.read_verdicts("train")[_row(0)["id"]]["level"] == "clean"

    # 改成判"叙事没断言"的桩 → 重判后该条应变可疑（旧判定被覆盖），并**留下翻转痕迹**
    llm2 = _Stub('{"断言": "否", "材料有依据": "否", "依据": "只是问了一句", "材料出处": ""}')
    ct.triage_layer("train", llm2, redo={_row(0)["id"]}, echo=lambda *a: None)
    assert llm2.calls == 1, "只该重判指定的一条"
    after = ct.read_verdicts("train")[_row(0)["id"]]
    assert after["level"] == "suspect"
    assert after["flipped"] is True and after["prev_level"] == "clean", after
    # 翻转要在报告里单列（"一次不稳定的判定不算干净的判定"）
    md = ct.render_md(["train"], [])
    assert "判定翻转" in md and _row(0)["id"] in md


def test_pin_overrides_a_clean_verdict(tmp_path, monkeypatch):
    """**人读指定压过机器**：模型判"干净"也照样列进可疑档。

    实测动因（2026-09-18）：sc-32676 的判定两次运行结论相反（起外号 vs 既成事实），
    模型在边界个案上不稳定 —— 若没有 pins，下次重生成就把它悄悄算成"低风险"。
    """
    monkeypatch.setattr(ct, "DATA_ROOT", tmp_path)
    (tmp_path / "train").mkdir(parents=True)
    (tmp_path / "train" / "judge.jsonl").write_text(
        json.dumps(_row(0), ensure_ascii=False) + "\n", encoding="utf-8")
    ct.triage_layer("train", _Stub(), echo=lambda *a: None)          # 模型判 clean
    assert ct.read_verdicts("train")[_row(0)["id"]]["level"] == "clean"

    ct.pins_path("train").parent.mkdir(parents=True, exist_ok=True)
    ct.pins_path("train").write_text(
        "# 注释行\n\n" f"{_row(0)['id']}\t边界个案：交人读定夺\n", encoding="utf-8")
    rows = ct.all_verdicts(["train"])
    assert rows[0]["level"] == "suspect" and rows[0]["pinned"] is True, rows[0]
    assert "人读指定" in rows[0]["why"] and "边界个案" in rows[0]["why"]
    assert ct.read_pins("train")[_row(0)["id"]] == "边界个案：交人读定夺"
    # 报告的总览与可疑档都要反映它（读侧叠加，不必重跑 LLM）
    md = ct.render_md(["train"], [])
    assert "人读指定 1 条" in md and _row(0)["id"] in md


def test_flip_mark_is_sticky_across_refresh(tmp_path, monkeypatch):
    """翻转痕迹**粘滞**：`--refresh` 跑第二遍不许把第一遍记下的翻转抹掉。

    实测踩到：改完粗筛口径跑了一次 `--refresh`（28 条 suspect→clean），移完 pins 又跑一次
    —— 第二遍把 `flipped` 覆写成 False，于是报告说"翻转 0 条"、计划档说"28 条"，两处对不上。
    审计痕迹必须是单调的：翻过就是翻过（`prev_level` 记**最初**那一档）。
    """
    monkeypatch.setattr(ct, "DATA_ROOT", tmp_path)
    (tmp_path / "train").mkdir(parents=True)
    (tmp_path / "train" / "judge.jsonl").write_text(
        json.dumps(_row(0), ensure_ascii=False) + "\n", encoding="utf-8")
    ct.triage_layer("train", _Stub(), echo=lambda *a: None)                 # → clean
    ct.triage_layer("train",                                              # 重判 → suspect
                    _Stub('{"断言": "否", "材料有依据": "否", "依据": "问句", "材料出处": ""}'),
                    redo={_row(0)["id"]}, echo=lambda *a: None)
    assert ct.read_verdicts("train")[_row(0)["id"]]["flipped"] is True

    ct.refresh_layer("train", echo=lambda *a: None)          # 只重算第一段
    e = ct.read_verdicts("train")[_row(0)["id"]]
    assert e["flipped"] is True, "第二遍 refresh 把翻转痕迹抹掉了"
    assert e["prev_level"] == "clean", f"prev_level 应记最初那一档，实际 {e['prev_level']}"


def test_review_list_mismatch_is_flagged(tmp_path, monkeypatch, capsys):
    """人读清单与出库 confab 行不一致 = 清单过期（决策 37 踩过两次的坑）。"""
    monkeypatch.setattr(ct, "DATA_ROOT", tmp_path)
    (tmp_path / "train").mkdir(parents=True)
    (tmp_path / "train" / "judge.jsonl").write_text(
        json.dumps(_row(0), ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "train" / "confab-manual-review-train.jsonl").write_text(
        json.dumps({"id": "sc-99999-9999"}) + "\n", encoding="utf-8")
    ct.triage_layer("train", _Stub(), dry_run=True)
    assert "不一致" in capsys.readouterr().out

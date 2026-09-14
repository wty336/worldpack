"""断点续跑守卫（2026-09-13 生产批前评估）。

背景（评估时在代码里逐行核实的两个事实）：出库是**最后一步**一次性落盘的，样本此前只活在
内存里 → 十来小时的批中途断了就全作废；而"分模块跑"会**覆盖 manifest**，让先产出的模块
无人认领。故补产线工作日志 + 幂等发布，本文件钉住：

- 中断后**已完成的卡落盘**、重跑**只补新增**（不重产 = 不重复付费）
- 分模块发布**不丢别的模块**（manifest 覆盖那个坑）
- 发布**幂等**（质检判定入日志 → 再发布不重判、数据集不抖动）
- 质检剔除的卡**会被重造**，而门禁丢弃的卡**不会**（重造=重复付费）
- 产线换代（提示词/卡生成器/模型变了）→ **拒绝续跑**，不许两代样本混一份数据集
- 半成品**不得**被标成 `complete`（目标粘性 + manifest 进度）
"""
from __future__ import annotations

import json

import pytest

from scripts.scenario_factory import assemble, worklog
from scripts.scenario_factory.assemble import quota_gaps

# --- 替身：构建器与两条 LLM 侧信道（编排逻辑才是本文件的被测对象） --------------


def _sample(card, *, text: str | None = None) -> dict:
    body = text if text is not None else f"素材 {card.card_id}"
    s = {"id": card.card_id, "module": card.module, "genre": card.axes.genre,
         "input": f"已有事实：无\n\n<回合内容>\n{body}\n</回合内容>", "output": body,
         "target_tokens": 600, "realized_chars": len(body), "long_input": False,
         "meta_soft": [], "name_confusables": []}
    if card.module == "judge" and card.corruptions:
        # 与真实 builder 同源的字段（`category` 来自卡面；material/narration/expect 是
        # 决策 16 的 confab 人读清单需要的键 —— 缺了就会被夹具掩盖住那条代码路径）
        c = card.corruptions[0]
        s.update({"category": c.category, "expect": c.expect, "detail": c.detail,
                  "material": f"材料 {card.card_id}", "narration": body,
                  "speaker": "某 NPC", "speaker_card": "角色卡正文"})
    return s


class Fake:
    """构建器替身：按卡 index 记录调用、可按脚本丢卡/中断。"""

    def __init__(self, *, drop_at=(), interrupt_at=None, text=None, limit=None):
        self.drop_at = set(drop_at)
        self.interrupt_at = interrupt_at
        self.text = text
        self.limit = limit          # 只回放这么多次调用（模拟"跑着跑着给自己收工"）
        self.index_calls: list[int] = []
        self.quality_calls: list[list[str]] = []
        self.label_calls = 0
        self.bad_ids: set[str] = set()

    # 构建器签名：build_xxx(llm, card[, sampling=...])
    def build(self, llm, card):
        i = int(card.card_id.rsplit("-", 1)[1])
        if self.interrupt_at is not None and len(self.index_calls) >= self.interrupt_at:
            raise KeyboardInterrupt
        if self.limit is not None and len(self.index_calls) >= self.limit:
            raise KeyboardInterrupt
        self.index_calls.append(i)
        if i in self.drop_at:
            return assemble.BuildResult(None, f"演绎丢弃: 替身丢卡 i={i}")
        return assemble.BuildResult(_sample(card, text=self.text), None)

    def build_compress(self, llm, card, *, sampling="off"):
        return self.build(llm, card)

    # 质检替身：返回点名的坏 id（并记下每次判了哪些 id）
    def quality(self, llm, samples, *, rate=0.20):
        self.quality_calls.append([s["id"] for s in samples])
        return [s["id"] for s in samples if s["id"] in self.bad_ids]

    def label(self, llm, samples, *, rate=0.20):
        self.label_calls += 1
        return {"checked": len(samples), "skipped": 0, "violations": [], "unknown": [],
                "prompt_version": "test"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """装好替身的产线环境：返回 `(out_dir, fake)`。"""
    fake = Fake()

    def install(f):
        monkeypatch.setattr(assemble, "build_extract_sample", f.build)
        monkeypatch.setattr(assemble, "build_judge_sample", f.build)
        monkeypatch.setattr(assemble, "build_compress_sample", f.build_compress)
        monkeypatch.setattr(assemble, "quality_sample", f.quality)
        monkeypatch.setattr(assemble, "label_check", f.label)
        monkeypatch.setattr(assemble, "_make_llm", lambda: object())

    install(fake)
    fake.reinstall = install
    return tmp_path / "out", fake


def _run(out_dir, *args) -> int:
    return assemble.main(["--layer", "train", "--out", str(out_dir), *args])


def _journal(out_dir, module="extract", layer="train"):
    return worklog.read_journal(worklog.journal_path(out_dir, layer, module))


def _rows(out_dir, module="extract", layer="train"):
    f = out_dir / layer / f"{module}.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


# --- 中断 → 续跑 -------------------------------------------------------------


def test_interrupt_keeps_finished_cards_on_disk(env):
    """中断时**已完成的卡必须已经落盘**（旧版样本只活在内存里 → 整批作废）。"""
    out, fake = env
    fake.interrupt_at = 2
    rc = _run(out, "--extract", "4")
    assert rc == 130
    j = _journal(out)
    assert sorted(j.done) == [0, 1], "中断前产完的两张没落盘"
    assert not (out / "train" / "manifest.json").exists(), "中断的批不该发布"


def test_rerun_resumes_and_does_not_reproduce_done_cards(env):
    """重跑**只补新增**：已完成的不重产（重产 = 重复付费）。"""
    out, fake = env
    fake.interrupt_at = 2
    assert _run(out, "--extract", "4") == 130
    first = {r["i"]: r["sample"] for r in _journal(out).entries_in_order()}

    fake.interrupt_at = None
    fake.index_calls.clear()
    assert _run(out, "--extract", "4") == 0
    assert fake.index_calls == [2, 3], f"续跑产了不该产的卡：{fake.index_calls}"
    rows = _rows(out)
    assert len(rows) == 4
    assert {r["id"]: r["input"] for r in rows}[first[0]["id"]] == first[0]["input"], \
        "续跑把第一轮已产样本重产了（内容变了）"


def test_completed_rerun_is_a_noop(env):
    """产完再跑一次同一个命令：不产卡、不重判质检（发布幂等）。"""
    out, fake = env
    assert _run(out, "--extract", "3") == 0
    n_calls, n_q = len(fake.index_calls), len(fake.quality_calls)
    assert _run(out, "--extract", "3") == 0
    assert len(fake.index_calls) == n_calls
    assert len(fake.quality_calls) == n_q, "重复发布又判了一遍质检（钱白花 + 判定会抖动）"


def test_status_reports_progress_without_touching_the_model(env):
    out, fake = env
    assert _run(out, "--extract", "3") == 0
    fake.index_calls.clear()
    assert _run(out, "--extract", "5", "--status") == 0
    assert fake.index_calls == [], "--status 不该产卡"
    assert _journal(out).header, "日志头应已落盘"


# --- 分模块发布（manifest 覆盖那个坑） ---------------------------------------


def test_partial_module_publish_keeps_the_earlier_module(env):
    """先 extract 后 judge：manifest 必须同时认这两个模块（旧版会覆盖成只剩 judge）。"""
    out, _ = env
    assert _run(out, "--extract", "2") == 0
    assert _run(out, "--judge", "2") == 0
    manifest = json.loads((out / "train" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["modules"]) == {"extract", "judge"}, \
        f"manifest 丢了模块：{sorted(manifest['modules'])}"
    assert manifest["modules"]["extract"]["count"] == 2
    assert len(_rows(out, "extract")) == 2, "先产的模块被覆盖/删掉了"


def test_pending_target_blocks_complete_flag(env):
    """半成品不得被标成 complete：目标粘性 + manifest 进度（否则 300/1000 与成品长得一样）。"""
    out, _ = env
    assert _run(out, "--extract", "2") == 0
    m = json.loads((out / "train" / "manifest.json").read_text(encoding="utf-8"))
    assert m["complete"] is True and m["progress"]["extract"]["pending"] == 0
    # 把目标抬到 5（模拟"将来要做 1000"，而本次只发布 judge）
    assemble.update_targets(out, "train", {"extract": 5})
    assert _run(out, "--judge", "2") == 0
    m = json.loads((out / "train" / "manifest.json").read_text(encoding="utf-8"))
    assert m["complete"] is False, "还有 3 张没产，却标成了 complete"
    assert m["progress"]["extract"]["pending"] == 3


def test_sticky_target_does_not_drive_production(env):
    """粘性目标只进账、**不驱动产出**：`--judge` 命令不该顺手把 extract 也产了。"""
    out, fake = env
    assemble.update_targets(out, "train", {"extract": 5})
    assert _run(out, "--judge", "2") == 0
    assert [c for c in fake.index_calls] == [0, 1], "只请求了 judge，却产了别的模块"
    assert _journal(out, "extract").done == set(), "extract 不该被动产出"


def test_targets_are_sticky_and_take_the_max(tmp_path):
    assert assemble.update_targets(tmp_path, "train", {"extract": 3}) == {"extract": 3}
    assert assemble.update_targets(tmp_path, "train", {"extract": 2}) == {"extract": 3}, \
        "目标被调小 = 半成品会被当成品"
    assert assemble.update_targets(tmp_path, "train", {}, persist=False) == {"extract": 3}


# --- 质检剔除 / 门禁丢弃：一个要重造，一个不重造 ------------------------------


def test_quality_rejected_card_is_excluded_then_rebuilt(env):
    """质检剔除的卡**不进数据集**，且下次跑会**重造**它（§7.4 的"剔除重造"）。"""
    out, fake = env
    fake.bad_ids = {"sc-10002-0002"}
    assert _run(out, "--extract", "3") == 0
    assert [r["id"] for r in _rows(out)] == ["sc-10000-0000", "sc-10001-0001"]
    j = _journal(out)
    assert j.rejected == {2: "质检自然度<1"} and 2 not in j.done

    fake.index_calls.clear()
    fake.bad_ids = set()
    assert _run(out, "--extract", "3") == 0
    assert fake.index_calls == [2], "被剔除的卡没被重造"
    assert len(_rows(out)) == 3


def test_gate_dropped_card_is_not_reproduced(env):
    """门禁丢的卡**不重造**：否则每续跑一次就把丢过的卡再烧一遍钱。"""
    out, fake = env
    fake.drop_at = {1}                     # 4 张丢 1 张 = 25%，仍在 30% 质量门内
    assert _run(out, "--extract", "4") == 0
    assert len(_rows(out)) == 3
    fake.index_calls.clear()
    assert _run(out, "--extract", "4") == 0
    assert fake.index_calls == [], "门禁丢过的卡被重产了"
    assert len(_rows(out)) == 3


# --- 版本守卫（两代样本混一份数据集 = 出库时看不出来的坏） -------------------


def test_header_mismatch_refuses_to_resume(env):
    out, fake = env
    assert _run(out, "--extract", "2") == 0
    jp = worklog.journal_path(out, "train", "extract")
    lines = jp.read_text(encoding="utf-8").splitlines()
    head = json.loads(lines[0])
    head["_header"]["factory_version"] = "oldgen"          # 模拟"产线改过了"
    jp.write_text("\n".join([json.dumps(head, ensure_ascii=False), *lines[1:]]) + "\n",
                  encoding="utf-8")
    fake.index_calls.clear()
    assert _run(out, "--extract", "4") == 1, "换代后竟然允许续跑"
    assert fake.index_calls == [], "被拒的续跑不该产卡"


def test_fresh_rewipes_the_progress(env):
    out, fake = env
    assert _run(out, "--extract", "2") == 0
    fake.index_calls.clear()
    assert _run(out, "--extract", "2", "--fresh") == 0
    assert fake.index_calls == [0, 1], "--fresh 之后应从头重做"


def test_no_publish_defers_gates(env):
    """`--no-publish`：只产卡入日志，不质检不出库（攒够了再发布一次）。"""
    out, fake = env
    assert _run(out, "--extract", "2", "--no-publish") == 0
    assert fake.quality_calls == [] and not (out / "train" / "manifest.json").exists()
    assert len(_journal(out).done) == 2
    assert _run(out, "--extract", "2") == 0               # 补一次发布
    assert len(fake.quality_calls) == 1 and len(_rows(out)) == 2


def test_sampling_guards_compress_only(env):
    """`sampling` 只对 compress 有意义（长档 n=4 拒绝采样）→ 只对 compress 设防。

    对 extract/judge 也比它，会出现两种坏结果：不带 `--sampling` 查进度时**误报换代**，
    以及**忘了带标志的续跑被误拒**（extract 的产出与 sampling 毫无关系）。
    """
    out, fake = env
    assert _run(out, "--extract", "2", "--sampling", "long") == 0
    fake.index_calls.clear()
    assert _run(out, "--extract", "2") == 0, "extract 不该因为 sampling 不同被拒"
    assert fake.index_calls == [] and _run(out, "--extract", "2", "--status") == 0


def test_sampling_mismatch_refuses_on_compress(env):
    out, fake = env
    assert _run(out, "--compress", "2", "--sampling", "long") == 0
    fake.index_calls.clear()
    assert _run(out, "--compress", "2", "--sampling", "off") == 1, \
        "compress 换了采样档 = 换了产出分布，必须拒绝续跑"
    assert fake.index_calls == []


def test_status_does_not_cry_wolf_about_sampling(env, capsys):
    out, _ = env
    assert _run(out, "--extract", "2", "--sampling", "long") == 0
    capsys.readouterr()
    assert _run(out, "--extract", "2", "--status") == 0
    assert "头不一致" not in capsys.readouterr().out


# --- 定向补产（类别家族配额，2026-09-14）-------------------------------------
#
# 动因：`plan-phase1-data.md` §4.2.2 要求 judge 训练集 **confab ≥40% / setting ≥25% / ooc ≥20%**，
# 而生产批 train judge 实测 confab 仅 **29.6%** 且**无任何代码在检查它**（`quota_gaps` 只查题材与
# compress 长档）—— 规格里写了配额就必须有代码在查。修法：`--only-category` 定向补产 +
# 类别配额门；补产的"跳过"必须与"丢弃"分开记（前者不是质量损失，混记会污染丢弃率与 30% 质量门）。


def test_skipped_is_done_but_never_a_drop(tmp_path):
    from scripts.scenario_factory.assemble import _progress, _stats_from_journals

    j = worklog.Journal.open(tmp_path, "train", "judge", {"journal": 1})
    j.append({"i": 0, "status": worklog.KEPT, "card_id": "c0", "sample": {"id": "c0"}})
    j.append({"i": 1, "status": worklog.DROPPED, "card_id": "c1", "reason": "演绎丢弃: x"})
    j.append({"i": 2, "status": worklog.SKIPPED, "card_id": "c2", "reason": "定向补产：本次只要 confab"})
    j.append({"i": 3, "status": worklog.REJECTED, "card_id": "c3", "reason": "质检自然度<1"})
    assert j.done == {0, 1, 2}, "skipped 要算已完成（否则 pending 永远归不了零）"
    assert 3 not in j.done, "rejected 仍不算完成 —— 它要重造（既定语义）"
    assert j.pending(4) == [3] and set(j.skipped) == {2}
    stats = _stats_from_journals({"judge": j})
    assert stats.built == 2 and stats.dropped == 1, "skipped 混进了丢弃账"
    assert stats.reasons["演绎丢弃"] == 1 and "定向补产" not in " ".join(stats.reasons)
    prog = _progress({"judge": j}, {"judge": 4})["judge"]
    assert prog["skipped"] == 1 and prog["dropped"] == 1
    assert prog["pending"] == 1, "只剩那张被质检剔除的要重造（skipped 不该留在待产里）"


def test_only_category_produces_only_that_category(env):
    """定向补产：只产指名类别的卡，其余索引记 skipped 且**不算丢弃**。"""
    from scripts.scenario_factory.assemble import card_category

    out, fake = env
    assert _run(out, "--judge", "40", "--only-category", "confab") == 0
    produced = [c for c in fake.index_calls]
    assert produced, "一张都没产"
    for i in produced:
        assert card_category("judge", 10000, i) == "confab", f"i={i} 不是 confab 却被产了"
    j = _journal(out, "judge")
    assert set(j.skipped) and len(j.skipped) + len(produced) == 40, \
        "跳过 + 实产 应恰好覆盖 0..39，不多不少"
    assert j.pending(40) == [], "跳过的索引不该留在待产里（否则 complete 永远 False）"


def test_only_category_then_publish_is_complete(env):
    """定向补产之后发布：`complete=True`，且丢弃账里没有"定向补产"这一项。"""
    out, fake = env
    assert _run(out, "--judge", "40", "--only-category", "confab") == 0
    fake.index_calls.clear()
    assert _run(out, "--judge", "40") == 0            # 无待产 → 只发布
    assert fake.index_calls == [], "跳过的索引被重新产了"
    m = json.loads((out / "train" / "manifest.json").read_text(encoding="utf-8"))
    assert m["complete"] is True
    assert m["progress"]["judge"]["skipped"] > 0
    drop = json.loads((out / "train" / "dropped-train.json").read_text(encoding="utf-8")) \
        if (out / "train" / "dropped-train.json").exists() else {"counts": {}}
    assert "定向补产" not in " ".join(drop["counts"]), "「跳过」被记进了丢弃留档"


def test_only_category_rejects_bad_usage(tmp_path, capsys):
    assert assemble.main(["--layer", "train", "--extract", "5", "--out", str(tmp_path),
                          "--only-category", "confab"]) == 1, "extract 没有类别轴"
    assert assemble.main(["--layer", "train", "--judge", "5", "--out", str(tmp_path),
                          "--only-category", "confabb"]) == 1, "未知类别应报错"
    assert "--only-category 只认" in capsys.readouterr().out


def _judge_rows(cats, genre="仙侠"):
    return [{"id": f"j{i}", "module": "judge", "genre": genre, "category": c,
             "target_tokens": 600, "realized_chars": 500} for i, c in enumerate(cats)]


def test_category_quota_floor_is_reported():
    """家族配额必须**有代码在查**（原先没有 → confab 29.6% 无人报警）。"""
    gaps = quota_gaps(_judge_rows(["confab"] * 3 + ["setting"] * 4 + ["ooc"] * 3))
    assert any("judge 家族 confab" in g and "30.0%" in g for g in gaps), gaps


def test_category_quota_passes_at_floor():
    gaps = quota_gaps(_judge_rows(["confab"] * 4 + ["setting"] * 3 + ["ooc"] * 3))
    assert not [g for g in gaps if "judge 家族" in g], gaps


def test_per_genre_class_minimum_is_reported():
    rows = _judge_rows(["confab"] * 4 + ["setting"] * 3 + ["ooc"] * 3, genre="仙侠")
    rows += [{"id": "x1", "module": "judge", "genre": "现代都市", "category": "confab",
              "target_tokens": 600, "realized_chars": 500}]
    gaps = quota_gaps(rows)
    assert any("现代都市/confab: 1 < 5" in g for g in gaps), gaps


# --- worklog 单元（日志语义本身） --------------------------------------------


def test_last_line_wins_and_rejected_is_retryable(tmp_path):
    j = worklog.Journal.open(tmp_path, "l", "m", {"journal": 1})
    c0 = {"id": "c0", "input": "素材0"}
    j.append({"i": 0, "status": worklog.KEPT, "card_id": "c0", "sample": c0})
    j.append({"i": 1, "status": worklog.DROPPED, "card_id": "c1", "reason": "演绎丢弃: x"})
    j.append({"i": 2, "status": worklog.REJECTED, "card_id": "c2",
              "sample": {"id": "c2", "input": "素材2"}, "reason": "质检自然度<1"})
    # 后写覆盖先写：质检把已产的第 0 张判成剔除（真实流程里 `{**entry, ...}` 保留 sample）
    j.append({"i": 0, "status": worklog.REJECTED, "card_id": "c0", "sample": c0,
              "reason": "质检自然度<1"})
    j2 = worklog.read_journal(j.path)
    assert j2.entries[0]["status"] == worklog.REJECTED, "后写的行没覆盖先写的"
    assert j2.done == {1} and set(j2.rejected) == {0, 2}
    assert j2.pending(3) == [0, 2], "被剔除的应可重造、门禁丢的不该重产"
    assert [s["id"] for s in j2.built_samples()] == ["c0", "c2"], \
        "被剔除的样本要能进 drop_flagged 的账（built − 剔除 = 出库）"
    assert j2.kept() == [], "剔除的不该留在保留集里"
    assert not j2.corrupt


def test_corrupt_line_is_not_counted_as_done(tmp_path):
    p = worklog.journal_path(tmp_path, "train", "extract")
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"_header": {"journal": 1}}) + "\n"
                 + '{"i": 0, "status": "kept", "card_id": "c0", "sample": {"id": "c0"}}\n'
                 + '{"i": 1, "status": "ke', encoding="utf-8")   # 断电写了一半
    j = worklog.read_journal(p)
    assert j.corrupt == 1 and j.done == {0}
    assert j.pending(2) == [1], "解析不出的行不算已完成（宁可多产一张，也不能少产）"


def test_seen_excludes_pending_indices(tmp_path):
    """重造时**必须**排除自己的旧指纹，否则重造版会被当成重复再丢一次（那张卡永远造不出来）。"""
    out = tmp_path / "out"
    j = worklog.Journal.open(out, "train", "extract", {"journal": 1})
    j.append({"i": 0, "status": worklog.REJECTED, "card_id": "c0",
              "sample": {"id": "c0", "input": "同一段素材"}})
    seen_all = assemble._seen_from_disk(out, "train", {})
    seen_regenerating = assemble._seen_from_disk(out, "train", {"extract": {0}})
    fp = assemble.fingerprint("同一段素材")
    assert fp in seen_all and fp not in seen_regenerating
    # 别的层照样看得见（跨层去重不能因为重造而漏）
    assert fp in assemble._seen_from_disk(out, "dev", {"extract": {0}})


def test_manifest_progress_marks_incomplete(tmp_path):
    m = assemble.write_layer("dev", [], tmp_path,
                             progress={"extract": {"target": 5, "kept": 2, "dropped": 0,
                                                   "rejected": 0, "pending": 3}})
    assert m["complete"] is False and m["progress"]["extract"]["pending"] == 3

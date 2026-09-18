"""训练导出的守卫（④）：**prompt 逐字对齐生产** + **目标能被生产解析器吃下**。

这两条是导出的全部意义所在：模板漂移 → 训练分布与推理分布不一致（学不到东西）；
目标不可解析 → 训出来的输出生产端读不懂。所以守卫重点不是"文件写出来了"，
而是"**改了会响**"。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from game_agent.compression import COMPRESS_SYSTEM, SUMMARY_MAX_TARGET
from game_agent.judge import JUDGE_SYSTEM, JudgeSystem, parse_verdict
from game_agent.memory import EXTRACT_SYSTEM, parse_facts
from scripts.scenario_factory import export_training as et


def _extract_row(i=0, *, labels=None, expect_empty=False):
    return {"id": f"sc-1100{i}-000{i}", "module": "extract", "genre": "仙侠", "version": "route-a-v1",
            "input": "已有事实：\n\n<回合内容>\n他递过一枚铜钱。\n</回合内容>",
            "existing": [], "expect_empty": expect_empty,
            "labels": labels if labels is not None else [
                {"type": "物品与装备", "text": "玩家的佩剑名为「听雨」", "importance": 8}],
            "target_tokens": 600, "realized_chars": 500, "long_input": False}


def _judge_row(i=0, *, category="confab"):
    return {"id": f"sc-1100{i}-000{i}", "module": "judge", "genre": "仙侠", "category": category,
            "expect": "通过" if category == "normal" else "问题类型：虚构事实",
            "detail": "" if category == "normal" else "把「白鸮」当作既成事实断言",
            "material": "<scene>第 3 天</scene>\n关键事实：\n- 玩家的佩剑名为「听雨」",
            "narration": "他说：「白鸮。」", "speaker_card": "personality：冷静",
            "target_tokens": 600, "realized_chars": 500, "long_input": False}


def _compress_row(i=0):
    return {"id": f"sc-1200{i}-000{i}", "module": "compress", "genre": "仙侠",
            "input": "<旧摘要>\n旧事。\n</旧摘要>\n\n<新增历史>\n新事。\n</新增历史>",
            "output": "# 剧情摘要\n\n- 玩家拿到了铜钱。", "preserve_points": [],
            "target_tokens": 600, "realized_chars": 500, "long_input": False}


# --- 1. 目标契约 -----------------------------------------------------------

def test_extract_target_round_trips_through_parse_facts():
    row = _extract_row(labels=[{"type": "物品与装备", "text": "玩家的佩剑名为「听雨」", "importance": 8},
                              {"type": "债务与人情", "text": "玩家欠沈砚五十两", "importance": 6}])
    target = et.target_of("extract", row)
    assert target == "8|玩家的佩剑名为「听雨」\n6|玩家欠沈砚五十两"
    assert parse_facts(target) == [("玩家的佩剑名为「听雨」", 8.0), ("玩家欠沈砚五十两", 6.0)]
    et.verify_target("extract", row, target)          # 不抛即过


def test_extract_empty_uses_the_production_sentinel():
    """无值得记的 → `无`（`parse_facts` 的哨兵），而不是空字符串。"""
    row = _extract_row(expect_empty=True)
    assert et.target_of("extract", row) == "无"
    assert parse_facts("无") == []
    et.verify_target("extract", row, "无")


def test_judge_targets_match_parse_verdict():
    defect = et.target_of("judge", _judge_row())
    normal = et.target_of("judge", _judge_row(category="normal"))
    assert defect == "问题类型：虚构事实：把「白鸮」当作既成事实断言"
    assert normal == "通过"
    assert parse_verdict(defect)[0] is False and parse_verdict(normal)[0] is True


# --- 2. 提示词逐字对齐生产 ---------------------------------------------------

def test_judge_user_message_comes_from_the_production_builder():
    """judge 的 user 必须**就是** `JudgeSystem._judge_messages` 的产物（复用而非重抄）。"""
    row = _judge_row()
    msgs = et.messages_of("judge", row)
    want = JudgeSystem(None)._judge_messages(row["narration"], row["material"])[1]["content"]
    assert msgs[1]["content"] == want
    assert msgs[1]["content"].startswith("<材料>\n") and "<最新叙事>" in msgs[1]["content"]


def test_system_prompts_are_the_production_constants():
    assert et.messages_of("extract", _extract_row())[0]["content"] == EXTRACT_SYSTEM
    assert et.messages_of("judge", _judge_row())[0]["content"] == JUDGE_SYSTEM
    assert et.messages_of("compress", _compress_row())[0]["content"] == \
        COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)
    # extract/compress 的 user 直接取出库样本的 input（工厂里就是生产模板渲染的）
    assert et.messages_of("extract", _extract_row())[1]["content"].startswith("已有事实：")


# --- 3. 校验会响（改错了必须报）--------------------------------------------

def test_verify_target_catches_a_mismatched_importance():
    row = _extract_row()
    with pytest.raises(AssertionError, match="不一致"):
        et.verify_target("extract", row, "5|玩家的佩剑名为「听雨」")   # 重要性被改


def test_verify_target_catches_a_flipped_judge_label():
    """缺陷样本的目标若被写成「通过」，生产解析器会读成"没问题" ⇒ 必须报。"""
    row = _judge_row()
    with pytest.raises(AssertionError, match="parse_verdict"):
        et.verify_target("judge", row, "通过")


# --- 4. 切分与层纪律 ---------------------------------------------------------

def test_holdout_split_is_deterministic_and_disjoint():
    rows = [_extract_row(i) for i in range(60)]
    a_tr, a_ho = et.split_rows(rows, "extract")
    b_tr, b_ho = et.split_rows(rows, "extract")
    assert [r["id"] for r in a_ho] == [r["id"] for r in b_ho], "同 seed 必得同一划分"
    assert len(a_ho) >= et.HOLDOUT_MIN
    assert not ({r["id"] for r in a_tr} & {r["id"] for r in a_ho}), "留出与训练集不得有交集"
    # 不同模块的划分互不相同（seed 里带 module）
    assert [r["id"] for r in et.split_rows(rows, "judge")[1]] != [r["id"] for r in a_ho]


def test_eval_layer_never_enters_the_training_files(tmp_path, monkeypatch):
    """**eval 是冻结的尺子**（决策 14/32）—— 同 id 混进来必须直接拒写。"""
    monkeypatch.setattr(et, "DATA_ROOT", tmp_path / "route-a")
    for layer in ("train", "dev", "eval"):
        (tmp_path / "route-a" / layer).mkdir(parents=True, exist_ok=True)
    same = _extract_row(0)
    for layer in ("train", "dev", "eval"):
        (tmp_path / "route-a" / layer / "extract.jsonl").write_text(
            json.dumps(same, ensure_ascii=False) + "\n", encoding="utf-8")
    for layer in ("train", "dev"):
        for name in ("judge", "judge_normal", "compress"):
            (tmp_path / "route-a" / layer / f"{name}.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "route-a" / "eval" / "judge.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(AssertionError, match="eval 层样本混进来"):
        et.export(tmp_path / "out", check_only=True, echo=lambda *a: None)


def test_mix_weights_follow_the_spec():
    """计划 §5：extract : judge : compress ≈ 4 : 3 : 2（dedup 0 / reflect 第一批 0）。"""
    assert et.TARGET_MIX == {"extract": 4, "judge": 3, "compress": 2}
    assert set(et.SOURCE_FILES["judge"]) == {"judge", "judge_normal"}, "judge 家族两个文件都要进"

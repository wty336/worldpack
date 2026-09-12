"""`scripts/rubric_x_calibrate.py` 的守卫测试（离线，零 API）。

决策 13 的 X 要能**复算**，所以三条性质必须钉住：

1. 分位与取整口径写死（最近秩法 + 向上取整到 5pp）——换个算法 X 就变了；
2. 对齐失败**不静默补零**：探针位样本排除、抽不出关键串的样本跳过（"空 = 未知 ≠ 通过"）；
3. 报告必须含决策 13 的三要素（X 是什么量 / 定在多少 / 依据哪批数据）。
"""

from __future__ import annotations

import pytest

from scripts.rubric_x_calibrate import calibrate, format_md, p95, round_up_to_5pp


def test_round_up_to_5pp():
    assert round_up_to_5pp(0.0) == 0.0        # 0 就是 0，不该被抬成 5pp
    assert round_up_to_5pp(0.20) == 0.20     # 正好落在 5pp 格点上 → 不动
    assert round_up_to_5pp(0.201) == 0.25
    assert round_up_to_5pp(0.049) == 0.05
    assert round_up_to_5pp(0.051) == 0.10


def test_p95_uses_nearest_rank():
    """最近秩法：P95 = 第 ceil(0.95·n) 个（1-based）——口径写死，换算法 X 会变。"""
    vals = [i / 100 for i in range(1, 21)]      # 0.01 … 0.20，n=20
    assert p95(vals) == 0.19                    # ceil(19) = 19 → 第 19 个
    assert p95([0.1]) == 0.1
    with pytest.raises(ValueError):
        p95([])


def _sample(sid, history, output, long_input=False):
    return {"id": sid, "module": "compress", "long_input": long_input,
            "input": f"<旧摘要>\n\n</旧摘要>\n\n<新增历史>\n{history}\n</新增历史>",
            "output": output}


def test_calibrate_aligns_tracks_and_skips_unknown():
    samples = [
        # 两条轨道一致（保全 1.0 vs 保真 2/2）→ diff 0
        _sample("c1", "他把剑擦亮，说这柄「听雨」跟了三年。", "摘要：「听雨」跟了玩家三年。"),
        # 轨道 1 丢一半（2 个关键串只保住 1 个），轨道 2 也判 1 分 → diff 0
        _sample("c2", "他提着「听雨」，又想起「龙井」的约定。", "摘要：他提着「听雨」。"),
        # 两条轨道分歧：轨道 1 全丢（0.0），轨道 2 却给满分（折算 1.0）→ diff 1.0
        _sample("c3", "他提着「听雨」走远了。", "摘要：他走远了。"),
        # 抽不出关键串 → 未知，跳过（不补 0）
        _sample("c4", "天气不错的样子，他慢慢走着。", "摘要：他慢慢走着。"),
    ]
    report = {"batch_valid": True, "probe_detection": 1.0,
              "prompt_version": "pv", "budget_policy": "bp", "judge_model": "m",
              "endpoint": {"model": "m"},
              "scores": [{"id": "c1", "保真": 2, "简洁": 2, "结构": 2, "流畅": 2},
                         {"id": "c2", "保真": 1, "简洁": 2, "结构": 2, "流畅": 2},
                         {"id": "c3", "保真": 2, "简洁": 2, "结构": 2, "流畅": 2},
                         # c4 在报告里（不是探针位），但抽不出关键串 → 走"跳过"分支
                         {"id": "c4", "保真": 2, "简洁": 2, "结构": 2, "流畅": 2}]}
    cal = calibrate(samples, report, metric="entities")   # 本夹具没有 preserve_points → 走实体口径
    assert cal["metric"] == "entities"
    assert [r["id"] for r in cal["rows"]] == ["c1", "c2", "c3"]
    assert cal["rows"][0]["diff"] == pytest.approx(0.0)
    assert cal["rows"][2]["diff"] == pytest.approx(1.0)
    assert cal["skipped_no_entities"] == ["c4"]
    assert cal["n"] == 3 and cal["x"] == 1.0          # P95 = 1.0 → 取整仍 1.0
    # 探针位样本（不在 scores 里）要单独列出来，不能当成"对齐失败"混在一起
    cal2 = calibrate(samples[:1] + [_sample("c9", "他提着「听雨」。", "摘要：「听雨」。")],
                     report, metric="entities")
    assert cal2["probes_excluded"] == ["c9"]


def test_calibrate_default_metric_uses_preserve_points():
    """工厂材料的默认轨道 1 口径是**要点锚点**（spec §7.2："规则管要点在不在"）。

    实体口径是给引擎真实轨迹的启发式；在合成材料上它抽的是"历史里出现过的数字/引号词"，
    摘要没义务复述 → 保全率成片 0.000 → `diff` 满格 → X 算出 100pp 的**废值**（实测过）。
    """
    def with_points(sid, hist, out, anchors):
        s = _sample(sid, hist, out)
        s["preserve_points"] = [{"text": "要点", "anchors": anchors}]
        return s

    samples = [with_points("p1", "材料甲", "摘要包含 听雨 二字", ["听雨"]),
               with_points("p2", "材料乙", "摘要完全没提那个词", ["龙井"])]
    report = {"batch_valid": True, "scores": [
        {"id": "p1", "保真": 2, "简洁": 2, "结构": 2, "流畅": 2},
        {"id": "p2", "保真": 0, "简洁": 2, "结构": 2, "流畅": 2}]}
    cal = calibrate(samples, report)                    # 默认 points
    assert cal["metric"] == "points"
    assert [r["track1"] for r in cal["rows"]] == [1.0, 0.0]
    assert cal["rows"][0]["diff"] == pytest.approx(0.0)   # 锚点全在 + 判官满分 → 一致
    assert cal["rows"][1]["diff"] == pytest.approx(0.0)   # 锚点全失 + 判官 0 分 → 也一致
    # 没有 preserve_points 的样本在 points 口径下算"未知"而非 0（须在报告里，否则先被判成探针位）
    report2 = {**report, "scores": report["scores"] + [
        {"id": "n1", "保真": 2, "简洁": 2, "结构": 2, "流畅": 2}]}
    cal2 = calibrate([_sample("n1", "材料丙", "摘要")], report2, metric="points")
    assert cal2["n"] == 0 and cal2["skipped_no_entities"] == ["n1"]


def test_report_carries_the_three_elements():
    cal = {"n": 2, "metric": "points",
           "rows": [{"id": "c1", "track1": 1.0, "track2": 1.0,
                     "long_input": False, "diff": 0.0}],
           "p50": 0.0, "p95": 0.0, "max": 0.0, "x": 0.0,
           "skipped_no_entities": [], "probes_excluded": ["c2"]}
    md = format_md(cal, samples_path="data/route-a/dev/compress.jsonl",
                   report_path="reports/rubric-eval.json",
                   report_meta={"probe_detection": 1.0, "batch_valid": True,
                                "prompt_version": "pv", "budget_policy": "bp",
                                "judge_model": "m", "endpoint": {"model": "m"}})
    assert "① X 是什么量" in md and "② 定在多少" in md and "③ 依据哪批数据" in md
    assert "X = 0pp" in md and "c2" in md and "prompt_version" in md

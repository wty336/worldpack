"""泄漏探针的守卫：**探针必须真的会响**。

一个永远不报警的探针比没有探针更糟（它让人以为检查过了）。所以这里的重点不是
"健康数据不报警"，而是"**决策 36 那种形态一定报警**" —— 用当时的真实形态做夹具：
confab 卡的材料整段丢掉「关键事实」段，于是"材料没有事实段 ⇒ 虚构事实"100% 成立。
"""
from __future__ import annotations

import json
import pathlib

from scripts.scenario_factory.leak_probe import main, probe, render


def _row(i, *, mod, material, narration, chars=800, target=600):
    return {"id": f"x{i}", "module": mod, "version": "route-a-v1", "genre": "仙侠",
            "material": material, "narration": narration, "realized_chars": chars,
            "target_tokens": target}


def _healthy(i, *, mod):
    """两类材料**同形**（都有关键事实段）、长度相近、无标记差异。"""
    cat = "normal" if mod == "judge_normal" else "confab"
    return _row(i, mod=mod,
                material=f"<scene>第 3 天</scene>\n关键事实：\n- 事实{i}\n",
                narration=f"第 {i} 段日常叙事，风从山谷里上来。\n",
                chars=800 + (i % 5) * 10)


def _layer(tmp_path, *, defects, normals):
    """建一层（`<tmp>/train/`）并返回**层目录**（`probe()` 要的是层目录，不是数据根）。"""
    d = tmp_path / "train"
    d.mkdir(parents=True, exist_ok=True)
    if defects is not None:
        (d / "judge.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in defects) + "\n", encoding="utf-8")
    if normals is not None:
        (d / "judge_normal.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in normals) + "\n", encoding="utf-8")
    return d


def _warns(rows):
    return [r for r in rows if r[0] == "WARN"]


def test_healthy_layer_has_no_warn(tmp_path):
    out = _layer(tmp_path,
                 defects=[_healthy(i, mod="judge") for i in range(60)],
                 normals=[_healthy(i, mod="judge_normal") for i in range(60)])
    rows = probe(out)
    # 防空跑：探针必须**真的读到了数据**（首版夹具给错目录，于是"无报警"是假通过）
    assert not any(name == "样本不足" for _, name, _ in rows), "探头没读到数据，这条测试是空转"
    assert len(rows) >= 15, f"探针项数不对：{len(rows)}"
    assert not _warns(rows), f"健康数据不该报警：{_warns(rows)}"
    assert "无报警 ✓" in render("train", rows)


def test_catches_decision36_material_hole(tmp_path):
    """**决策 36 的真实形态**：缺陷类材料缺「关键事实」段 ⇒ 探针必须报警。

    当时的数据是 train 457/457 命中、零误报 —— 一条不需要读叙事就能 100% 判对的规则。
    """
    defects = [_row(i, mod="judge", material=f"<scene>第 3 天</scene>\n",
                    narration=f"第 {i} 段叙事。\n") for i in range(40)]
    normals = [_row(i, mod="judge_normal",
                    material=f"<scene>第 3 天</scene>\n关键事实：\n- 事实{i}\n",
                    narration=f"第 {i} 段叙事。\n") for i in range(40)]
    rows = probe(_layer(tmp_path, defects=defects, normals=normals))
    warns = _warns(rows)
    assert warns, "决策 36 的形态没被抓到 —— 探针形同虚设"
    assert any("关键事实" in name for _, name, _ in warns), warns
    assert any("材料结构指纹" == name for _, name, _ in warns), warns


def test_catches_length_shortcut(tmp_path):
    """「短 ⇒ 通过」若成立，判官会学成"总挑毛病"的反面：不看内容只看长度。"""
    defects = [_healthy(i, mod="judge") for i in range(40)]
    normals = [_healthy(i, mod="judge_normal") for i in range(40)]
    for r in defects:
        r["realized_chars"] = 200
    for r in normals:
        r["realized_chars"] = 1600
    warns = _warns(probe(_layer(tmp_path, defects=defects, normals=normals)))
    assert any("长度分布" == name for _, name, _ in warns), warns


def test_catches_marker_shortcut(tmp_path):
    """生成期的锚点标记（「」）若成了类别线索，模型学的是"找引号"而不是"查一致性"。"""
    defects = [_healthy(i, mod="judge") for i in range(40)]
    normals = [_healthy(i, mod="judge_normal") for i in range(40)]
    for r in normals:
        r["narration"] = "她说「今日不练了」，转身走了。\n"
    warns = _warns(probe(_layer(tmp_path, defects=defects, normals=normals)))
    assert any("叙事含「」" in name for _, name, _ in warns), warns


def test_missing_or_one_sided_data_is_reported_not_crashed(tmp_path):
    """eval 层**故意没有负例**（尺子不补）—— 探针要如实说"算不出"，不能崩、不能静默通过。"""
    out = _layer(tmp_path, defects=[_healthy(i, mod="judge") for i in range(5)], normals=None)
    rows = probe(out)
    assert rows == [("INFO", "样本不足", rows[0][2])] and "正常 0" in rows[0][2]
    assert not _warns(rows), "「算不出」不是「通过」——但也不该报成泄漏"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert probe(empty)[0][0] == "INFO"          # 两个文件都不存在    assert main(["--layer", "eval"]) == 0        # 无 WARN ⇒ 退出码 0（eval 无负例）


def test_review_list_staleness_is_detected(tmp_path):
    """清单头写着"来源 xxx sha256 yyy"——**得有人去核对**，否则那只是清单里的一行字。

    实测教训：材料修复与负例补产都改过 `judge.jsonl`，而旧清单还在（我因此重生成过两次）。
    """
    from scripts.scenario_factory.leak_probe import probe_review_lists

    data = tmp_path / "data" / "route-a" / "train"
    data.mkdir(parents=True)
    src = data / "judge.jsonl"
    src.write_text('{"id": "a"}\n', encoding="utf-8")
    from game_agent.evalmeta import file_digest

    good = file_digest(src)[:16]
    reports = tmp_path / "reports"
    reports.mkdir()

    def _write(name, sha):
        (reports / name).write_text(
            f"# 出库人读清单：train / judge\n\n- 生成于 2026-09-18；来源 "
            f"`data/route-a/train/judge.jsonl`（sha256 `{sha}`）；**1 条**\n", encoding="utf-8")

    _write("judge-review-train-20260101.md", good)
    rows = probe_review_lists(reports, "train", root=tmp_path)
    assert rows and rows[0][0] == "INFO" and "过期 0 份" in rows[0][2], rows

    _write("judge-review-train-20260102.md", "0" * 16)      # 故意写错摘要
    rows = probe_review_lists(reports, "train", root=tmp_path)
    assert rows[0][0] == "WARN" and "过期 1 份" in rows[0][2], rows
    assert "judge-review-train-20260102.md" in rows[0][2]
    assert probe_review_lists(tmp_path / "nowhere", "train", root=tmp_path) == []  # 不报也不崩


def test_module_is_actually_runnable():
    """`python -m` 必须真的跑起来 —— **我这次就漏了 `__main__` 守卫**：
    命令静默返回 0、什么都不打印，看起来像"检查通过"。这类失败必须有个守卫。"""
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "scripts" / "scenario_factory" / "leak_probe.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src, "缺 __main__ 守卫 ⇒ python -m 调用是静默空转"
    assert "raise SystemExit(main())" in src

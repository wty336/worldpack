"""出库人读清单守卫：清单必须**完整、可定位、不吞异常**。

人读清单的失败方式很隐蔽——少了几条、旗帜没打上、没有 dropped 文件就崩，
这些都不会报错，只会让人**以为看全了**。
"""
from __future__ import annotations

import json
import pathlib

from scripts.scenario_factory.factory_review import (
    flags,
    label_violations,
    load_rows,
    main,
    render,
    render_full,
)


def _row(i, *, mod="extract", genre="仙侠", soft=None, conf=None, empty=False,
         target=600, realized=500):
    return {"id": f"sc-1000{i}-000{i}", "module": mod, "version": "route-a-v1", "genre": genre,
            "input": f"已有事实：无\n\n<回合内容>\n第 {i} 条的回合文本。\n</回合内容>",
            "existing": [], "expect_empty": empty,
            "labels": [] if empty else [{"type": "人物关系", "text": f"标签{i}", "importance": 3}],
            "meta_soft": soft or [], "name_confusables": conf or [],
            "target_tokens": target, "realized_chars": realized, "long_input": False}


def _layer(tmp_path, rows, *, dropped=None, violations=None):
    d = tmp_path / "train"
    d.mkdir(parents=True, exist_ok=True)
    (d / "extract.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    if dropped:
        (d / "dropped-train.json").write_text(json.dumps(dropped, ensure_ascii=False),
                                              encoding="utf-8")
    if violations:   # 新版格式：runs 列表
        (d / "label-check-train.json").write_text(
            json.dumps([{"checked": 1, "violations": violations}], ensure_ascii=False),
            encoding="utf-8")
    return tmp_path


def test_every_sample_appears_in_index_and_detail(tmp_path):
    rows = [_row(i) for i in range(5)]
    out = _layer(tmp_path, rows)
    md, full = render(out, "train", "extract"), render_full(out, "train", "extract")
    for r in rows:
        assert md.count(f"| `{r['id']}` |") >= 1, "索引里缺了这条"
        assert full.count(f"### `{r['id']}`") == 1, "详情里有重复或缺失"
    assert "**5 条**" in md and "全文：train / extract（5 条）" in full


def test_negative_example_is_marked_but_never_a_risk_item(tmp_path):
    """负例是卡面的**正常设计**（正负例 4:1），不是风险项。

    首版把它当风险项 → §2 的 64 条"风险"里 40 条是负例，真正的 1 条标签自检点名直接看不见。
    """
    out = _layer(tmp_path, [_row(0, empty=True)])
    md = render(out, "train", "extract")
    top = md.split("## 2. 先看这些")[1].split("## 3.")[0]
    assert "无 —— 本批没有风险项命中的样本" in top, "负例混进了风险清单"
    assert "负例（该回合无新事实）" not in top
    assert "| 负例 |" in md, "索引的「正/负」列没标出来"
    assert "**负例**" in render_full(out, "train", "extract"), "详情的标题没标负例"


def test_flagged_samples_are_hoisted_to_the_top(tmp_path):
    rows = [_row(0), _row(1, soft=["接口"], conf=["沈青秋"], realized=100), _row(2)]
    md = render(_layer(tmp_path, rows), "train", "extract")
    top = md.split("## 2. 先看这些")[1].split("## 3.")[0]
    assert "sc-10001-0001" in top, "有风险项的样本没被顶到前面"
    for want in ("温和元词: 接口", "近误人名: 沈青秋", "长度未兑现 100/600"):
        assert want in top
    assert "sc-10000-0000" not in top, "没风险项的样本不该占人读位"


def test_label_violation_is_surfaced_with_the_checker_verdict(tmp_path):
    viol = [{"id": "sc-10002-0002", "module": "extract", "items": ["标签「装备名为「密码本」」"]}]
    rows = [_row(2)]
    out = _layer(tmp_path, rows, violations=viol)
    md = render(out, "train", "extract")
    assert "⚠ 标签自检点名" in md and "装备名为「密码本」" in render_full(out, "train", "extract")
    assert label_violations(out, "train") == {"sc-10002-0002": ["标签「装备名为「密码本」」"]}


def test_label_table_lists_every_label(tmp_path):
    """标签总表是这份清单的**主要人读面**（每张卡的 4 条 fact 标签要能单独扫一遍）。"""
    rows = [_row(0), _row(1), _row(2, empty=True)]
    md = render(_layer(tmp_path, rows), "train", "extract")
    table = md.split("## 5. 标签总表")[1]
    assert table.count("| `sc-") == 2, "标签条数不对"
    for text in ("标签0", "标签1"):
        assert text in table
    assert "标签2" not in table and "fact 标签共 2 条" in md


def test_zero_drops_is_stated_not_crashed(tmp_path):
    """0 丢弃是**正常的**（本批就是），清单要把它说明白而不是崩或含糊过去。"""
    md = render(_layer(tmp_path, [_row(0)]), "train", "extract")
    assert "本层 **0 丢弃**" in md
    assert "没有卡被门禁丢掉" in md


def test_drop_reasons_are_listed_with_detail(tmp_path):
    drop = {"counts": {"演绎丢弃": 2},
            "ids": {"演绎丢弃": ["sc-10009-0009", "sc-10010-0010"]},
            "detail": {"演绎丢弃": {"sc-10009-0009": "演绎丢弃: 缺 anchor「听雨」",
                                    "sc-10010-0010": "演绎丢弃: 元叙述"}}}
    md = render(_layer(tmp_path, [_row(0)], dropped=drop), "train", "extract")
    assert "演绎丢弃 2" in md and "缺 anchor「听雨」" in md and "元叙述" in md


def test_cli_writes_both_the_list_and_the_fulltext(tmp_path, capsys):
    out = _layer(tmp_path, [_row(0), _row(1)])
    dest = tmp_path / "r.md"
    assert main(["--layer", "train", "--module", "extract", "--out", str(out),
                 "--md", str(dest)]) == 0
    full = tmp_path / "r-full.md"
    assert dest.exists() and full.exists(), "全文附录没写出来"
    assert "出库人读清单" in dest.read_text(encoding="utf-8")
    assert "全文（逐条叙事）：`" in dest.read_text(encoding="utf-8"), "清单没指向全文附录"
    assert "出库全文" in full.read_text(encoding="utf-8")
    assert "2 条" in capsys.readouterr().out
    assert main(["--layer", "dev", "--module", "extract", "--out", str(out)]) == 1, \
        "缺文件应报错退出，而不是写出一份空清单"


def test_judge_and_compress_shapes(tmp_path):
    """三模块字段形态不同，清单要各自渲染对（否则人会以为"字段没了 = 数据坏了"）。"""
    j = {"id": "sc-1", "module": "judge", "genre": "仙侠", "expect": "问题类型：设定矛盾",
         "detail": "听雨 → 听风", "speaker_card": "沈青秋：冷静克制", "material": "材料正文",
         "narration": "叙事正文", "realized_chars": 12, "target_tokens": 600}
    c = {"id": "sc-2", "module": "compress", "genre": "仙侠", "input": "历史正文",
         "output": "摘要正文", "preserve_points": [{"text": "保住听雨", "anchors": ["听雨"]}],
         "realized_chars": 4, "target_tokens": 600, "over_target": True}
    d = tmp_path / "train"
    d.mkdir(parents=True, exist_ok=True)
    for mod, row in (("judge", j), ("compress", c)):     # 写盘 = 走真实路径（含 sha256 溯源）
        (d / f"{mod}.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
    mdj = render_full(tmp_path, "train", "judge")
    mdc = render_full(tmp_path, "train", "compress")
    assert "问题类型" in mdj and "沈青秋：冷静克制" in mdj and "材料正文" in mdj and "叙事正文" in mdj
    assert "保住听雨" in mdc and "摘要正文" in mdc and "超提示词目标" in mdc
    assert "sha256" in render(tmp_path, "train", "judge"), "清单必须自证来源（哪份文件、什么摘要）"
    # 行由调用方直接传入（无对应文件）也不能崩 —— 预览/试渲染是合法用法
    assert "出库人读清单" in render(tmp_path / "nowhere", "train", "judge", [j])


def test_load_rows_reads_what_jsonl_holds(tmp_path):
    rows = [_row(0), _row(1)]
    assert len(load_rows(_layer(tmp_path, rows), "train", "extract")) == 2
    assert load_rows(pathlib.Path(tmp_path), "train", "judge") == []
    assert flags(_row(0), {}) == []

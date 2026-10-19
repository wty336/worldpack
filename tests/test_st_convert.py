"""N9 守卫：SillyTavern 人物卡 / 世界书的导入（以及导出往返）。

上游：`docs/sillytavern-borrow.md` §E-1（字段映射）、§E-3（读宽写严）、
`docs/plan-dsh-tavern-parity.md` 期 4、`docs/roadmap.md` **N9**。

三个部分，各守一件事：

1. **读宽（解析层）**：V1/V2/V3 三种载体、字符串/数组混用、缺字段、老世界书导出
   （`entries` 是 uid 字典）都要归一成同一套标准模型，**而不是拒绝作者**；
2. **不静默**：被丢掉的（`enabled:false`）、不支持的（`position`/`probability`）、
   猜不得的（`selectiveLogic` 的数值枚举）都必须出现在 warnings 里。
   这类导入工具最难发现的故障是"悄悄少了一条设定"——它不报错，只是世界变薄了；
3. **往返**：`lore → character_book → lore` 在**真实世界包**的 lore 上字段等价。
   往返是发现映射表错误最有效的手段：只做一个方向的话，"读错"与"写错"会混在一起。

**关于语料**：本仓库里没有真实 ST 人物卡（本机无网络，`samples/` 只有一份小说素材）。
所以往返测试用**本项目 8 个世界包里作者真实写下的 lore** 作为驱动数据——它们不是为
这个转换器写的（`secondary_keys` / `constant` / `/re/` 键都是 A 系列为别的问题加的），
因此能暴露"只对着映射表写、从没跑过真实数据"的那类错误。真实 ST 卡的验证仍是
N9 的收尾项，已记在 roadmap 里。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from game_agent import st_convert as st
from game_agent.worldpack import load_worldpack

REPO = Path(__file__).resolve().parent.parent
PACKS_ROOT = REPO / "world-packs"
SHIPPED = sorted(
    p for p in PACKS_ROOT.iterdir()
    if p.is_dir() and (p / "world.yaml").is_file() and not p.name.startswith("_")
)


# ---------------------------------------------------------------------------
# 1. 读宽：三种载体归一
# ---------------------------------------------------------------------------


def test_v2_card_is_parsed_without_warnings():
    raw = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "沈清秋",
            "description": "长安城沈府的长女。",
            "personality": "外冷内热",
            "tags": ["古代", "名门"],
            "character_book": {"entries": []},
        },
    }
    w = st.StWarnings()
    card = st.parse_card(raw, w)
    assert card["name"] == "沈清秋" and card["personality"] == "外冷内热"
    assert card["tags"] == ["古代", "名门"]
    assert not w, f"干净的 V2 卡不该有警告：{w.items}"


def test_v1_card_is_normalized_and_says_so():
    """V1 老卡（字段在顶层）：能读，但**要说清**走了老的形状（§E-3 的多版本回退）。"""
    raw = {"name": "老卡", "description": "旧格式", "mes_example": "<START>"}
    w = st.StWarnings()
    card = st.parse_card(raw, w)
    assert card["name"] == "老卡"
    assert any("V1" in m for m in w.items), w.items


def test_world_book_uid_dict_is_sorted_and_reported():
    """老的世界书导出是 `{"entries": {"0": …, "1": …}}`——按 uid 数值升序读，且说明这一点。"""
    book = {
        "entries": {
            "10": {"keys": ["k10"], "content": "第十条"},
            "2": {"keys": ["k2"], "content": "第二条"},
            "1": {"keys": ["k1"], "content": "第一条"},
        }
    }
    w = st.StWarnings()
    rows = st.character_book_to_lore(book, warnings=w)
    assert [r["id"] for r in rows] == ["entry_1", "entry_2", "entry_3"]
    assert [r["text"] for r in rows] == ["第一条", "第二条", "第十条"], "必须按 uid 数值升序"
    assert any("uid" in m for m in w.items), w.items


def test_string_lists_are_normalized():
    """§E-3 的类型归一：`keys` 用 `|` 分隔、`tags` 用逗号分隔，都要变成列表。"""
    card = st.parse_card({"name": "x", "tags": "古代,名门，第三人"})
    assert card["tags"] == ["古代", "名门", "第三人"]
    rows = st.character_book_to_lore(
        {"entries": [{"keys": "长安|沈府", "content": "正文"}]}
    )
    assert rows[0]["keys"] == ["长安", "沈府"]


# ---------------------------------------------------------------------------
# 2. 不静默：丢弃 / 不支持 / 猜不得
# ---------------------------------------------------------------------------


def test_disabled_entries_are_dropped_and_counted():
    book = {"entries": [
        {"keys": ["a"], "content": "要的"},
        {"keys": ["b"], "content": "关掉的", "enabled": False},
        {"keys": ["c"], "content": "老写法关掉的", "disable": True},
    ]}
    w = st.StWarnings()
    rows = st.character_book_to_lore(book, warnings=w)
    assert [r["text"] for r in rows] == ["要的"]
    assert any("跳过 2 条" in m for m in w.items), w.items


def test_entry_missing_keys_or_text_is_skipped_with_reason():
    book = {"entries": [
        {"content": "没有关键词"},
        {"keys": ["k"]},
        {"keys": ["ok"], "content": "这条能进"},
    ]}
    w = st.StWarnings()
    rows = st.character_book_to_lore(book, warnings=w)
    assert [r["id"] for r in rows] == ["entry_3"]
    assert sum("缺少" in m for m in w.items) == 2, w.items


def test_use_regex_wraps_keys_in_slashes():
    """ST 的 `use_regex: true` ↔ 本项目 lore 的 `/re/` 键（原生支持正则）。"""
    rows = st.character_book_to_lore(
        {"entries": [{"keys": ["沈.秋"], "content": "正则命中", "use_regex": True}]}
    )
    assert rows[0]["keys"] == ["/沈.秋/"]


def test_secondary_keys_keep_logic_and_do_not_guess_selective_logic():
    """带次键 → 用调用方给的 logic，并**明确说没做 selectiveLogic 数值映射**。"""
    w = st.StWarnings()
    rows = st.character_book_to_lore(
        {"entries": [{"keys": ["a"], "secondary_keys": ["b"], "content": "正文",
                      "selectiveLogic": 2}]},
        logic="NOT_ANY", warnings=w,
    )
    assert rows[0]["logic"] == "NOT_ANY"
    assert rows[0]["secondary_keys"] == ["b"]
    assert any("selectiveLogic" in m for m in w.items), w.items


def test_unsupported_fields_are_reported():
    """ST 有而本项目不借的字段（插入位置/加权随机）要**列出来**，不能悄悄吞掉。"""
    w = st.StWarnings()
    st.character_book_to_lore(
        {"entries": [{"keys": ["a"], "content": "正文", "position": "after_char",
                      "probability": 60, "sticky": 3}]},
        warnings=w,
    )
    joined = "\n".join(w.items)
    assert "position" in joined and "probability" in joined and "sticky" in joined, w.items


def test_empty_book_says_so_instead_of_returning_silently():
    w = st.StWarnings()
    rows = st.character_book_to_lore({"entries": []}, warnings=w)
    assert rows == []
    assert any("没有可导入的条目" in m for m in w.items), w.items


# ---------------------------------------------------------------------------
# 3. 往返：真实世界包的 lore 驱动
# ---------------------------------------------------------------------------


def _lore_dicts(pack_id: str) -> list[dict]:
    pack = load_worldpack(PACKS_ROOT / pack_id)
    return [e.model_dump() for e in pack.world.lore]


def test_round_trip_on_real_shipped_packs_and_field_usage_is_reported():
    """**核心**：8 个世界包的真实 lore → character_book → lore，映射字段等价。

    同时**如实报告**这套语料到底用到了哪些映射字段——如果某个字段在真实数据里
    零用量，那它对我们的价值就是"未验证"，不该假装它被覆盖了。
    """
    exercised: dict[str, int] = {}
    total = 0
    for pack_dir in SHIPPED:
        lore = _lore_dicts(pack_dir.name)
        if not lore:
            continue
        book = st.lore_to_character_book(lore, name=pack_dir.name)
        back = st.character_book_to_lore(book)
        assert len(back) == len(lore), f"{pack_dir.name}: 条目数变了 {len(lore)} → {len(back)}"
        for src, dst in zip(lore, back):
            total += 1
            # 映射表覆盖的字段必须等价（未映射的字段另见下面的说明性断言）
            assert dst["text"] == src["text"], f"{pack_dir.name}: text 不等价"
            assert dst["keys"] == list(src["keys"]), f"{pack_dir.name}: keys 不等价"
            assert bool(dst.get("constant")) == bool(src.get("constant")), "constant 不等价"
            assert list(dst.get("secondary_keys") or []) == list(src.get("secondary_keys") or []), (
                "secondary_keys 不等价"
            )
            for field in ("keys", "text", "constant", "secondary_keys"):
                if src.get(field):
                    exercised[field] = exercised.get(field, 0) + 1
            if any(k.startswith("/") and k.endswith("/") for k in src["keys"]):
                exercised["regex_keys"] = exercised.get("regex_keys", 0) + 1
    assert total >= 20, f"真实 lore 只有 {total} 条，往返覆盖太薄，这条守卫的价值会打折"
    # **实测结论**（2026-10，44 条真实 lore）：只用到 keys 与 text。
    # `secondary_keys` / `constant` / `/re/` 键在 8 张卡里**零用量**——这几行映射因此是
    # "单元测试覆盖、真实数据未验证"。不写成断言（否则一加真实用例它就红），
    # 而是写在这里 + roadmap §2.13；它们的正确性由下一条用构造数据单独钉住。
    assert exercised.get("keys", 0) == total and exercised.get("text", 0) == total, exercised


def test_round_trip_covers_every_mapped_field():
    """真实语料覆盖不到的映射行（次键 / 常驻 / 正则键）用构造数据**逐个走一遍**。

    为什么不能不测：这几个字段是主动支持的能力（A-2/A-3/A-4），映射表里各占一行；
    而真实卡语料里一次都没出现过（见上一条）。没有这条，那几行就是"写完从未运行"。
    """
    lore = [
        {"id": "plain", "keys": ["听雨"], "text": "剑名听雨。"},
        {"id": "with_secondary", "keys": ["沈府"], "text": "沈府是长安大族。",
         "secondary_keys": ["长女"], "logic": "AND_ANY"},
        {"id": "resident", "keys": ["历法"], "text": "本朝用夏历。", "constant": True},
        {"id": "regex", "keys": ["/第.天/"], "text": "日子一天天过去。"},
    ]
    back = st.character_book_to_lore(st.lore_to_character_book(lore))
    by_id = {r["id"]: r for r in back}
    assert by_id["plain"]["keys"] == ["听雨"]
    assert by_id["with_secondary"]["secondary_keys"] == ["长女"]
    assert by_id["resident"]["constant"] is True
    assert by_id["regex"]["keys"] == ["/第.天/"], "正则键往返后必须还是 `/re/` 形式"


def test_round_trip_is_stable_for_the_unmapped_fields():
    """**反过来说清楚**：ST 没有对应概念的字段不会在往返中存活——已知且刻意的损失。

    `logic` 特殊：它会被**重新推导**（有次键时取调用方给的 logic），所以原文的值
    只在恰好一致时才"看起来保留了"。这条断言的就是"不保留"，免得有人以为它无损。
    """
    lore = [{
        "id": "x", "keys": ["k"], "text": "t",
        "secondary_keys": ["s"], "logic": "NOT_ALL", "case_sensitive": False,
        "ignore_budget": True, "exclude_from_recursion": True, "no_recursion_trigger": True,
    }]
    back = st.character_book_to_lore(st.lore_to_character_book(lore))
    assert back[0]["logic"] == "AND_ANY", "logic 是重新推导的，不是原样带回"
    lost = set(lore[0]) - set(back[0])
    assert lost == {"case_sensitive", "ignore_budget",
                    "exclude_from_recursion", "no_recursion_trigger"}, lost
    # 而映射表覆盖到的那些必须回来
    assert back[0]["keys"] == ["k"] and back[0]["text"] == "t"
    assert back[0]["secondary_keys"] == ["s"]


def test_exported_book_is_reimportable():
    """导出物必须能被自己的解析器读回来（附带一条：id 走 comment）。"""
    book = st.lore_to_character_book(
        [{"id": "lin_sword", "keys": ["听雨"], "text": "剑名听雨。"}], name="demo"
    )
    assert book["spec"] == st.EXPORT_SPEC and book["entries"][0]["comment"] == "lin_sword"
    back = st.character_book_to_lore(book)
    assert back == [{"id": "lin_sword", "keys": ["听雨"], "text": "剑名听雨。"}]


# ---------------------------------------------------------------------------
# 4. 文件入口：拒绝要给办法
# ---------------------------------------------------------------------------


def test_png_card_is_refused_with_actionable_message(tmp_path):
    p = tmp_path / "card.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(st.StConvertError) as ei:
        st.read_card_file(p)
    msg = str(ei.value)
    assert "PNG" in msg and "导出为 JSON" in msg, msg


def test_missing_entries_key_is_reported_clearly():
    with pytest.raises(st.StConvertError) as ei:
        st.character_book_to_lore({"nope": 1})
    assert "entries" in str(ei.value)


def test_bad_json_is_reported(tmp_path):
    p = tmp_path / "card.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(st.StConvertError) as ei:
        st.read_card_file(p)
    assert "不是合法 JSON" in str(ei.value)


def test_card_to_source_text_carries_the_prose_fields():
    card = st.parse_card({
        "spec": "chara_card_v2",
        "data": {"name": "沈清秋", "description": "长安城长女", "personality": "外冷内热",
                 "scenario": "诗会", "first_mes": "她抬眼看你。"},
    })
    text = st.card_to_source_text(card)
    for piece in ("沈清秋", "长安城长女", "外冷内热", "诗会", "她抬眼看你"):
        assert piece in text, text


# ---------------------------------------------------------------------------
# 5. 合并进世界包：换掉生成器瞎编的 lore，且结果仍过严格校验
# ---------------------------------------------------------------------------


def test_cli_sillytavern_import_end_to_end(tmp_path):
    """`--format sillytavern --offline` 一条命令跑通（**真跑 CLI**，不是只调函数）。

    这是 N9 与作者之间的唯一接口，必须验证：退出码 0、降级说明被打印、世界书真的
    落进了 `world.yaml`（不是我调对了函数但 CLI 没接上）、合并之后仍过严格校验。
    """
    import subprocess
    import sys

    card = tmp_path / "card.json"
    card.write_text(json.dumps({
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "沈清秋",
            "description": "长安城沈府长女，诗会上以才名闻。",
            "personality": "外冷内热。",
            "scenario": "上元诗会。",
            "tags": "古代,名门",
            "character_book": {"entries": [
                {"keys": ["诗会"], "content": "上元诗会是长安城的盛事。", "comment": "festival"},
                {"keys": ["沈府"], "content": "沈府家风极严。", "comment": "shen_house",
                 "constant": True},
                {"keys": ["赌"], "content": "西市有赌坊。", "comment": "closed", "enabled": False},
            ]},
        },
    }, ensure_ascii=False), encoding="utf-8")

    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "import_story.py"), str(card),
         "--name", "st_probe", "--format", "sillytavern", "--offline",
         "--pack-dir", str(out)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )
    assert r.returncode == 0, f"CLI 退出码 {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "跳过 1 条 `enabled: false`" in r.stdout, r.stdout  # 降级说明真的打印了
    assert "世界书已合并 2 条" in r.stdout, r.stdout
    assert "合并后仍通过严格校验" in r.stdout, r.stdout

    pack = load_worldpack(out / "st_probe")
    ids = [e.id for e in pack.world.lore]
    assert ids == ["festival", "shen_house"], ids  # 关掉的那条不在
    assert any(e.constant for e in pack.world.lore)


def test_lore_merge_into_a_real_pack_still_validates(tmp_path):
    """把卡里的 lore 合并进一个真实包 → 仍然能被**严格加载**（写严）。

    这里的"校验"就是 `load_worldpack`：它的 97 个 `raise WorldPackError` 站点
    覆盖 schema、交叉引用、可达性、数值上限——`cli.py check-worldpack` 与
    `catalog.can_publish` 都只是它的调用方，没有第二条校验路径。
    """
    import shutil

    src = PACKS_ROOT / "ancient_jianghu"
    dst = tmp_path / "merged"
    shutil.copytree(src, dst)

    book = st.lore_to_character_book(_lore_dicts("ancient_jianghu"), name="ancient_jianghu")
    rows = st.character_book_to_lore(book)
    old = st.lore_into_world_yaml(dst / "world.yaml", rows)
    assert old, "合并应当返回被顶替的旧条目（作者要看得见换了什么）"

    merged = load_worldpack(dst)  # 校验不通过会抛 WorldPackError
    assert len(merged.world.lore) == len(rows)

"""extract 提示词去域化（Step 0）的守卫测试（离线）。

背景：`EXTRACT_SYSTEM` 原先把事实类型写成「身份、来历、名字、**剑名、师承**、喜好…」——
提示词层面就把题材写死成武侠，而 extract 恰是 Phase 1 必训模块（实测 railed 0/3）。
Step 0 换成题材中性枚举 + 跨题材示例；本文件守住两条**可离线断言**的性质：

1. **类型清单**必须题材中性（黑名单守卫，启发式）；示例句则相反，**必须**跨题材
   （否则等于换个方式把题材写死）；
2. **输出契约**不变：`重要性|事实` 行式 + 「无」哨兵，`parse_facts` 能原样解析。

提示词的**效果**（换题材后是否仍能抽出正确事实）只能真机验证，
执行记录见 `docs/plan-phase1-data.md` §2。
"""

from __future__ import annotations

from game_agent.memory import EXTRACT_SYSTEM, parse_facts

NEUTRAL_CATEGORIES = (
    "身份身世",
    "称谓与别名",
    "所属与来历",
    "物品与装备",
    "能力与技艺",
    "地点与势力",
    "承诺与约定",
    "债务与人情",
    "目标与线索",
    "关系变化",
)

# 题材专有词黑名单：若出现在**类型清单**里，等于把某题材的实体类型写死（去域化的判据）
DOMAIN_SPECIFIC_TERMS = (
    "剑名", "师承", "门派", "修为", "灵气", "银两", "镖局", "武林", "江湖",
    "信用点", "义体", "赛博", "魔杖", "咒语", "末日",
)


def _type_list() -> str:
    """取出「长期事实（…）」里的类型清单（示例句不在其中，故可分别断言）。"""
    start = EXTRACT_SYSTEM.index("长期事实（") + len("长期事实（")
    end = EXTRACT_SYSTEM.index("）", start)
    return EXTRACT_SYSTEM[start:end]


def test_extract_type_list_lists_all_neutral_categories():
    listed = _type_list()
    missing = [c for c in NEUTRAL_CATEGORIES if c not in listed]
    assert not missing, f"类型清单缺中性类别：{missing}"


def test_extract_type_list_has_no_domain_specific_terms():
    listed = _type_list()
    hits = [t for t in DOMAIN_SPECIFIC_TERMS if t in listed]
    assert not hits, f"类型清单又写死了题材专有词：{hits}"


def test_extract_examples_span_multiple_genres():
    """示例句必须跨题材：同一类别给出不同题材形态（古风 / 现代 / 科幻）。"""
    assert "光剑" in EXTRACT_SYSTEM, "缺少科幻形态示例"
    assert "货车" in EXTRACT_SYSTEM or "驾驶" in EXTRACT_SYSTEM, "缺少现代形态示例"
    assert "公司" in EXTRACT_SYSTEM or "舰队" in EXTRACT_SYSTEM, "缺少非门派型组织示例"
    assert "门派" in EXTRACT_SYSTEM, "古风形态示例应保留其一（作为三选一，而非唯一）"


def test_extract_prompt_contract_matches_parser():
    """输出契约不变（parser 是唯一真源，提示词不得漂出它的支持范围）。"""
    assert "重要性|事实" in EXTRACT_SYSTEM
    assert "「无」" in EXTRACT_SYSTEM
    assert parse_facts("8|玩家把佩剑改名为听雨") == [("玩家把佩剑改名为听雨", 8.0)]
    assert parse_facts("无") == []

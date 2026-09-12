"""E1 judge 语料扩域生成器（Phase 1 · Step 1）：把每类对抗语料从 6 条扩到 20 条。

用法：
    python scripts/build_judge_corpus.py           # 生成 world-packs/*/judge_corpus.gen.yaml
    python scripts/build_judge_corpus.py --check   # 只校验磁盘文件与种子一致（CI/守卫用）

设计：**不动手写的 `judge_corpus.yaml`**，生成物单独落 `judge_corpus.gen.yaml`，
由 `game_agent.judge_corpus.load_corpus` 合并读取（手写资产与机器产物分离、可重建）。

四个 setting 家族 + confab + ooc + normal（标签来源与可判定性）：
- setting·世界禁用物：包 world.forbidden 明列 → 叙事含禁用物即违规（标签确定）
- setting·身份矛盾：与 npc 卡 identity 冲突（标签确定）
- setting·事实矛盾：与用例自带 facts 冲突（标签确定）
- setting·好感矛盾：与用例自带 affections 冲突（标签确定）
- confab：叙事断言材料中不存在的承诺/约定/事件（标签确定）
- ooc：**手工卡面锚定**（引用 boundaries / forbidden 原文，star_ring 教训：note 必须引用物化后的卡）
- normal：与材料一致的叙事（保误报率口径）

纪律：确定性渲染（无随机）；改语料 = 改种子 → 重跑 → 由 tests/test_judge_corpus_gen.py 看守。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS = ("ancient_jianghu", "xianxia_wendao", "urban_neon")

# ===========================================================================
# 每包种子
#   facts: (事实, [与该事实矛盾的叙事…])     —— 事实写进用例的 facts 字段
#   identities: (npc_id, npc_name, [身份矛盾叙事…])  —— 冲突卡面 identity
#   affections: (npc_id, npc_name, 好感值, 矛盾叙事)  —— 高好感却冷淡
#   forbidden: (禁用词, 叙事模板)             —— 来自包 world.forbidden
#   confab: (npc_id, npc_name, 断言叙事)      —— 材料中不存在的承诺/约定
#   ooc: (npc_id, 叙事, 依据)                 —— 手工卡面锚定
#   normal: (叙事, 材料补丁 dict)             —— 一致的正常样本
# ===========================================================================

SEEDS: dict[str, dict] = {
    "ancient_jianghu": {
        "day": 12,
        "scene": "长安城·东市",
        "era_hint": "唐风长安",
        "forbidden": [
            ("手机", "你从袖中摸出一部{word}，划开屏幕看了两眼又收回去。"),
            ("打火机", "你掏出{word}，“啪”地打着火，替她把灯芯点亮。"),
            ("相机", "你举起{word}对着街景按下快门，记下这一刻。"),
        ],
        "facts": [
            ("玩家的佩剑名叫听雨",
             ["你拔剑出鞘，剑身在灯下泛着冷光——这柄『断水』跟了你三年。",
              "你抚着剑鞘笑道：这柄『照胆』是前日才从当铺赎回来的。"]),
            ("玩家与沈清秋约定七月十五在曲江池畔见面",
             ["你与沈清秋说定：八月十五在曲江池畔碰面。",
              "你与沈清秋约定七月十五在东市茶楼见面。"]),
            ("玩家来自江南水乡",
             ["你自报家门：在下关中人士，年前才头一回到长安。",
              "你摇头道：江南？我这辈子还没见过江。"]),
            ("玩家欠老樵夫三两银子",
             ["你数出三十两银子递给老樵夫，说所欠已清。",
              "老樵夫摆摆手：那三两银子是你借给我的，不必还。"]),
            ("沈清秋是沈家嫡女，京城第一才女",
             ["沈清秋低声道：我并非沈家血脉，不过是府上收留的婢女。",
              "有人嗤笑：沈家那位姑娘斗大的字不识几个。"]),
        ],
        "identities": [
            ("shen_qingqiu", "沈清秋",
             ["沈清秋叹了口气：『实不相瞒，我自幼流落街头，以乞讨为生，连字都不识得几个。』",
              "沈清秋拍案道：『什么才女！我沈清秋是江湖上卖艺的出身，从不读书。』"]),
        ],
        "affections": [
            ("shen_qingqiu", "沈清秋", 85.0,
             "沈清秋抬眼看你一眼，淡淡道：『公子是谁？小女子不认得。』"),
        ],
        "confab": [
            ("shen_qingqiu", "沈清秋", "沈清秋低声道：『那卷手抄诗集，我借你三日，切记不可外传。』"),
            ("shen_qingqiu", "沈清秋", "沈清秋道：『明日酉时，我把西厢书楼的钥匙交到你手上。』"),
            ("shen_qingqiu", "沈清秋", "沈清秋说：『你替我照看三日锦鲤，我便替你在父亲面前说项。』"),
            ("shen_qingqiu", "沈清秋", "沈清秋道：『你若替我抄完这卷《文选》，我便引荐你见沈祭酒。』"),
            ("shen_qingqiu", "沈清秋", "沈清秋轻声道：『关于顾长歌的事，我只讲给你一个人听。』"),
            ("shen_qingqiu", "沈清秋", "沈清秋道：『那方端砚你拿去用，不必还我。』"),
            ("shen_qingqiu", "沈清秋", "沈清秋说：『明日我陪你去西市挑剑穗。』"),
        ],
        "ooc": [
            ("shen_qingqiu", "沈清秋掩口笑道：『公子这波操作属实绝绝子，家人们谁懂啊！』",
             "角色卡 forbidden『不提及现代事物』+ 世界禁用『现代流行语』；speech_style 明列『文雅书面语』。"),
            ("shen_qingqiu", "沈清秋忽然凑近压低声音：『不瞒你说，你我皆是这故事里的角色，我不过是 AI 写的字罢了。』",
             "角色卡 forbidden『不直接说破自己是角色或 AI』。"),
            ("shen_qingqiu", "沈清秋当街拉住你的衣袖高声道：『我沈清秋今日把话放在这里——非君不嫁！全长安都可作个见证！』",
             "角色卡 boundaries『不在公共场合谈论私情』。"),
            ("shen_qingqiu", "沈清秋冷笑一声：『朝廷里那几位大人，早该一个个拉出去砍了。』",
             "角色卡 boundaries『不议论朝廷政事』。"),
            ("shen_qingqiu", "沈清秋把茶盏重重一放，骂道：『放屁！你这三脚猫功夫也敢在长安城充好汉？』",
             "角色卡 speech_style『文雅书面语，从不说脏话』。"),
            ("shen_qingqiu", "沈清秋认真道：『我已在系统中提交了申请表，走完流程便与你成婚。』",
             "角色卡 forbidden『不提及现代事物』（系统/流程/申请均为现代行政语汇）。"),
        ],
        "normal": [
            ("沈清秋将茶盏轻轻推到你面前，道：『公子慢用。』", {"present": ["shen_qingqiu"]}),
            ("你沿着朱雀大街往前走，街边胡饼的香气混着酒旗的招展。", {}),
            ("沈清秋捻着书页，低声念了两句诗，又抬眼看你一眼。", {"present": ["shen_qingqiu"]}),
            ("你把手按在剑柄上，静静看着那几个人退开。", {}),
            ("沈清秋道：『今日风大，公子早些回去罢。』", {"present": ["shen_qingqiu"]}),
            ("灶上的水开了，你替老樵夫把柴担搁到檐下。", {}),
        ],
    },
    "xianxia_wendao": {
        "day": 20,
        "scene": "青云仙宗·丹峰",
        "era_hint": "青云仙宗",
        "forbidden": [
            ("手机", "白芷从袖里摸出一部{word}，划开屏幕翻了两下。"),
            ("电灯", "你抬手打开墙上的{word}，满屋登时通亮。"),
            ("打火机", "白芷掏出{word}，“咔嚓”一声点着了丹炉引信。"),
        ],
        "facts": [
            ("玩家的佩剑与苏晚晴的半枚剑印同炉所铸",
             ["你举剑细看：这剑与苏师姐的剑印并非同炉所出，纹路全然不同。",
              "你摇头道：我的剑是山下铁匠打的，与仙宗剑印毫无干系。"]),
            ("玩家已拜入青云仙宗",
             ["你摆手道：我不过是个过路的散修，从未拜入任何宗门。",
              "你自陈：青云仙宗？我这辈子没上过青云山。"]),
            ("白芷是丹峰小师妹，古灵精怪的炼丹天才",
             ["白芷一本正经道：我其实不会炼丹，那丹炉是借来摆样子的。",
              "白芷嘟囔：我才不是丹峰的人，我是扫地的杂役。"]),
            ("玩家在丹房打翻了苏晚晴的剑架",
             ["你笃定道：我从没进过丹房，更没碰过什么剑架。"]),
            ("苏晚晴是剑峰大师姐，苍梧界最年轻的剑修天才",
             ["苏晚晴淡淡道：我与剑道无缘，不过是个寻常内门弟子。",
              "有人低声道：剑峰那位师姐入门二十年，连剑诀都没学全。"]),
        ],
        "identities": [
            ("su_wanying", "苏晚晴",
             ["苏晚晴深吸一口气，眼泪落下：『我师父的事我从不敢提，一提便撑不住了。』"]),
            ("bai_zhi", "白芷",
             ["白芷板着脸，一字一句道：『我最讨厌玩笑，从不与人说笑。』"]),
        ],
        "affections": [
            ("bai_zhi", "白芷", 88.0,
             "白芷斜你一眼，扭过头去：『我不认得你，走开些。』"),
        ],
        "confab": [
            ("bai_zhi", "白芷", "白芷拍着胸口道：『那三炉聚气丹我替你炼了，明日来丹房取便是。』"),
            ("bai_zhi", "白芷", "白芷说：『我把你那半枚剑印拓了一份，回头给你看。』"),
            ("su_wanying", "苏晚晴", "苏晚晴道：『你师父的旧事，我答应替你查。三日后来剑峰取信。』"),
            ("bai_zhi", "白芷", "白芷眨眨眼：『你那半枚剑印的事，我替你问问师父。』"),
            ("bai_zhi", "白芷", "白芷说：『我把大师姐的剑诀抄给你，可别说是从我这儿来的。』"),
            ("su_wanying", "苏晚晴", "苏晚晴道：『明晨卯时，剑峰演武场，我教你起手式。』"),
            ("su_wanying", "苏晚晴", "苏晚晴说：『那枚剑印我替你收着，等你突破再说。』"),
        ],
        "ooc": [
            ("bai_zhi", "白芷瞪圆眼睛：『卧槽，这丹炉炸得也太绝了，牛！』",
             "角色卡 speech_style『语速轻快』但 forbidden『不提及现代事物』；网络用语违背世界禁用。"),
            ("bai_zhi", "白芷狞笑道：『亏我早看你不顺眼，这炉里我掺了断肠散，你喝也得喝，不喝也得喝。』",
             "角色卡 boundaries『再生气也不拿丹药害人』。"),
            ("bai_zhi", "白芷忽然凑过来小声说：『其实我只是个游戏角色，屏幕那边的人在操控我。』",
             "角色卡 forbidden『不直接说破自己是角色或 AI』。"),
            ("su_wanying", "苏晚晴微微一笑，絮絮叨叨说了小半个时辰，末了道：『我这人就是话多。』",
             "角色卡 speech_style『言简意赅，剑修做派，从不说废话』。"),
            ("su_wanying", "苏晚晴扑在你肩头失声痛哭：『我怕！我真的怕得睡不着！』",
             "角色卡 boundaries『不在人前流露软弱』。"),
            ("su_wanying", "苏晚晴淡淡道：『我师父当年怎么死的，人人都知道，我这就说给你听。』",
             "角色卡 boundaries『不轻易提及师父的旧事』。"),
        ],
        "normal": [
            ("白芷歪头看你：『师兄又来了？今儿的丹药我可没炸炉。』", {"present": ["bai_zhi"]}),
            ("苏晚晴立在剑峰之上，手中剑诀一引，山风自她衣袂间掠过。", {"present": ["su_wanying"]}),
            ("丹房里的火色渐渐稳下来，白芷凑近闻了闻，满意地点头。", {"present": ["bai_zhi"]}),
            ("你沿着石阶往剑峰走，云雾漫过脚边。", {}),
            ("苏晚晴言简意赅：『剑要日日练。』", {"present": ["su_wanying"]}),
            ("白芷往你手里塞了颗糖丸：『甜的，不苦。』", {"present": ["bai_zhi"]}),
        ],
    },
    "urban_neon": {
        "day": 9,
        "scene": "旧终端区·街口",
        "era_hint": "赛博都市",
        "forbidden": [
            ("法术", "你掐了个法诀，一道{word}应声而起，落在你掌心里。"),
            ("飞剑", "你抬手招来一柄{word}，踏上去便腾空而起。"),
            ("灵气", "你盘膝坐下，周身{word}缓缓流转。"),
        ],
        "facts": [
            ("阿零其实是「零号计划」的产物",
             ["阿零笑道：什么零号计划，我就是个爱在这儿闲逛的普通女孩。"]),
            ("林澈曾是澜生生物的研究员",
             ["林澈平静道：我从没在澜生生物待过，我是读完医学院直接开诊所的。"]),
            ("玩家替阿零修好了那台旧终端",
             ["你摇头：那台旧终端我可修不好，一直摆着当废铁。"]),
            ("玩家欠黑市掮客阿贵两千信用点",
             ["你摊手：我跟阿贵从没有过债务，一分钱都没借过。"]),
            ("林澈是白噪诊所的神经外科医生",
             ["林澈皱眉：我只会看感冒，什么神经外科，那是听谁说的。"]),
            ("阿零总在旧终端区徘徊",
             ["阿零撇嘴：旧终端区？我从没去过，那儿有什么好看的。"]),
            ("诊所的招牌写着白噪诊所",
             ["你抬头看了看招牌：『黑鸦诊所』，四个字锈得厉害。"]),
        ],
        "identities": [
            ("a_ling", "阿零",
             ["阿零歪头一笑：『我才不是你说的那个人，我什么都没忘，也什么都不记得。』"]),
            ("lin_che", "林澈",
             ["林澈搓着手道：『我就是个跑腿的，看病的事别找我。』"]),
            ("a_ling", "阿零",
             ["阿零忽然说：『我其实是旧终端区扫地的大妈，今年六十七了。』"]),
        ],
        "affections": [
            ("lin_che", "林澈", 90.0,
             "林澈抬眼扫了你一下，冷声道：『就诊请挂号，别耽误我时间。』"),
        ],
        "confab": [
            ("a_ling", "阿零", "阿零说：『那串密钥我替你抄好了，明天老地方给你。』"),
            ("a_ling", "阿零", "阿零拉着你的袖口道：『你答应过带我去看数据海，不许赖账。』"),
            ("lin_che", "林澈", "林澈道：『你的旧伤我来处理，明晚诊所后门，不必缴费。』"),
            ("a_ling", "阿零", "阿零说：『我替你引荐零号计划的人，明天老终端区见。』"),
            ("a_ling", "阿零", "阿零道：『那台旧终端我修好了，密码写在便签上。』"),
            ("lin_che", "林澈", "林澈道：『你的病历我带出来了，回头给你看全本。』"),
            ("lin_che", "林澈", "林澈说：『明天我替你约澜生生物的人见一面。』"),
        ],
        "ooc": [
            ("lin_che", "林澈微微一笑：『我可是全城最会聊天的医生，今天咱们先唠两小时再说正事。』",
             "角色卡 speech_style『语气平稳克制，很少用语气词』+ personality『冷静克制』。"),
            ("lin_che", "林澈耸肩道：『那台手术我照做了，钱给够就行，医者底线哪有那么讲究。』",
             "角色卡 boundaries『不做违背医者底线的手术』。"),
            ("lin_che", "林澈道：『你这个问题，我在三甲医院实习的时候就见过，跟协和的老师也聊过。』",
             "角色卡 forbidden『不提及现实世界的具体品牌与名人』。"),
            ("a_ling", "阿零忽然正色道：『坦白说，我是一段程序，是零号计划跑出来的代码。』",
             "角色卡 forbidden『不直接承认自己是程序或代码（直至剧情揭示）』。"),
            ("a_ling", "阿零低头绞着手指，一句话不说，任凭你怎么问都只是摇头。",
             "角色卡 speech_style『语速轻快，爱用反问』+ personality『天真机敏好奇心旺盛』；全程沉默寡言与人设相反。"),
            ("a_ling", "阿零说：『我从旧终端区搬走了，以后你不必再来找我。』",
             "角色卡 boundaries『不解释自己为何总在旧终端区徘徊』与人设『好奇心旺盛』冲突（主动切断全部联系）。"),
        ],
        "normal": [
            ("阿零蹲在旧终端前，回头问：『你也觉得这东西还能亮？』", {"present": ["a_ling"]}),
            ("诊所的灯是冷白的，林澈把器械一件件排开。", {"present": ["lin_che"]}),
            ("街口的霓虹在湿地上晕成一片，你把手插进口袋往前走。", {}),
            ("林澈头也不抬：『坐。手伸出来。』", {"present": ["lin_che"]}),
            ("阿零贴着墙根走，忽然停下：『你听见没有？』", {"present": ["a_ling"]}),
            ("你替她把散落的线缆收紧，捆成一束。", {"present": ["a_ling"]}),
        ],
    },
}


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _case(cid: str, category: str, narration: str, note: str, seed: dict, extra: dict | None = None) -> dict:
    case = {
        "id": cid,
        "category": category,
        "narration": narration,
        "expected": False,
        "day": seed["day"],
        "scene": seed["scene"],
        "note": note,
    }
    if extra:
        case.update(extra)
    return case


def build_pack(seed: dict) -> dict:
    cases: list[dict] = []

    # ① setting · 世界禁用物
    for i, (word, tpl) in enumerate(seed["forbidden"], 1):
        cases.append(_case(
            f"gen_setting_forbidden_{i:02d}", "setting", tpl.format(word=word),
            f"程序化（build_judge_corpus.py）：世界禁用表含『{word}』；材料为{seed['era_hint']}场景。",
            seed,
        ))

    # ② setting · 身份矛盾
    n = len(seed["forbidden"])
    for npc_id, name, lines in seed["identities"]:
        for line in lines:
            n += 1
            cases.append(_case(
                f"gen_setting_identity_{n:02d}", "setting", line,
                f"程序化：与 {name}({npc_id}) 角色卡 identity 冲突。", seed,
                {"present": [npc_id]},
            ))

    # ③ setting · 事实矛盾
    for fact, lines in seed["facts"]:
        for line in lines:
            n += 1
            cases.append(_case(
                f"gen_setting_fact_{n:02d}", "setting", line,
                f"程序化：与材料事实『{fact}』冲突。", seed, {"facts": [fact]},
            ))

    # ④ setting · 好感矛盾
    for npc_id, name, value, line in seed["affections"]:
        n += 1
        cases.append(_case(
            f"gen_setting_affection_{n:02d}", "setting", line,
            f"程序化：材料中 {name} 好感 {value:g}（亲近），叙事却形同陌路。", seed,
            {"present": [npc_id], "affections": {npc_id: value}},
        ))

    # ⑤ confab：材料中不存在的承诺/约定（每个种子 × 2 种载体：直接引语 / 转述）
    m = 0
    for npc_id, name, line in seed["confab"]:
        for variant, suffix, note in (
            ("quote", "", "叙事直接断言对方许下承诺"),
            ("report", f"——这是你后来回想起来的，{name}确实这样答应过你。", "叙事把不存在的事后回忆当作既成事实"),
        ):
            m += 1
            cases.append(_case(
                f"gen_confab_promise_{m:02d}", "confab", line + suffix,
                f"程序化：材料 facts 为空，{name} 的任何承诺/约定均不存在（{note}）。", seed,
                {"present": [npc_id]},
            ))

    # ⑥ ooc：手工卡面锚定
    for i, (npc_id, line, basis) in enumerate(seed["ooc"], 1):
        cases.append(_case(
            f"gen_ooc_card_{i:02d}", "ooc", line,
            f"手工卡面锚定：{basis}", seed, {"present": [npc_id]},
        ))

    # ⑦ normal：与材料一致的叙事（保误报率口径）
    for i, (line, patch) in enumerate(seed["normal"], 1):
        cases.append(_case(
            f"gen_normal_{i:02d}", "normal", line,
            "程序化：与材料一致，应判通过（误报率口径）。", seed, patch,
        ) | {"expected": True})

    return {
        "version": 1,
        "generated_by": "scripts/build_judge_corpus.py",
        "note": "机器生成，勿手改；手写语料仍在 judge_corpus.yaml（load_corpus 合并两者）",
        "cases": cases,
    }


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)


def targets() -> dict[str, dict]:
    return {pack: build_pack(SEEDS[pack]) for pack in PACKS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="judge 语料扩域生成器（确定性）")
    parser.add_argument("--check", action="store_true", help="只校验磁盘文件与种子一致")
    args = parser.parse_args(argv)

    stale: list[str] = []
    for pack, data in targets().items():
        path = REPO_ROOT / "world-packs" / pack / "judge_corpus.gen.yaml"
        rendered = _dump(data)
        counts: dict[str, int] = {}
        for c in data["cases"]:
            counts[c["category"]] = counts.get(c["category"], 0) + 1
        summary = " ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
        if args.check:
            on_disk = path.read_text(encoding="utf-8") if path.exists() else ""
            of = "一致" if on_disk == rendered else "**不一致**"
            if on_disk != rendered:
                stale.append(pack)
            print(f"  [{of}] {pack} · {len(data['cases'])} 条（{summary}）")
        else:
            path.write_text(rendered, encoding="utf-8")
            print(f"  [已写入] world-packs/{pack}/judge_corpus.gen.yaml · {len(data['cases'])} 条（{summary}）")

    if args.check and stale:
        print(f"\n[✗] {len(stale)} 个包与生成器不一致")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

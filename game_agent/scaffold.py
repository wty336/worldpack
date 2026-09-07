"""E3（P3）：世界包脚手架——init-worldpack 生成六个带注释 YAML 骨架。

模板即是规范的可运行样例：B1 lore、P2 检定/门槛/收益曲线都以注释形式示范。
生成后立即可通过 check-worldpack（设计原则：模板必须自身过质量门）。
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES: dict[str, str] = {
    "world.yaml": """\
# 世界包：《{name}》——脚手架模板（规范见 docs/design.md §12）
# 改完即可校验：python -m game_agent check-worldpack
name: {name}
era: 架空大陆·青云城
start_scene: 青云城·南门
player_role: 初到青云城的旅人
player_goal: 在青云城立足，弄清自己的身世

core_rules:
  - 玩家是初到青云城的旅人
  - 修为以吐纳与实战磨砺

style_guide:
  - 古风白话，克制典雅
  - 旁白用第三人称，简短有力

forbidden:
  - 现代事物
  - 现代流行语

opening: |
  你背着行囊，走进了青云城。

# B1 Lorebook：带触发关键词的设定条目（地点/势力/物品/传闻）。
# 不进静态前缀，按「当前场景 + 主线目标 + 最近对话」命中关键词后按需注入。
lore:
  - {{id: qingyun, keys: [青云城], text: 青云城是大陆有名的修行之城，城中设有青云书院。}}
""",
    "schedule.yaml": """\
# 数值与日程定义：引擎状态的真值 schema（design.md §5）
day_action_points: 1

stats:
  charm:   {{label: 魅力, min: 0, max: 100, initial: 10}}
  martial: {{label: 修为, min: 0, max: 100, initial: 5}}
  silver:  {{label: 灵石, min: 0, max: 999999, initial: 50}}

affections:
  a_jiu: {{label: 阿九, min: 0, max: 100, initial: 5}}

# 剧情旗标：全部在此声明（初始值），正文只引用不新增
flags:
  met_jiu: false

# 行动（P2 特性活样板）：
#   check              → 检定三档（大成功 critical_effects / 成功 effects / 失败 failure_effects）
#   requires           → 执行门槛（条件 DSL，design.md §5.4：flags/stat/affection/day）
#   收益曲线           → {{base, spread?, decay_every?, decay_step?}}（范围随机 + 边际递减，最低 0）
actions:
  - id: cultivate
    label: 修炼
    cost: 1
    check: {{stat: martial, difficulty: 5, margin: 10}}
    effects:
      stats: {{martial: {{base: 4, spread: 1, decay_every: 20, decay_step: 1}}}}
    scene: 青云城·城郊
    present: []
  - id: visit_jiu
    label: 拜访阿九
    cost: 1
    requires: {{flags: {{met_jiu: true}}}}
    effects: {{}}
    scene: 青云城·茶寮
    present: [a_jiu]
""",
    "mainline.yaml": """\
# 主线节点链（design.md §6.1）。骨架先留空；示例见下方注释。
nodes: []

# 示例节点（取消注释即可用，注意 completion 的 flag 必须有写入路径）：
# - id: n1_meet
#   title: 初遇
#   when: {{all: []}}                        # 恒真：开场即触发
#   goal: 与阿九结识
#   completion: {{flags: {{met_jiu: true}}}} # 完成信号必须代码可验证
#   on_enter:
#     scene: 青云城·茶寮
#     present: [a_jiu]
#     briefing: 阿九在茶寮遇险，你出手相助，由此结识。
#   critical_choices:
#     - id: how_to_help
#       prompt: 你如何相助？
#       options:
#         - {{text: 挺身而出, effects: {{flags: {{met_jiu: true}}, affections: {{a_jiu: 3}}}}}}
#         - {{text: 以理劝解, effects: {{flags: {{met_jiu: true}}, affections: {{a_jiu: 2}}}}}}
""",
    "events.yaml": """\
# 事件规则（design.md §9）。三类触发：condition（条件）/ schedule（行动概率）/ time（日期）
events: []

# 示例：
# - id: ev_teahouse
#   title: 茶寮风波
#   trigger:
#     kind: schedule
#     action: visit_jiu
#     chance: 0.3
#   priority: normal
#   script: 茶寮来了几个闹事的修士，阿九朝你使了个眼色。
#   effects:
#     affections: {{a_jiu: 2}}
#   once: true
""",
    "endings.yaml": """\
# 结局规则（design.md §6.3）。auto = 条件满足自动进入；choice = 关键选择直达。
endings: []

# 示例：
# - id: ending_stay
#   title: 长留青云
#   when:
#     all:
#       - {{affection: {{a_jiu: {{gte: 80}}}}}}
#       - {{flags: {{met_jiu: true}}}}
#   kind: auto
#   text: 你留在了青云城，与阿九共度余生。
""",
    "npcs/a_jiu.yaml": """\
# NPC 角色卡（design.md §8.1）。affection_stages 区间须升序覆盖 0~100。
id: a_jiu
name: 阿九
identity: 青云城茶寮的少女掌柜
personality: 爽利直率，外热内细
speech_style: 口齿伶俐，爱用市井俏皮话
boundaries:
  - 不轻易向外人吐露身世
forbidden:
  - 不提及现代事物
affection_stages:
  - {{range: [0, 20], tone: 客气疏离}}
  - {{range: [21, 50], tone: 熟络玩笑}}
  - {{range: [51, 80], tone: 亲近信任}}
  - {{range: [81, 100], tone: 情根深种}}
memory_limit: 20
""",
}


def init_worldpack(name: str, root: str | Path) -> Path:
    """生成世界包骨架目录。已存在则抛 FileExistsError。"""
    target = Path(root) / name
    if target.exists():
        raise FileExistsError(f"目录已存在: {target}")
    for rel, template in TEMPLATES.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(template.format(name=name), encoding="utf-8")
    return target

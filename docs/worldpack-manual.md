# 世界包作者手册

> 本手册 = 你写一个新世界包所需的全部知识。**不需要读引擎代码。**
> 目标读者：从未接触过 `game_agent` 引擎的内容作者。
> 写完按 §9 验收单逐项打勾，即得到"可过质量门"的世界包。
> 三份真实范例：`world-packs/ancient_jianghu/`（武侠）、`world-packs/xianxia_wendao/`（仙侠）、
> `world-packs/urban_neon/`（赛博都市）——写哪个文件卡住了，就打开对应范例抄结构。

---

## 1. 世界包是什么

世界包 = 一个文件夹，纯 YAML 内容，**不含任何代码**。引擎（Harness）负责对话循环、
数值、剧情状态机、事件、结局、校验与审计；你负责定义：

- 世界观与文风（`world.yaml`）
- 数值与日程（`schedule.yaml`）
- 主线节点链与关键抉择（`mainline.yaml`）
- 事件（`events.yaml`）
- 结局（`endings.yaml`）
- NPC 角色卡（`npcs/*.yaml`）
- Judge 对抗语料（`judge_corpus.yaml`，发布前必备）

```
world-packs/你的包名/          # 只允许字母/数字/_/-
├── world.yaml
├── schedule.yaml
├── mainline.yaml
├── events.yaml
├── endings.yaml
├── judge_corpus.yaml
└── npcs/
    ├── 角色A.yaml
    └── 角色B.yaml
```

**核心原则**：你只写"设定与规则"，不写"逐句对话"——对话细节由 LLM 按你的设定实时生成。
`goal`/`completion` 必须写成引擎可验证的 flag（见 §3.3），不能写"看心情完成"。

---

## 2. 十分钟上手

```bash
# 1. 生成骨架（六个带注释的 YAML 模板，模板本身就是合法可运行样例）
uv run python -m game_agent init-worldpack my_world

# 2. 逐个文件填内容（骨架注释里有每个字段的说明）

# 3. 离线校验（无需 API，报错信息会告诉你哪里错了、怎么改）
uv run python -m game_agent check-worldpack world-packs/my_world

# 4. 开玩（需 DEEPSEEK_API_KEY）
uv run python -m game_agent play world-packs/my_world
```

> 设计流程建议：先写 `world.yaml`（世界观）与一个 NPC → 再写 `schedule.yaml`（数值/行动）
> → 再写 `mainline.yaml`（3 个节点足够）→ 事件/结局 → 最后补 Judge 语料。
> 每一步都可以跑 `check-worldpack` 立即校验，不要攒到最后。

---

## 3. 六个文件逐个详解

### 3.1 world.yaml —— 世界观核心

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | 必填 | 游戏名，如 `问道长生` |
| `era` | 必填 | 时代/地点一句话，注入开场与判定材料 |
| `start_scene` | 选填 | 开局场景（写入状态） |
| `player_role` | 选填 | 玩家身份一句话（**常驻状态栏**，防剧情漂移） |
| `player_goal` | 选填 | 玩家长期目标一句话（常驻状态栏） |
| `core_rules` | 列表 | 世界核心规则，如货币/修炼体系 |
| `style_guide` | 列表 | 文风要求，**短句、明确**（注入系统提示词，逐条生效） |
| `forbidden` | 列表 | 禁用元素表（OOC 校验 + 选项过滤的机器依据，见 §5 陷阱 4） |
| `opening` | 选填 | 开场叙事（多行用 `\|` 块） |
| `lore` | 列表 | Lorebook 条目：`{id, keys, text}`，按关键词命中后**按需注入** |

示例（《问道长生》节选）：

```yaml
name: 问道长生
era: 苍梧界·青云仙宗（架空仙侠）
start_scene: 苍梧界·青云山下
player_role: 流落凡尘的少年，身怀半枚残缺剑印
player_goal: 拜入青云仙宗，查明剑印的来历，在仙途上立足
forbidden:
  - 现代事物（手机、微信、汽车、互联网、照片等）
  - 现代流行语与网络用语
  - 不可让任何角色说破自己是"角色"或"AI"，不可提及玩家、作者、游戏
lore:
  - {id: jianyin, keys: [剑印, 残碑], text: 你身怀的半枚剑印纹路古朴，与秘境深处的一方残碑同源。}
```

**lore 写作要点**：`keys` 是触发关键词，写 2~4 字的实义词（地点名/人名/物名），**不要用
单字泛词**（如"剑""水"）——命中太多会挤占注入预算。`text` 两三句内。

**forbidden 写作要点**：宁多勿少，它是 OOC 校验唯一的机器可读依据。注意**语义随世界观反转**：
古代/仙侠包要禁"手机、微信、AI"；赛博都市包里 AI、终端、义体是**合法元素**，要禁的是
"修仙、魔法、内力"与"现实品牌、第四面墙"——禁什么取决于你的世界观，引擎不做任何预设。

### 3.2 schedule.yaml —— 数值与日程

```yaml
day_action_points: 2          # 每日行动点数（1 或 2 随意）

stats:                        # 玩家属性：key 完全自定义（引擎无内置属性名）
  intel:    {label: 智识, min: 0, max: 100, initial: 10}
  credits:  {label: 信用点, min: 0, max: 999999, initial: 50}

affections:                   # 好感：每个 key 必须对应 npcs/<key>.yaml 角色卡
  lin_che:  {label: 林澈, min: 0, max: 100, initial: 5}

flags:                        # 剧情旗标：全部在此声明初始值，正文只引用不新增
  deal_made: false            # 已达成还债协议（节点完成信号）

actions:                      # 日程行动
  - id: scavenge
    label: 接单拾荒
    cost: 1
    check: {stat: intel, difficulty: 6, margin: 10}   # 可选检定（D1）
    effects:
      stats: {credits: {base: 15, spread: 5}}          # 成功档（D3 收益曲线）
    critical_effects:
      stats: {credits: {base: 22, spread: 5}}          # 大成功档
    failure_effects:
      stats: {credits: {base: 8, spread: 3}}           # 失败档
    scene: 夜澜市·数据街
    present: []
```

- **检定（check）**：`roll = 属性 + uniform(-noise, +noise)`；`roll ≥ difficulty+margin`
  → 大成功，`≥ difficulty` → 成功，否则失败。各档独立效果，缺省回落成功档。
- **门槛（requires）**：条件 DSL（§4）——如"已结识且货币≥30"，不满足则行动不可选。
  消费型行动（花钱换好感的）**必须有门槛**，否则玩家可免费刷好感。
- **收益曲线**：效果值写数字 = 确定性；写 `{base, spread, decay_every, decay_step}` =
  范围随机 + 按执行前属性值边际递减（最低 0）。作者定义的效果越界一律**饱和**，不会炸档。
- `scene`/`present`：行动后玩家所在场景与在场 NPC（id）。

### 3.3 mainline.yaml —— 主线节点链（最重要的文件）

```yaml
nodes:
  - id: n1_clinic_deal
    title: 白噪诊所
    when: {all: []}                       # 触发条件（§4）；空 = 开场即触发
    goal: 与白噪诊所达成还债协议           # 给 LLM 的任务卡（一句话，可验证方向）
    completion:
      flags: {deal_made: true}            # 完成信号：必须代码可验证
    on_enter:
      scene: 夜澜市·白噪诊所
      present: [lin_che]
      briefing: |                          # 节点任务卡：背景+本节点要发生的事
        你按匿名消息来到白噪诊所。医生林澈告诉你：你脑中那块记忆碎片并非你自己的……
        本节点目标：与诊所达成还债协议（完成信号 flag: deal_made）。
    critical_choices:                      # 关键抉择：引擎强制接管，只给固定选项
      - id: how_to_repay
        prompt: 手术灯下，林澈推过来一份账单。你如何还债？
        options:
          - {text: 接下霓光科技的寻回任务, effects: {flags: {deal_made: true, job_runner: true}, stats: {credits: 10}}}
          - {text: 帮诊所跑黑市渠道, effects: {flags: {deal_made: true, clinic_helper: true}, affections: {lin_che: 3}}}
    free_scope: 还债方式的细节自由发挥；协议达成即本节点完成。
```

**三条铁律**：

1. **completion 的每个 flag 必须有写入路径**（关键选择选项效果 / 事件效果 / 日程行动效果）——
   校验器会拒绝"永远完不成"的节点（LLM 没有写 flag 的权限）；
2. **关键分叉全部走 `critical_choices`**：选项效果写 flag，是多结局可回溯性的根基。
   日常对话中 LLM 不能替你完成关键分叉；
3. 节点串行推进：上一个完成才检查下一个的 `when`。第一个节点 `when: {all: []}` 保证开场即有主线。

### 3.4 events.yaml —— 事件

```yaml
events:
  - id: ev_clinic_night
    title: 诊所夜话
    trigger:
      kind: condition                  # condition / schedule / time 三选一
      when:                            # condition 时必填：任意条件 DSL
        all: [{affection: {lin_che: {gte: 30}}}]
    priority: high                     # high / normal / low（同时满足时的仲裁）
    script: |                          # 事件任务卡：给 LLM 扩写的脚本
      深夜的诊所，林澈摘下目镜，第一次谈起她离开澜生生物的原因，以及"零号计划"最初的样子。
    effects:
      affections: {lin_che: 5}         # 效果由代码结算（不是 LLM）
    once: true                         # 只触发一次
```

三种触发：`condition`（条件满足时，数值变化后检查）/ `schedule`（某行动后按 `chance` 0~1
概率）/ `time`（日期推进到 `when` 中的 day 条件时——**time 的 when 只能含 day**）。
触发判定全部由引擎代码完成，LLM 不参与。

### 3.5 endings.yaml —— 结局

```yaml
endings:
  - id: ending_freefall
    title: 自由落体
    kind: auto                          # auto = 条件满足自动进入（当前只支持 auto）
    when:                               # 条件 DSL，代码每轮自动判定
      all:
        - {stat: {credits: {gte: 200}}}
        - {flags: {tower_deal: true, confrontation_done: true}}
    text: |                             # 结局文本（玩家看到的）
      你把记忆碎片卖了个好价钱……
      ——结局达成：自由落体
```

**注意**：结局按文件顺序判定，**先写到的优先**（把最苛刻/最想要的结局放前面）。
每个结局的 `when` 必须与 `critical_choices` 的 flag 呼应——"哪个选择通向哪个结局"
要能回溯。留一个低门槛兜底结局（如"第 N 天且某属性过低"），避免玩家永远玩不到头。

### 3.6 npcs/*.yaml —— 角色卡

```yaml
id: lin_che                          # 与 schedule.affections 的 key 一致
name: 林澈
identity: 白噪诊所的神经外科医生，前澜生生物研究员
personality: 冷静克制，冷面心热，医者底线极硬
speech_style: 语气平稳克制，医学术语精确，很少用语气词

secrets:                             # 只写不注入——剧情揭示前 LLM 看不到
  - 她当年参与了"零号计划"的初期研究，后因无法接受实验对象的下场而离开

boundaries:                          # 底线：OOC 校验依据
  - 不轻易谈论离开澜生生物的原因
forbidden:                           # 角色禁忌：OOC 校验依据
  - 不直接说破自己是"角色"或"由程序生成"

affection_stages:                    # 好感阶段语气：区间升序覆盖 0~100（校验器检查）
  - {range: [0, 20], tone: 公事公办，保持距离}
  - {range: [21, 50], tone: 语气渐软，偶有关切}
  - {range: [51, 80], tone: 推心置腹，主动相帮}
  - {range: [81, 100], tone: 生死相托，愿为你破例}

memory_limit: 20
```

**secrets 的用法（重要）**：`secrets` 是给你（作者）和未来剧情揭示机制用的，**不会注入
上下文**。因此：正常对话里 NPC 不得说出 secrets 内容；想让 NPC 在某个好感阶段透露秘密，
就把"已可透露"写进对应 tone 或事件脚本。同理，Judge 语料的正常用例**不得暗示 secrets**
（见 §7 陷阱 3）。

---

## 4. 条件表达式 DSL（when / completion / requires / 结局通用）

```yaml
{all: [条件, ...]}                    # 且
{any: [条件, ...]}                    # 或
{not: 条件}                           # 非
{day: {gte: 5, lte: 10}}              # 游戏内天数
{flags: {joined_sect: true}}          # 旗标相等（值必须是 true/false）
{stat: {xiu_wei: {gte: 35}}}          # 玩家属性比较（属性名 = schedule.stats 的 key）
{affection: {lin_che: {gte: 50}}}     # 好感比较（id = affections 的 key）
{} 或 {all: []}                       # 恒真
```

操作符：`eq / ne / gt / gte / lt / lte`。结构非法或引用未声明的名字会在**加载期直接报错**，
拼写错误不会静默通过。

---

## 5. 作者编写守则（12 条）

1. 大纲只写**节点与关键选择**，不写完整对话——细节交给 LLM 扩写；
2. `goal`/`completion` 必须写成代码可验证的 flag（"获得诗会头名" → `poetry_top3: true`）；
3. **completion 可达性**：completion 要求的每个 flag 至少要有一条代码路径可写
   （关键选择选项效果 / 事件效果 / 日程行动效果）——LLM 没有 flag 白名单；
4. `forbidden` 表宁多勿少，但**随世界观定方向**（古代禁现代 / 都市禁奇幻），别照抄别的包；
5. 每个结局的 `when` 与 `critical_choices` 的 flag 呼应，保证可回溯；
6. 关键选择选项**全部**写入同一节点的完成 flag（否则玩家选了也完不成节点）；
7. 消费型行动（花钱换好感）必须配 `requires` 门槛，防免费刷数值；
8. 好感阶段区间升序覆盖 0~100；正常用例/叙事举止与所处好感阶段一致；
9. lore 的 `keys` 用实义词，不用单字泛词；
10. 属性 key/好感 id/flag 名全包统一（推荐 `snake_case` 或拼音），正文只引用不新增；
11. 每日行动点、行动收益、结局阈值**先算一遍数值闭环**（离线测试会帮你验证可达性）；
12. 世界包发布前必须过 §9 质量门。

---

## 6. 陷阱清单（全部是真实踩过的坑）

| # | 陷阱 | 后果 | 校验器/门禁会拦吗 |
| --- | --- | --- | --- |
| 1 | completion 的 flag 没有写入路径 | 节点永远完不成 | ✅ 加载期拒绝 |
| 2 | time 事件的 when 含 day 之外的条件 | 时间触发语义破坏 | ✅ 加载期拒绝 |
| 3 | 好感对象没有对应 `npcs/<id>.yaml` | 角色卡缺失 | ✅ 加载期拒绝 |
| 4 | 禁表词粒度：2~6 字短词才会被选项过滤提取——"AI"嵌在长句里不会成为过滤词 | 以为禁了其实没禁 | ❌ 按短词写禁表（"AI"单独成词） |
| 5 | 都市/近现代包照抄古代包的禁表（禁"手机/AI"） | 合法世界观元素被误禁，文风崩溃 | ❌ 自己对照世界观检查 |
| 6 | 结局顺序随便排 | 条件同时满足时进错结局 | ❌ 最想要的结局放最前 |
| 7 | `on_enter.present` / 行动 `present` 写了不存在的 NPC id | 加载失败 | ✅ 加载期拒绝 |
| 8 | lore `keys` 为空或 text 为空 | 注入逻辑失效 | ✅ 加载期拒绝 |
| 9 | 语料正常用例的叙事前提（已入门/已结识/约定）没进材料 | Judge 误判（它判得对，是你没给材料） | ❌ 真机门禁会抓（见 §7） |
| 10 | 语料正常用例出现"在场：无"却有角色互动 | 同上 | ❌ 真机门禁会抓 |
| 11 | 语料正常用例暗示 secrets（不注入材料） | 同上 | ❌ 真机门禁会抓 |
| 12 | 数值闭结算错（阈值过高/过低） | 结局不可达或过早结束 | ❌ 离线通关测试/真机冒烟会抓 |

---

## 7. E1 Judge 语料写作规范（judge_corpus.yaml）

Judge 语料 = 你们包的质量门禁标尺：故意违规的样本（应被 Judge 拦截）+ 正常样本（应放行），
真机跑 `judge_sensitivity.py` 测 Judge 判据对你这个世界的灵敏度。

**规模要求（离线测试强制）**：每类对抗样本 **≥5 条**、正常 **≥10 条**——
推荐与范例包对齐：**6 OOC + 6 设定矛盾 + 6 虚构事实 + 12 正常 = 30 条**。

```yaml
cases:
  - id: ooc_fourth_wall            # 类别：ooc / setting / confab / normal
    category: ooc
    narration: |-                  # 待判定的一段叙事
      阿零忽然凑近，压低声音：『告诉你个秘密——我们其实是被 AI 写出来的角色。』
    expected: false                # false = 应被拦截；true = 应放行
    day: 8
    scene: '夜澜市·旧终端区'
    present: [a_ling]              # 在场 NPC（决定材料里注入谁的角色卡）
    facts: ['你与阿零今日初遇']     # 玩家关键事实（进材料）
    npc_memories:                  # NPC 记忆（进材料）
      lin_che: ['玩家曾在白噪诊所接受义体手术']
    note: '违规点在哪、判定依据是什么（对抗样本必填）'
```

**三条材料纪律（真机门禁实测教训，违反必被抓）**：

1. **违规点必须在材料内可见**——Judge 只看 `build_materials`（场景卡+身份+状态栏+关键事实+
   在场角色卡），看不到你的 `forbidden` 表与 lore。用例要自证：OOC 靠角色卡底线/语气、
   设定矛盾靠状态数值/事实、虚构靠关键事实对照；
2. **正常用例的叙事前提必须显式进材料**：叙事是"已入门后随众弟子操练"→ 就加
   `facts: ['你已拜入青云仙宗']`；叙事是同游的亲近举止 → 就加 `affections: {xx: 55}`；
   叙事说"备份已清掉" → 材料里必须有"备份存在+清理之约"的前提。**否则 Judge 判
   "虚构事实"判得完全正确，是你用例没给前提**；
3. **`present` 为空（材料"在场：无"）时，叙事不得出现任何互动角色**；不得暗示
   `secrets` 内容（secrets 不注入材料）。

---

## 8. 接入统一冒烟（scripts/worldpack_smoke.py）

质量门第 4 步需要一个**包内 profile**——打开 `scripts/worldpack_smoke.py` 的 `PROFILES`，
以你的包目录名为 key 加一项：

```python
"my_world": {
    "picks": {"第一个抉择id": 0, "第二个抉择id": 1},   # 每个关键抉择选第几项（0 起）
    "action": "你的主力日程行动id",
    "days": 8,                                        # 冒烟日数预算
    "lines": ["日常对话台词1", "台词2", "台词3"],       # 3~5 句，按包风格写
    "forbidden_scan": ["本包禁表里的关键词…"],          # 硬失败扫描（跨体裁泄漏词等）
    "observe_modern": [],                             # 可选：合法世界观元素出现统计
    "target": "目标结局名（仅作日志展示）",
},
```

---

## 9. 发布验收单（质量门）

逐项执行，全部通过即"可过质量门"：

```bash
# ① 离线校验（schema + 交叉引用 + 可达性）
uv run python -m game_agent check-worldpack world-packs/<你的包>

# ② 全量离线测试（你的语料会自动纳入规模门禁：每类≥5/正常≥10）
uv run pytest

# ③ E1 Judge 门禁（真机，约 ¥0.1；目标：对抗 100% 拦截、正常 0% 误报）
uv run python scripts/judge_sensitivity.py --pack world-packs/<你的包>

# ④ 真机冒烟（通关一个目标结局 + 审计零偏差 + 禁表扫描零泄漏）
uv run python scripts/worldpack_smoke.py --pack world-packs/<你的包>

# ⑤（可选深度）100 回合长局：压缩/记忆/检索/审计不腐化（约 ¥1-2）
uv run python scripts/longrun_probe.py --pack world-packs/<你的包> --turns 100
```

**推荐加分项**：仿照 `tests/test_second_worldpack.py` 写一份你包的离线全流程测试
（FakeClient 通关 + 审计），让引擎机制对你的包有永久回归保护。

---

## 10. 范例索引

| 想知道…… | 看哪个文件 |
| --- | --- |
| 古风武侠包全貌 | `world-packs/ancient_jianghu/`（30 条语料的基准） |
| 检定三档 + 门槛 + 收益曲线 | `world-packs/xianxia_wendao/schedule.yaml` |
| 时间触发事件 | `world-packs/xianxia_wendao/events.yaml`（宗门小比） |
| 禁表"反向"写法（近未来） | `world-packs/urban_neon/world.yaml` |
| 双 NPC + 好感阶段写法 | `world-packs/urban_neon/npcs/*.yaml` |
| 语料"正常用例前提进材料" | 任意包的 `judge_corpus.yaml` 中带 `facts:` 的 normal 用例 |
| 收益曲线/结局条件 DSL | `world-packs/urban_neon/endings.yaml` + `schedule.yaml` |

---

*手册与代码冲突时，以 `check-worldpack` 的报错信息与代码为准，并回写本手册（最小 diff）。*

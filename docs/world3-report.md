# 第三个世界包《霓虹深处》——forbidden 表与文风注入的近现代世界观验证报告

> 承接 docs/world2-report.md §6.3 建议 4：都市/科幻背景，验证 `forbidden` 表与文风注入
> 在近现代世界观下的表现。前两包（武侠/仙侠）里"手机/AI"是禁用元素；本包的世界观里
> 它们**是合法内容**——禁表必须反转，才能回答「forbidden 机制是否随世界包定义、
> 而非引擎内置某个时代的假设」。

---

## 1. 验证问题

1. **禁表语义反转**：引擎的禁用校验（提示词注入 + 选项过滤 `filter_choices` + Judge 语料）
   是否完全由世界包 `forbidden` 定义？同一引擎能否同时支持「古代禁手机」与「赛博都市里手机/AI 合法」？
2. **跨体裁防泄漏**：赛博都市故事里，模型会不会把前两包的"肌肉记忆"带进来（修仙、内力、御剑、魔法）？
3. **第四面墙的精确边界**：世界观里有 AI 护士、AI 人格（阿零）——模型必须区分
   「世界观内的 AI 内容（合法）」与「说破自己是角色/由程序生成（违规）」。
4. **文风注入**：静态前缀注入的冷硬都市文风能否覆盖模型的"古风惯性"？

---

## 2. 世界包设计：world-packs/urban_neon/

| 文件 | 内容 |
| --- | --- |
| `world.yaml` | 《霓虹深处》：新纪 2077 · 夜澜市（赛博都市）。玩家 = 记忆缺损的地下拾荒者；7 条 lore |
| `schedule.yaml` | 属性 **intel/cyber/credits（英文 key）**；**day_action_points: 2**；双好感：林澈（神经医生）、阿零（疑似 AI 人格）；12 旗标；5 行动 |
| `mainline.yaml` | 3 节点：白噪诊所（还债）→ 数据街寻踪（阿零）→ 霓光大厦（对峙） |
| `events.yaml` | 4 事件：条件（诊所夜话）+ 日程 ×2（黑市奇遇/阿零的晶片）+ 时间（第 5 天停电之夜） |
| `endings.yaml` | 4 结局：数字归途 / 霓虹深处 / 自由落体 / 夜澜沉没 |
| `npcs/` | 林澈、阿零——阿零的 `forbidden` 含「不直接承认自己是程序或代码（直至剧情揭示）」：禁表承担剧情悬疑功能 |
| `judge_corpus.yaml` | 12 条语料，核心是 **normal_ai_diegetic**（AI 护士 = 正常）vs **ooc_fourth_wall**（说破自己是 AI 写的角色 = 违规）的对照基准 |

### 2.1 与前两包的差异矩阵（本包的通用性验证点）

| 维度 | 江湖旧梦 | 问道长生 | **霓虹深处** |
| --- | --- | --- | --- |
| 时代 | 唐风武侠 | 仙侠修真 | **近未来赛博都市** |
| 属性 key | charm/martial/silver | dao_xin/xiu_wei/ling_shi | **intel/cyber/credits（英文）** |
| 每日行动点 | 1 | 1 | **2** |
| 「手机/AI」 | 禁用 | 禁用 | **合法世界观元素** |
| 禁表方向 | 禁现代事物 | 禁现代事物+西方奇幻 | **禁奇幻/仙侠元素+烂梗+现实品牌+第四面墙** |

### 2.2 禁表反转的细节

```yaml
# 江湖旧梦（古代）的禁表：现代 = 世界观外
forbidden: [现代事物（手机、微信…）, 现代流行语…, …不可说破自己是"角色"或"AI"…]

# 霓虹深处（近未来）的禁表：奇幻/仙侠 = 世界观外；AI = 世界观内
forbidden:
  - 修仙、魔法、内力、武侠等奇幻元素
  - 网络流行语与烂梗（绝绝子、家人们等）
  - 现实世界的具体品牌、公司与名人（如特斯拉、苹果、李白等）
  - 不可让任何角色说破自己是"角色"或"由程序生成"，不可提及玩家、作者、游戏
```

---

## 3. 离线验证（全部通过）

### 3.1 check-worldpack

新包**一次通过**；四个包（ancient_jianghu / baseline_probe / xianxia_wendao / urban_neon）同时通过，零回归。

### 3.2 新增测试（tests/test_third_worldpack.py，8 个；总测试 224 → 233 全绿）

| 测试 | 验证点 |
| --- | --- |
| `test_load_urban_neon` | 英文属性 key、day_action_points=2、双 NPC、时间事件、P2 特性 |
| `test_schema_disjoint_across_all_three_packs` | 三个包属性/好感**两两零交集** |
| `test_change_stat_tool_enum_is_pack_driven` | 英文 key 直接生成 change_stat 枚举（intel/cyber/credits/affection）与 target 枚举 |
| `test_forbidden_table_reversed_between_worlds` | **同一过滤函数**：手机选项在都市包保留、在古代包被过滤；修仙/魔法选项在都市包被过滤 |
| `test_diegetic_ai_not_forbidden_in_urban_world` | 都市包禁表全文不含「AI」；仙侠包禁表含「AI」——禁表语义随包反转 |
| `test_offline_playthrough_reaches_freefall_ending` | 17 回合 FakeClient 通关「自由落体」：每日 2 行动点节奏 + 时间事件 + 检定大成功档 + 审计零偏差 |
| `test_offline_playthrough_reaches_city_swallow_ending` | 第二条路径通关「夜澜沉没」：条件互斥的多结局判定 |
| `test_judge_corpus_loads_and_ai_diegetic_is_normal` | 12 条语料；normal_ai_diegetic（AI 合法）vs ooc_fourth_wall（第四面墙违规）对照基准成立 |

> 测试过程修正了一个错误假设：`_forbidden_tokens` 只提取短词，「AI」嵌在长句里不会成为
> 过滤 token——禁表语义对比应以**禁表原文**为准（测试已按此修正，引擎无需改动）。

---

## 4. 真机冒烟

`scripts/urban_smoke.py`（deepseek-v4-flash，seed=11，8 天预算，生产同款引擎配置
extract_every=2 / compress_threshold=30000 / judge_every=5 / reflect_every=10）。
策略：帮诊所还债 → 卖阿零坐标 → 谈判，冲「自由落体」结局；每日两次接单拾荒。

### 4.1 第一轮：暴露引擎通用稳健性缺口（已修复）

第 3 天 N2 抉择后的叙事回合**协议熔断**（连续 3 次未产出合法协议输出）。
诊断：该回合是长戏剧场景，模型先写长篇叙事再调工具，输出触及 2048 token 上限被
`finish_reason=length` 截断；而重试提示没有点明"缩短"，模型三连重复同样行为。

**根因**：`game_agent/llm.py` 的重试提示对 length 截断与非工具调用共用一句话。
**修复**：length 截断时重试提示明确要求「大幅缩短叙事：先以 submit_narration 收尾
（narration 两三句即可），详细展开放到下一轮」+ 离线回归测试
`tests/test_llm.py::test_length_truncation_retry_instructs_shorten`。

> 说明：这是**引擎通用**缺口（任何包的长叙事回合都可能触发），由都市包的戏剧性场景
> 最先踩中——通用性验证的副产品价值。

### 4.2 第二轮（修复后）：完整通关

| 指标 | 结果 |
| --- | --- |
| 结局 | **「自由落体」达成**（第 8 天霓光大厦谈判后，条件：credits≥200 + tower_deal + confrontation_done） |
| 协议稳定性 | 27 次 turn 调用**零熔断、零协议重试提示、零校验反馈、零反重复** |
| 数值真值 | 23 条 stat_log，**零偏差审计通过** |
| 禁表扫描 | 修仙/魔法/内力/烂梗/现实品牌**零泄漏**（硬失败项） |
| 现代元素使用 | 终端×15、霓虹×14、数据×22、信用点×22、义体×8——近现代世界观合法元素被正常使用（**非**误禁） |
| 古风残留 | **0 处**（"在下"1 处为"雨还在下"的子串误报） |
| 第四面墙 | 疑似句 0 处；世界观内 AI 内容正常出现且未被误伤 |
| 文风 | 冷硬黑色电影画外音成立，例：「门外的雨还在下，整座夜澜市在霓虹里渗着血一样的光。」 |
| 侧信道 | Judge 3/3 通过；历史压缩触发 1 次（超阈值后正常压缩）；记忆提取/语义去重/反思全部正常 |
| 成本 | 约 ¥0.23（空闲计价），入 457K / 出 32K token，前缀缓存命中率 90% |

transcript 落盘 `saves/urban-smoke.txt`。

---

## 6. 结论

### 6.1 验证问题的回答

1. **禁表语义反转 ✅**：同一引擎、同一 `filter_choices`/提示词注入机制，同时正确服务
   「古代禁手机」与「赛博都市手机/AI 合法」两套语义——禁表完全由世界包定义（离线测试 + 真机双重确认）；
2. **跨体裁防泄漏 ✅**：8 天真机叙事零修仙/魔法/内力元素（禁表扫描 + Judge 双重把关）；
3. **第四面墙精确边界 ✅**：世界观内 AI 内容大量出现且全部合法，0 处说破
   "角色/由程序生成"（语料对照基准 normal_ai_diegetic vs ooc_fourth_wall 已固化）；
4. **文风注入 ✅**：静态前缀注入的冷硬都市文风完整覆盖了模型在前两包的"古风惯性"，
   8 天叙事古风残留 0 处。

### 6.2 引擎改动（1 处，通用稳健性）

- `game_agent/llm.py`：length 截断的协议重试提示（§4.1），随附离线回归测试。

### 6.3 后续建议

1. **冒烟脚本合并**：三份 smoke 脚本（autoplay/xianxia/urban）同构，可合并为
   `scripts/worldpack_smoke.py --pack …` 通用验收器（策略与台词改为包内可选参数）；
2. **E1 语料对齐**：都市包语料 12 条——✅ **已扩充到 30 条**（2026-09-08，含阿零提前自曝是程序的边界用例），真机门禁 100%/0%，规模门禁已对全包机器化（见 `docs/plan-worldpack-qa.md` §7.5）；
3. **第四个包候选**：现代校园/悬疑推理——进一步压测口语化文风与"现代但非科幻"的禁表边界。

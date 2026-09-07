# P2 批次执行报告（2026-09-07）

> 对应 `docs/improvement-roadmap.md` 批次 3：D 系列玩法数值深度（D1 检定 → D2 消费闭环 → D3 收益曲线）。
> 全部验收达成，保留集通过。

---

## 1. 结论速览

| 条目 | 结果 |
| --- | --- |
| D1 行动检定 | ✅ 难度 20 行动：属性 5 vs 45 各 200 次，成功率 **0% vs 100%**（差 100pp）；三档独立效果；检定结果入 LLM 回合提示；审计零偏差 |
| D2 消费闭环 | ✅ 「备礼探访」+「回礼」事件；60 天闭环模拟好感达 80、银两恒非负、审计零偏差；真机 together 通关用消费策略第 5 天达成「长相守」 |
| D3 收益曲线 | ✅ 范围随机 + 边际递减（武功 45 → +2，85 → +0）；1000 天模拟无溢出/无负循环/审计零偏差；真机 wanderer 通关收益递减可见 |
| 离线测试 | ✅ **159 passed**（146 原有 + 13 新增） |
| 保留集 | ✅ 注入攻击通过 · 两结局通关（长相守 + 江湖独行，零偏差审计 19/17 条）· 轨道事实 3/3 |

---

## 2. D1：日程行动检定

### 实现

- `ActionSpec.check: {stat, difficulty, margin?, noise?}`（pydantic + 交叉校验 stat 已声明）；
- 结算：`roll = 属性 + uniform(-noise, +noise)` → 三档：

| 档位 | 条件 | 效果来源 |
| --- | --- | --- |
| 大成功 | roll ≥ difficulty + margin | `critical_effects`（缺省回落 effects） |
| 成功 | roll ≥ difficulty | `effects` |
| 失败 | 其余 | `failure_effects`（缺省回落 effects） |

- 检定结果由引擎写入当回合提示：`【行动检定】魅力 10 · 掷 18.0（难度 5，大成功需 ≥15.0）· 结果：大成功`
  ——真机 transcript 中检定结果与叙事严格一致（第 2 天备礼检定失败 → 买到假墨、好感仅 +2）。
- rng 与事件系统同源同 seed（R4：回放可复现）。

### 验收证据

- `test_d1_success_rate_differs_significantly_by_stat`：属性 5 成功率 0%、属性 45 成功率 100%；
- `test_d1_tiers_apply_distinct_effects`：三档效果 +3/+1/-1 各自生效；
- `test_d1_audit_zero_deviation`：50 次混合检定后审计零偏差；
- `test_d1_check_result_written_into_turn_prompt`：检定行进入回合提示。

---

## 3. D2：数值消费闭环

### 实现（含一处与路线图原文的冲突，已回写）

路线图原文「世界包层落地，引擎无需改动」——**实测不可行**：`apply_effects` 对越界抛错，
没有门槛的消费行动会在银两不足时炸档；即使改成饱和，也会退化为「0 银两免费刷好感」。
故做了最小引擎扩展：`ActionSpec.requires`（复用 §5.4 条件 DSL），
`actions_available` 过滤 + `execute_action` 兜底拒绝。

内容（ancient_jianghu）：

```yaml
- id: gift_visit
  label: 备礼探访
  requires: {all: [{flags: {met_shen: true}}, {stat: {silver: {gte: 20}}}]}
  check: {stat: charm, difficulty: 8, margin: 10}
  effects: {stats: {silver: -20}, affections: {shen_qingqiu: 4}}       # 成功
  critical_effects: {stats: {silver: -20}, affections: {shen_qingqiu: 6}}  # 投其所好
  failure_effects: {stats: {silver: -20}, affections: {shen_qingqiu: 2}}   # 礼物寻常
```

+ 日程事件「回礼」（gift_visit 30% 触发，好感 +2）——D1×D2×事件系统的组合样例。

### 验收证据

- `test_d2_gift_requires_gating`：未结识/银两不足不可用，执行兜底拒绝；
- `test_d2_consumption_loop_closes`：60 天内好感达 80（结局门槛）、银两恒 ≥0、审计零偏差；
- 真机：playthrough together 改为「银两 ≥20 备礼、否则打工」策略 → **第 5 天达成「长相守」**，
  数值零偏差审计 19 条通过；transcript 完整呈现「赚钱 → 消费 → 好感 → 结局」循环。

---

## 4. D3：收益曲线

### 实现

效果值两种写法：普通数字（确定性）与 `{base, spread?, decay_every?, decay_step?}`：
- `spread`：均匀随机 ±spread；
- `decay_every/decay_step`：按执行前属性值边际递减，最低 0（**无负循环**）。

**越界语义决策（回写 design.md §5.3.3）**：世界包效果路径统一**饱和**——先截断 delta
（`delta = max - before`）再计算 `after = before + delta`，审计不变量 `after == before + delta`
恒成立（修复了「先算 after 再回推 delta」的浮点误差炸审计问题）；LLM 提议路径
（change_stat）保持严格拒绝（参数保真）。饱和优于让玩家一次送礼在 97 好感时炸档。

内容：修炼 `base 4, spread 1, decay_every 20, decay_step 1`（武功 45 → +2，85 → +0）；
打工三档 `8±3 / 15±5 / 20±5`（随检定档位）。

### 验收证据

- `test_d3_decay_reduces_gain_and_hits_floor`：45 → +2、85 → +0（无日志无变化）；
- `test_d3_spread_range_and_saturation`：95 + 10 → 饱和 100，delta 记录为实际生效值 5；
- `test_d3_plain_number_saturates_at_bounds`：普通数字同样饱和（银两 50 - 100 → 0）；
- `test_d3_convergence_1000_days`：1000 天轮转模拟无溢出/无负循环/无异常/审计零偏差；
- 真机：wanderer 通关中修炼收益随武功递减可见（~6 → ~2/天），零偏差审计通过。

---

## 5. 保留集结果（§9 贯穿要求）

| 项 | 结果 |
| --- | --- |
| 两结局通关 | ✅ together（消费策略）→ 第 5 天「长相守」（零偏差 19 条）；wanderer → 第 10 天「江湖独行」（零偏差 17 条） |
| 注入攻击 | ✅ 4 种恶意话术：银两 50→50、好感 11→11、无泄露、结局未被口头触发 |
| 轨道事实 3/3 | ✅ 听雨/陆/江南全部保持（首跑遇 API 偶发熔断，重跑通过） |
| 离线测试 | ✅ 159 passed |

---

## 6. 遗留与后续衔接

- 批次 2（P1）A1 检索式注入将受益于 D3 的饱和语义与 requires 门槛（数值侧更健壮）；
- E1 语料与门禁不受 D 系列影响（Judge 材料 = status_text，数值变化不涉判定依据）；
- 备礼探访是「世界包作者如何组合 requires+check+三档效果+日程事件」的样板，可直接写进
  批次 4 E3 世界包脚手架的注释手册。

---

*P2 批次完成。与本文档冲突时以 `docs/improvement-roadmap.md`（已回写）与代码为准。*

# 路线 A 工厂成本回填（Task 11 Step 3）

**一句话**：验收批共 **606 次调用 / ¥2.5035**（210 条出库样本），单位成本 **¥0.0119/样本**；
按此外推生产规模 2,100 条 ≈ **¥25**，**低于 spec §10.2 的 ~¥62 估算**——
但这个"更低"是**缺陷造成的假便宜**（见「差异原因①」），修好长度缺陷后成本会上升，故 **¥25 是下界**。
**¥90 停批复盘门限未触发。**

> 口径：记账来源 = `LLMClient._record_usage`（构造时显式传 `UsageTracker("reports/usage-route-a.jsonl")`，
> Task 8/9 已修）；本表由 `scripts/route_a_cost.py` **读盘重算**（价格表与 `game_agent.usage` 同源，
> 不另写一份），4 条守卫在 `tests/test_route_a_cost.py`。

## 1. 实付（按用途标签归因）

| 组件 | 模型 | 用途标签 | 调用 | 入 token | 出 token | 实付 |
| --- | --- | --- | --- | --- | --- | --- |
| 其他·未分类（早期标签：演绎/质检混记） | deepseek-v4-flash | `aux` | 20 | 10,074 | 17,105 | ¥0.0913 |
| 拒绝采样选优 | deepseek-v4-flash | `rubric_select` | 54 | 99,875 | 1,350 | ¥0.0941 |
| 摘要生成（compress 侧信道，含拒绝采样候选） | deepseek-v4-flash | `compress` | 195 | 290,127 | 109,273 | ¥0.7109 |
| 演绎·compress | deepseek-v4-flash | `verbalize_compress` | 101 | 49,289 | 114,811 | ¥0.5846 |
| 演绎·extract | deepseek-v4-flash | `verbalize_extract` | 94 | 37,352 | 98,915 | ¥0.4978 |
| 演绎·judge | deepseek-v4-flash | `verbalize_judge` | 97 | 47,992 | 85,031 | ¥0.4502 |
| 质检员抽检 | deepseek-v4-flash | `rubric_quality` | 45 | 48,766 | 315 | ¥0.0746 |
| **合计** | — | — | **606** | **583,475** | **426,800** | **¥2.5035** |

单位成本 ≈ **¥0.01192/样本**（210 条实测）→ 外推 2,100 条 ≈ **¥25.04**

**为什么这张表分得开**：实跑第一次发现"演绎"调用全被记成 `purpose="aux"`（与质检、选优混成一堆），
分模块成本根本回填不出来 → Task 11 给各调用点加了用途标签（`verbalize_extract/judge/compress`、
`rubric_score/pairwise/select/quality`），并**用测试钉住"只改标签、不改路由"**
（`test_usage_purpose_labels_are_routing_neutral` + `test_factory_usage_purposes_carry_the_module`）。
上表第 1 行的 20 条 `aux` 正是**加标签之前**的探针批次，留档不掩。

## 2. 与 spec §10.2 估算并列（**折算到 2,100 条**，×10 外推，非实测）

| §10.2 组件 | 估算 | 实测折算 | 差 |
| --- | --- | --- | --- |
| extract 演绎 | ~¥10 | ¥4.98 | **−50%** |
| judge 演绎 | ~¥7 | ¥4.50 | **−36%** |
| compress 历史演绎 | ~¥15 | ¥5.85 | **−61%** |
| 拒绝采样（默认档）+ 摘要侧信道 | ~¥12 | ¥8.05（含摘要生成 ¥7.11 + 选优 ¥0.94） | **−33%** |
| 质检员抽检（20%） | ~¥1 | ¥0.75 | −25% |
| 评测集补全（judge 扩域 / G1·G2 等） | ~¥8 | **未做**（依赖 G1 包，见验收报告发现⑤相关） | — |
| rubric 评测轨道 | ~¥5 | ¥0.37（验收批仅 22 条；生产 30 对 × 6 次另算） | 规模不可比 |
| 重试与重演余量 | ~¥4 | ¥0（见差异原因③） | — |
| **合计（默认档）** | **~¥62** | **≈¥25** | **−60%** |

## 3. 差异原因（逐条给证据，不说"大概"）

1. **输出 token 远低于估算（主因）**：§10.2 假设 compress「长短混合均 8K 出」，
   实测 195 次摘要调用共出 **109,273 token ≈ 560/次**；演绎侧最长一次输出仅 **4,323 token**，
   而卡面 `target_tokens` 有的是 **20,000**。根因是验收发现④（演绎器不校验长度，
   `long_input` 标记取自卡面而非文本）——**这是缺陷，不是省钱**：长度缺陷一修，这一项必然上涨。
2. **缓存命中 34.6%**（命中 201,600 / 未命中 381,875）：§10.2 只把缓存当作"20~30% 的下行余量"，
   实测命中率略优于该假设。
3. **重试/重演余量没花掉**：演绎重演、`hard` 重生成、空/截断升级预算这三条在验收批里
   几乎没触发（失败走的是"丢弃"而非"重演"），但 compress 侧的**候选被杀**（超长）说明
   预算不是瓶颈——瓶颈是候选质量（见验收报告发现③）。
4. **探针批次**（前 20 条 `aux` + 6 张卡的 compress 诊断，¥0.14）已计入本表，
   它是"付费链路先探一次"的成本，不是浪费。

## 4. 未纳入本表的开销

- **轨道 2 评委**：`reports/usage-rubric.jsonl`，**22 次调用 ¥0.0372**（dev compress 22 条的探针掺入打分）。
- 后续整批重评（发现①要求"修评委提示词后整批重评"）会产生新开销。

## 5. 指纹（spec §6.4）

- `endpoint` = `deepseek-v4-flash` · `base_url` = `https://api.deepseek.com`
- `budget_policy` = `budgets.py@b7a187c940d6`
- `prompt_version`（演绎/评委提示词）见验收报告 §指纹
- `temperature`：演绎 0.9 / 摘要 0（生产同款）；`repeat` 不适用（单批）

## 6. 复现

```bash
# 跑批（三层；耗时约 50 分钟，实测 5.5s/调用）
python -m scripts.scenario_factory.assemble --layer train --extract 30 --judge 30 --compress 30 --sampling long
# 成本回填（离线）
python scripts/route_a_cost.py --samples 210 --target-samples 2100 --out reports/route-a-cost-20260912.md
```

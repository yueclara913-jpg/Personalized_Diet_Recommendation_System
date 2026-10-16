# 基于营养缺口的可解释推荐核心

## 范围与研究目标

本模块实现毕业论文中的确定性算法实验：在经过验证的一般成人档案、PR #11 每日目标、PR #12 完整日摄入和显式 `TEST_ONLY` 候选食品之间，比较能量基线与多目标营养缺口评分。它是内部 Python 服务，没有 HTTP 接口、用户认证、数据库写入或生产食品目录接入，不构成诊断、治疗或临床膳食处方。

当前没有完成独立核验和正式发布的 approved 食品目录。`evaluation` 模式只接受 `source_type=TEST_ONLY` 且 `candidate_status=TEST_ONLY_EVALUATION` 的人工夹具，并在每条结果标记 `test_only=true`。客户端写入 `approved`、USDA staging 或人工 `accept_candidate` 都不会获得生产信任。`production` 模式固定返回 `no_eligible_candidates`，直至后续建立服务端控制的正式目录与准入证明。

## 输入模型和完整性

`FoodCandidate` 是不可变类型，必须给出食品 ID、名称、TEST_ONLY 来源引用与版本、SHA-256、明确的 `per_100g` 基准、能量定义、四项每 100 g 数值、过敏/饮食标签，以及以克为单位的最小、最大和步进份量。缺字段、负值、NaN、Infinity、未知单位/基准、无效哈希及不合理份量均被硬排除；缺失值不会补零，也不执行密度、每份或可食部推断。

入口重新校验健康档案并调用 PR #11 的 `calculate_daily_targets()`，因此 `ManualReviewRequired` 会阻断推荐。输入日分析的 profile ID、目标状态、目标值和策略版本必须与档案重算结果一致；四项 intake 必须都是 `complete`，并且 supplied gap 必须与目标、摄入重新计算结果一致。`no_records` 不代表零摄入，`unknown` 和 `partial` 也不产生伪精确推荐，统一返回 `insufficient_intake_data`。这些校验提供算术完整性，不构成调用者身份认证；在普通用户认证和对象级授权完成前，本入口只能由可信内部代码调用。

## 饮食限制和硬过滤

档案原字段只是字符串列表，本模块只解释版本 `controlled_restrictions_v1` 的受控 token：

- `allergen:milk|egg|peanut|tree_nut|soy|wheat|fish|shellfish|sesame`
- `exclude:contains_pork|contains_alcohol`
- `require:vegetarian|vegan`

其他自由文本拒绝进入自动推荐。存在过敏限制而候选的 `allergen_tags` 未知，或存在排除/必需要求而 `dietary_tags` 未知时，采用 fail-closed。冲突候选返回机器可读代码 `ALLERGEN_CONFLICT`、`ALLERGEN_INFORMATION_UNKNOWN`、`DIETARY_RESTRICTION_CONFLICT`、`REQUIRED_DIETARY_TAG_MISSING` 或 `DIETARY_TAGS_UNKNOWN`，不在原因中复制用户敏感描述。baseline 和 proposed 共用同一层过滤。

## 份量搜索

份量序列为：

`minimum_portion_g + n × portion_step_g <= maximum_portion_g`

全程使用 `Decimal`，每份营养贡献为 `per_100g × portion_g / 100`，显示值采用 `ROUND_HALF_UP` 到 0.01。策略默认最多枚举 100 个份量，超过即排除候选。每种食品只保留评分最高的一个份量，然后进行食品间 Top-N 排序；分数相同时按 `food_id`，同一食品份量并列时按较小克数，保证复现。

## proposed 多目标评分

对营养项 `i`，目标为 `T_i > 0`，当前摄入为 `C_i`，候选贡献为 `A_i`：

```text
gap_improvement_i = |T_i-C_i|/T_i - |T_i-(C_i+A_i)|/T_i
incremental_overshoot_i = (max(C_i+A_i-T_i,0)-max(C_i-T_i,0))/T_i
score = Σ(w_i × gap_improvement_i)
      - Σ(p_i × incremental_overshoot_i)
      - 0.02 × portion_fraction
```

默认改进权重：能量 0.25、蛋白质 0.35、脂肪 0.20、碳水 0.20。默认超额惩罚权重：能量 0.35、蛋白质 0.20、脂肪 0.25、碳水 0.20。两组权重必须非负且分别精确合计 1；份量惩罚非负。目标来自 PR #11 且大于零，所有数值必须有限。分数按 `ROUND_HALF_UP` 固定到 6 位。负 improvement 会降低分数，因此改善一项不能自动掩盖其他项恶化；新增超额另行惩罚。权重是 `nutrition_gap_recommender_v1` 工程实验参数，尚未获得营养或临床验证。

## baseline 与实验复现

`algorithm=baseline` 使用 `energy_gap_baseline_v1`，只对能量的归一化距离改善和新增能量超额计分，同时保留相同档案/摄入完整性、候选集合、硬过滤、份量枚举和份量惩罚。调用相同输入两次会得到字节语义一致的 Python 结果，可用于记录测试画像、目标、摄入、候选、排序、分项偏差、超额和覆盖率。仓库没有宣称推荐准确率；当前可复现实验证据仅来自 `tests/test_nutrition_recommender.py` 的 TEST_ONLY 夹具。

## 可解释输出

结果包含算法和策略版本、模式、推荐状态、每种食品的最佳克数、四项预计贡献及单位、总分、每项归一化改进和超额惩罚、份量惩罚、来源引用/版本/哈希/基准/能量定义、规则生成的中文说明，以及候选排除代码。说明只陈述计算出的贡献、改善和超额，不生成“适合某疾病”“降低血压”等医学结论。

## 已知限制与后续工作

- 现有 `food_data.csv` 计量基准和来源未核实，不接入本模块；USDA staging、人工审核候选也不是正式目录。
- 日分析一致性校验不能替代用户身份认证或证明记录来自授权用户；正式接口需要服务端从受保护仓储重新计算。
- 受控过敏/禁忌映射覆盖范围有限，需要专业人员和产品规则审批；标签未知时会降低候选覆盖率。
- 当前仅比较能量和三种宏量营养素，不包含微量营养素、组合膳食、餐次、价格、口味或疾病需求。
- 权重、阈值、份量范围和评价指标仍需营养专业人员确认，并用正式批准食品及真实、合规的匿名数据开展实验。
- 下一阶段应建立服务端控制的正式食品发布链，连接 `food_id` 与可验证目录版本，再实现有认证和对象级授权的个人推荐 API。

论文算法设计章节可按“输入可信边界—硬约束—有限份量搜索—归一化多目标目标函数—稳定排序—规则解释—能量基线实验”组织；实验章节只能引用实际运行产生的测试或数据结果。

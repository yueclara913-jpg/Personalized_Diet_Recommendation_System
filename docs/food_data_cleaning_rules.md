# 食品营养数据审计规则 v1.0

本版只审计，不清洗、不换算、不补齐营养值，也不影响 Flask 推荐候选。

## 运行

在项目根目录执行（仅使用 Python 标准库）：

```bash
python -B scripts/audit_food_data.py --input food_data.csv --output-dir reports/food_quality/v1
python -B -m unittest discover -s tests -p 'test_food_data_audit.py' -v
node --test tests/frontend_events.test.cjs
```

输出目录内同名报告已存在时拒绝覆盖；再次审计应选择新版本目录。
原始CSV按字节读取，报告包含输入SHA-256和输出前后哈希核查。
输入必须为UTF-8（支持BOM）。CSV行号为记录起始物理行，支持带换行的引号字段。

## 规则与解释

- A类：首尾空格、完全重复记录可确定，但本版只报告；重名可能对应不同规格，禁止自动合并。
- B类：所有记录缺少来源、份量、单位、碳水和糖定义，标记needs_review。
- C类：较大糖碳水冲突、脂肪供能和强能量筛查属于高风险疑点，仍标记needs_review，不能视作已证实数值错误。
- 数值解析使用Decimal；缺失、非有限值、非法布尔、表头和列数异常单独报告。
- 负值保留并待核实。零热量不计算相对偏差，另设规则。
- Sugar > Carbohydrates只在同基准、同单位、定义匹配时有意义。差值<=0.2标medium，其余high；0.2是审计参数，不是营养标准。
- Fibre > Carbohydrates只有总碳水包含纤维时才构成疑点，默认medium待核实。
- 9×Fats > Calories假设kcal、g且相同份量，仅作条件性筛查。
- E0=4×Protein+4×Carbohydrates+9×Fats。偏差严格超过20%标medium。
- 宽松范围[E0-4×Fibre,E0+2×Fibre]，再加max(50,20%×Calories)容差；超出标high。它覆盖部分纤维定义差异，但不能覆盖所有特殊能量体系。
- 不按E0重写Calories。绝对极值因计量基准未知不能可靠判定；饱和脂肪因字段缺失不可检查。
- 风险字段描述审核优先级，审核状态独立描述是否核实；不直接作为当前API过滤条件。

## 输出与可追溯性

summary.json记录字段、记录数、缺失比例、规则命中数、数值疑点记录数、规则版本和哈希。
anomalies.csv每个问题一行；包含原始行号、名称、原值JSON、证据前提、风险、状态、建议和来源哈希。
food_metadata.csv独立保存审核元数据。record_id使用“原始文件SHA-256:起始行号”，适用于不可变原始版本；后续版本以显式映射关联。

原始13列从未写入。原值为CSV解析后的单元格字符串；原始字节仍以原文件及哈希追溯。
所有审核元数据默认未知或needs_review，不把文件更新时间当成食品数据更新时间。
下一版需经确认后才可增加人工审核、隔离和有证据的数值修正；本版没有生成可推荐的审核通过集。

## OCR兼容预留

独立元数据预留data_source、source_version、source_updated_at、serving_quantity、serving_unit、nutrition_basis、energy_unit、nutrient_units、carbohydrate_definition、sugar_definition、reviewer、reviewed_at、evidence_reference。
OCR字段包括ocr_document_id、ocr_page、ocr_region、ocr_engine、ocr_engine_version、ocr_confidence、ocr_raw_text；import_method当前为legacy_csv。
未来OCR应另存原图/文档哈希和提取结果，将文本识别与营养核实分开；识别置信度不能视作营养可信度。单位未知时不得默认每100g，用户确认后再进入审核数据集。
本版未引入OCR依赖、数据库或外部营养来源。

## 验收补充约束

- 固定Decimal精度512；最多100位有效数字，非零数数量级限于±100。超出标NUMERIC_PRECISION_LIMIT并跳过相关计算，属于计算范围限制，不判营养数值错误。
- 严格解析CSV；编码错误或未闭合引号停止审计。零热量独立检查，即使其他字段异常也不会遗漏。
- 所有报告独占创建，已有文件、软链接、硬链接和创建时竞争不能触发覆盖写入。执行失败可能留下部分报告，需选择新输出目录重跑；不宣称整个目录事务性写入。
- summary仅记录source_name和来源哈希，不保存机器绝对路径；空数据集也保持完整元数据表头。
- 相同输入重复审计的三个报告逐字节一致；报告没有时间戳或随机标识。

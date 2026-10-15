# 食品营养审核与准入规则 v1

## 作用与边界

本模块从 PR #6 的专用 SQLite 暂存库只读获取完整解析记录，输出可解释的规则建议。三种决策是：

- `ready_for_manual_review`：基础规则通过，可以交给管理员人工审核；不等于 `approved`。
- `needs_review`：记录可追溯且结构可读，但存在必须人工核实的事项。
- `rejected_by_policy`：身份、结构、来源元数据或数值明确违反当前准入规则，应修复并重新解析。

所有结果的 `review_status` 固定为 `pending`。模块没有审批写入、正式数据库导入、推荐接口或管理员界面；不生成审核人、审核时间和官方验证事实。USDA 真实快照仍未取得和验证，全部自动化测试均为 `TEST_ONLY` 人工夹具。

## 兼容性结论

暂存 `staging_records.record_json` 完整保留原始记录、解析营养素、单位、LOQ、基准、转换依据、能量及糖的不同定义，足以通过独立适配层审核。暂存表 CHECK 仅允许 `pending`，本模块不修改该约束。

正式 `nutrition/schema.sql` 可表达来源版本、FDC ID、单一标准营养值和审核状态，但 `(profile_id,nutrient_code)` 不能无损保存三个能量定义和两个糖定义；同时缺少完整原始记录、双文件哈希及食品干物质状态。本次保持 schema 不变。未来正式准入前，仍需用户批准候选营养项扩展或能量/糖选择策略，以及干物质、份量和缺失值准入规则。

## 默认策略与 33 条规则

默认策略 `nutrition-review-v1` 保守要求 `calories`、`protein`、`fat`、`carbohydrates`，接受 `per_100g` 和 `Foundation`。来源真实性标志及证据引用完全由外部调用方声明；程序只检查声明字段是否齐全并与 URL、版本、原始文件 SHA-256 一致，不会联网、验签或独立认证 USDA 来源。只有哈希不算官方真实性验证。

规则分为五类：来源与治理 9 条、结构与完整性 7 条、计量基准 4 条、营养值与转换 11 条、定义冲突 2 条。规则编号稳定地写入每条 issue；同一记录按编号、位置和营养素 ID 排序，结果包含审核记录 SHA-256，重复执行输出一致。

> 分类存在治理与完整性含义交叉；唯一规则编号总数为 33：SRC001—SRC007、GOV001、INT001、STR001—STR007、BAS001—BAS004、NUT001—NUT011、DEF001—DEF002。

硬性拒绝包括非法 FDC/营养素 ID、重复 ID、非法食品名称或结构、来源 URL/版本/哈希/定位非法、暂存状态不是 pending、非法或超技术范围数值、未知转换状态，以及原始营养素与解析营养素无法逐项追溯。

食品身份按 `(release_key, fdc_id)` 判重。同一发布版本的相同 FDC ID 由 PR #6 暂存表复合主键直接阻止，审核层仍保留 `INT001` 作为防御性规则；不同发布版本中的相同 FDC ID 是合法版本共存，不触发重复拒绝。

默认人工复核包括：官方真实性证据未独立匹配、非每 100 g 或无基准依据、可能的干物质或份量换算、缺失值、未知单位、已知转换失败、LOQ 限定值、缺少策略必需营养素，以及多个能量或糖定义。真实零值不会被当作缺失；任何缺失项都不会填零或估算。

能量 `1008 / 2047 / 2048` 与糖 `1063 / 2000` 分开保留。多定义只产生提示，规则引擎不选择、不相加，不用 4-4-9 覆盖能量，也不把两种糖写成同一标准值。

## 策略配置

`ReviewPolicy` 支持以下字段，结果记录实际 `version`：

- `required_nutrients`：八个标准概念的子集：calories、protein、fat、carbohydrates、fiber、sugar、sodium、iron。
- `accepted_bases`：per_100g、per_100ml、per_serving、unknown 的非空子集。
- `allowed_data_types`：允许的来源数据类型。
- `missing_level`、`unknown_unit_level`、`multiple_energy_level`、`multiple_sugar_level`：`ignore`、`needs_review` 或 `rejected_by_policy`。
- `require_verified_official_file`：是否要求匹配的独立官方文件证据。

配置 JSON 只能包含上述字段及 `version`。放宽策略只改变审核建议，仍不能生成 approved 或写入推荐库。本模块不预设任何能量或糖优先级。

## 只读命令行

```sh
python scripts/review_usda_staging.py \
  --database /outside/review-staging.sqlite \
  --output-dir /outside/review-report-v1
```

`--policy` 可指定策略 JSON。`--provenance-evidence` 可指定以 `release_key` 为键的证据对象；每项可包含 `official_file_verified`、`verified_source_file_sha256`、`verified_source_version`、`verified_source_url` 和 `verification_reference`。这些字段只是调用方提供的外部证据声明，不由工具生成，也不表示程序已经独立认证 USDA 来源；缺省时真实性未核验。

数据库必须是常规非链接文件。工具用 SQLite URI `mode=ro` 和 `query_only=ON` 打开，验证 PR #6 专用 `application_id`、`user_version`、完整 schema、外键和完整性；正式业务库、额外/缺失表及记录摘要不一致均拒绝。数据库字节不应变化。

输出目录必须不存在且父目录存在。成功独占创建 `review.json` 和中文 `summary.txt`，既有路径拒绝覆盖；写出失败清理本次目录。JSON 报告包含策略版本、三种决策统计、逐食品结果及问题明细。空暂存库产生零记录报告，不伪造食品。失败返回退出码 2 和结构化 stderr，不生成成功报告。

## 后续前置条件

在任何正式准入前，需要真实官方快照及独立来源证据、真实数据全量审核、人工审批身份与审计记录、正式 schema 的多定义/文件清单迁移设计，以及能量、糖、干物质、份量和缺失数据策略的用户批准。本模块只产生人工审核队列的候选建议。

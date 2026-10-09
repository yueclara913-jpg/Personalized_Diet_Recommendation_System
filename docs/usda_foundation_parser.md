# USDA Foundation JSON 解析器 v1

## 来源与真实数据状态

官方页面核查日期：2026-10-10（Asia/Shanghai）。下载页列出 Foundation Foods 2026 年 4 月 JSON（压缩 459K、解压 6.5M）和 CSV 快照。精确发布日期未核实，压缩包直链已从官方页面确认，不根据第三方文件名推断日期。下载清单记录月份版本 2026-04，published_at、downloaded_at 和 source_file_sha256 均为空；download_url 为官方页面确认的地址。

执行环境访问官方站点返回 `CONNECT tunnel failed, response 403`，未下载真实数据，未进行真实数据冒烟测试。测试数据全部由 tests/test_usda_foundation.py 人工构造并标记 TEST_ONLY，不代表 USDA 真实食品。

官方依据：
- 下载及版本：https://fdc.nal.usda.gov/download-datasets/
- 数据类型：https://fdc.nal.usda.gov/data-documentation/
- 公共领域、CC0 1.0：https://fdc.nal.usda.gov/api-guide/
- Foundation 能量、碳水及基准：https://fdc.nal.usda.gov/Foundation_Foods_Documentation/
- FDC 营养素 ID 对照（Appendix K）：https://www.ars.usda.gov/ARSUserFiles/80400530/pdf/fndds/2021_2023_FNDDS_Doc.pdf

最后一项仅用于 USDA 通用营养素 ID 对照，本模块不读取 FNDDS 食品。名称不用于模糊映射。

## 使用及边界

仅支持解压后的官方 Foundation JSON：顶层对象包含 FoundationFoods 数组，食品 nutrient 使用嵌套 id/name/unitName 和 amount。原始 JSON 最大 32 MiB；不支持 CSV、压缩包直接解析或实时 API。需指定来源版本和地址；这些元数据是调用方声明，尚不能自动验证文件确属该版本。

```sh
PYTHONDONTWRITEBYTECODE=1 python -m nutrition.sources.usda_foundation /tmp/foundation.json \
  --source-version 2026-04 \
  --source-url https://fdc.nal.usda.gov/download-datasets/ \
  --basis per_100g \
  --basis-evidence https://fdc.nal.usda.gov/Foundation_Foods_Documentation/
```

命令将解析结果输出到 stdout，不写 SQLite。默认基准 unknown，未知、每份和每 100 mL 均不转换为每 100 g。Foundation 官方文档支持每 100 g 的表达，但农业记录可能为干物质基准；原始食品描述及完整记录保留供核实，不能把干重数据当作熟食营养。

能量和糖默认不选择，可显式使用 --energy-id 2047 或 2048、--sugar-id 2000 或 1063。这只是指定语义，不是人工审核，不表示该值一定存在。选择的字段无效、缺失或重复时仍为 NULL。

## 映射

| FDC ID | 独立语义 | 目标单位 |
|---|---|---|
| 1003 | protein | g |
| 1004 | fat | g |
| 1005 | carbohydrates_by_difference（包括纤维） | g |
| 1079 | fiber | g |
| 1089 | iron | mg |
| 1093 | sodium | mg |
| 2000 | sugar_total_nlea | g |
| 1063 | sugar_total | g |
| 1008 | energy_legacy | kcal |
| 2047 | energy_atwater_general | kcal |
| 2048 | energy_atwater_specific | kcal |

保留 nutrient 名称、原始单位、原始 amount、来源营养素 ID 及原始记录，不根据名称猜测未知 ID。未映射 ID 标记 unknown_nutrient_id；未知单位、无效数值、非有限值、重复 nutrient、LOQ 标记值均待核实，不用零替代。ID 不是历史 nutrient number。

重复 food ID 全部保留且标记，不按名称合并，也不保留最后一条覆盖前值。原始 JSON 对象键重复会拒绝读取。解析结果记录文件 SHA-256、数组位置、来源版本、映射和解析版本。小数以 Decimal 读取并保存十进制文本；原始字节和哈希才是数字词法及完整文件的最终证据。所有食品 review_status 为 pending；无素食、餐次或过敏标签推断。

## 换算

支持 identity、kJ / 4.184 → kcal、kcal × 4.184 → kJ、g × 1000 → mg、mg / 1000 → g。只在明确每 100 g 且提供依据时转换，不实施分母转换。仅接受显式列出的单位拼写；未知单位不猜测。

Decimal 使用独立上下文，精度 110、ROUND_HALF_EVEN，输出 6 位小数。超过 100 位有效数字、负值、超过 1e12 的输入/结果标记待审核；正值舍入为零时标记 below_output_precision，不报告真实零。1e12 为技术防护界限，不是食品营养合理性标准。后续仍需营养质量审计。原值、单位、规则和版本均保存，不使用 4-4-9 重写能量。

## 下载工具

scripts/fetch_usda_foundation.py 接受已经核实的官方 HTTPS ZIP 直链与仓库外的新文件路径，保留平台代理和 TLS 验证，不需要密钥、不重试、不覆盖、不解压。限制下载 10 MiB，验证返回为 ZIP、全部成员 CRC，限制总解压体积 64 MiB，失败删除本次创建的不完整文件。重定向仍须满足同一官方主机限制；若官方未来使用其他 CDN，先核实再修改规则，不自动放行。

本次未实际运行下载工具取得食品包；测试用内存生成的 TEST_ONLY ZIP 验证成功、拒绝覆盖及失败清理。下载成功返回时间、大小、SHA-256，应在后续批准获取后更新清单并额外记录解压 JSON 的 SHA-256。现在清单不伪造成功记录。

## SQLite 兼容性及待解决问题

来源、版本、FDC ID、原始定位、单位、转换依据可以对应现有表。以下尚不能无损导入：

1. food_nutrients 每 profile/营养素仅一条，不能同时保存不同能量/糖定义；需批准选择策略或未来扩展来源候选营养素表。
2. food_profiles 没有明确干物质/可食部状态及逐营养素推导方法字段；完整原始记录目前仅在解析输出保留。
3. source_releases 只支持单文件哈希，包与解压文件的双重追溯需要未来文件清单结构。
4. foodPortions 当前保留在 raw_record，尚未逐项标准化，也不生成 serving_weight_g。
5. source_data_type 输出为官方 Foundation，不自动转换为自定义类别；中文名及应用标签不生成。

本版输出是暂存解析结构，不声称已可直接写入所有 SQLite 表。未知 ID、LOQ、缺测、干物质和定义差异需后续导入校验。真实快照未取得，需核对实际 JSON 结构后再宣称真实数据兼容。

复现：在仓库根目录使用标准库运行 python -m unittest discover -s tests -p test_usda_foundation.py；不需要安装任何新依赖，不联网。原始 CSV、Flask、已合并 schema、初始化工具和审计报告保持不变。

## 最终验收补充

官方 OpenAPI 可核对 FoundationFoodItem、FoodNutrient、Nutrient 和 FoodPortion 的字段；这不能替代 2026-04 压缩包实测。当前 FoundationFoods 顶层封装仍未从真实快照独立核实，因此称为目标快照格式，不宣称已通过官方快照兼容验收。测试明确拒绝 API 单条食品对象和 API 裸数组。foodPortions 仅保留原始结构，不标准化；非法数组结构标记待审核。

已修复 ZIP 仅检查文件头而不校验 CRC、Decimal 继承调用方 traps、非对象食品记录缺少来源字段及超长字符串 ID 转换崩溃问题。未改变已有数据库结构。

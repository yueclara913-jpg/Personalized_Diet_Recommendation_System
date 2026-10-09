# 食品营养标准化结构 v1

## 范围与初始化

本结构独立于 `food_data.csv`、Flask API 和现有推荐算法。原始 99 条记录仍为未知计量基准的原型数据，不迁移、不补零、不换算。首批计划使用 USDA Foundation Foods；本次不下载、不导入真实数据。

仅使用 Python 标准库 sqlite3。调用：

```sh
python scripts/init_nutrition_db.py --database /tmp/nutrition-demo.sqlite
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_nutrition_schema.py' -v
```

输出路径必须显式指定，建议位于 Git 工作树之外。父目录必须存在；已存在的非 SQLite 文件（包括空文件）会被拒绝。初始化设置连接级 `PRAGMA foreign_keys=ON`，以事务执行建表和定义插入，失败回滚；不删除已有表或记录。返回连接由调用方关闭。新增 `connect_database()` 统一开启并验证外键，初始化及本次测试的每个连接均通过该函数创建。每个未来调用方也必须使用它或自行开启外键；SQLite 外键不是永久数据库配置。

`user_version=1` 表示本版本，其他非零版本拒绝处理。重复初始化保留数据；它不是迁移工具。初始化会核对已有表、视图和索引的结构；不相关、残缺或被修改的结构、冲突的标准营养素定义及外键违规会被拒绝，不自动修复。空数据库可初始化，完整结构可重复初始化。SQL 可独立执行，使用 IF NOT EXISTS；独立执行时调用者负责事务和版本标记，不在已有事务内才开启外键。

## 身份、来源与单位

11 张表均按计划实现，没有新增用户、推荐、数据库导入或 OCR 识别功能。食品 ID 为调用方生成的稳定内部字符串（推荐 UUID），名称不是唯一键；来源食品身份由 `(release_id, source_data_type, source_food_id)` 唯一确定。来源版本保留发布、获取时间及文件哈希。USDA FDC ID 存入 `source_food_id`，数据类型存入 `source_data_type`。不能按名称自动合并不同来源、生熟状态或版本。

同一 profile 的所有营养值具有同一计量基准：

| nutrition_basis | basis_quantity | basis_unit | 含义 |
|---|---:|---|---|
| per_100g | 100 | g | 每 100 g，食品状态和可食部须从来源核实 |
| per_100ml | 100 | ml | 每 100 mL，不能默认等于 100 g |
| per_serving | 1 | serving | 每一来源定义的份量，具体份量保存在 food_portions |
| unknown | NULL | NULL | 未知，不进入已审核候选视图 |

SQL 显式处理 NULL，避免 CHECK 的三值逻辑放过不完整基准。不同基准保存为不同 profile，不建立跨基准求和视图。每份必须在后续准入校验中核实份量；体积转质量必须有可靠密度，本版不存储或假设密度。未来若需为同一来源 ID 保存转换后的 profile，应先增加转换版本模型，不能绕过现有唯一约束。

## 营养素字典

下列是定义，不是实际食品数值，初始化只插入这 8 项定义。

| nutrient_code | 旧字段映射（计划） | 标准单位 | 含义 |
|---|---|---|---|
| calories | Calories | kcal | 来源能量，保留 energy_method |
| protein | Protein | g | 蛋白质，保留来源定义 |
| fat | Fats | g | 总脂肪 |
| carbohydrates | Carbohydrates | g | 碳水化合物，记录是否含纤维等定义 |
| fiber | Fibre | g | 膳食纤维，记录来源定义 |
| sugar | Sugar | g | 总糖，不等于添加糖 |
| sodium | Sodium | mg | 钠，不等于食盐 |
| iron | Iron | mg | 铁 |

`food_nutrients` 将数值与 profile、营养素及标准单位关联。原始文本、原单位、来源营养素 ID 和转换规则独立保存。数值没有默认 0；真实零存储为 0，缺失存储为 NULL。`trace`、`below_loq` 不应自动变成精确零。未采集字段可没有行，已知缺测可保存 missing 行；二者均不满足未来的完整性要求。

换算规则需记录，例如 identity、kJ_to_kcal_div_4.184；本版不实施换算。4-4-9 只筛查，不重写来源能量。有限数值范围 0..1e12 是数据库防护范围，不是营养合理性阈值；以后需要来源感知的质量校验。NaN 不作为数值输入，未来导入器必须先拒绝非有限数值，因为 Python sqlite3 可能将 NaN 转为 NULL。

## 审核与 OCR 隔离

审核状态仅为 pending、approved、rejected，默认 pending。状态表示人工处理结论，不表示营养测量绝对正确，也不表示许可允许公开。

OCR 保留图片来源、图片 SHA-256、原始文本、引擎版本、0..1 识别置信度及独立审核状态。置信度可空；高识别置信度不能自动升级审核状态，不能等同营养可信度。图片地址应为合规相对标识或公开来源，不存储密钥、个人信息或无必要的绝对机器路径。图片公开权须另行核实，不能继承 USDA 许可。

OCR profile 必须使用 `value_origin=ocr_label` 并链接同一 release 的 OCR 记录。其他来源不得带 OCR 链接。`reviewed_food_profiles` 仅包含 profile 已审核且基准已知的记录；OCR 还必须同时审核通过，撤销 OCR 审核会立即移出视图。

此视图是候选集合，不是正式推荐接口。还缺字段完整性、许可、食品状态、标签和份量准入。每份未定义重量的记录不能仅凭该视图用于精确营养计算。SQL 无法判断操作者是否把 OCR 内容谎报为 analysis，未来导入入口必须约束来源。review_events 保留历史，但本版不自动更新状态；未来审核服务应在同一事务写入事件和更新状态。

## 字段目录

以下逐列列出 SQLite 类型、空值、单位及字段约束。主键均显式 NOT NULL；所有外键默认阻止删除仍被引用的记录。时间为 ISO 8601 文本约定，SQL 不验证日期格式；网址、版本及证据的内容真实性由后续校验和人工审核确认。

### data_sources

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `source_id` | TEXT | 否 | — | 来源内部标识；`PRIMARY KEY NOT NULL CHECK(length(trim(source_id)) > 0)` |
| `source_name` | TEXT | 否 | — | 数据来源名称；`NOT NULL CHECK(length(trim(source_name)) > 0)` |
| `organization` | TEXT | 否 | — | 提供机构；`NOT NULL` |
| `official_url` | TEXT | 否 | — | 机构官方入口；`NOT NULL` |
| `license_id` | TEXT | 否 | — | 许可标识，不代表审核状态；`NOT NULL` |
| `license_url` | TEXT | 是 | — | 许可原文地址；`无列级附加约束` |
| `redistribution_status` | TEXT | 否 | — | 再分发条件，unknown 不授权公开；`NOT NULL DEFAULT 'unknown' CHECK(redistribution_status IN ('allowed','restricted','unknown'))` |

### source_releases

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `release_id` | TEXT | 否 | — | 来源发布批次标识；`PRIMARY KEY NOT NULL` |
| `source_id` | TEXT | 否 | — | 来源内部标识；`NOT NULL REFERENCES data_sources(source_id)` |
| `source_version` | TEXT | 否 | — | 来源版本；`NOT NULL` |
| `published_at` | TEXT | 是 | — | 来源发布时间；`无列级附加约束` |
| `retrieved_at` | TEXT | 否 | — | 获取时间；`NOT NULL` |
| `source_file_sha256` | TEXT | 否 | — | 原始来源文件哈希；OCR 批次可使用原始输入清单哈希；`NOT NULL CHECK(length(source_file_sha256)=64 AND source_file_sha256 NOT GLOB '*[^0-9a-f]*')` |
| `source_url` | TEXT | 否 | — | 该版本获取地址；`NOT NULL` |

### foods

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `food_id` | TEXT | 否 | — | 内部食品唯一 ID；`PRIMARY KEY NOT NULL CHECK(length(trim(food_id)) > 0)` |
| `food_name` | TEXT | 否 | — | 来源原名；`NOT NULL CHECK(length(trim(food_name)) > 0)` |
| `food_name_en` | TEXT | 是 | — | 英文名称；`无列级附加约束` |
| `food_name_zh` | TEXT | 是 | — | 中文名称，需人工核对；`无列级附加约束` |
| `category` | TEXT | 是 | — | 食品类别；`无列级附加约束` |
| `preparation_state` | TEXT | 是 | — | 生熟、干燥等加工状态；`无列级附加约束` |

### ocr_imports

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `ocr_import_id` | TEXT | 否 | — | OCR 输入记录标识；`PRIMARY KEY NOT NULL` |
| `release_id` | TEXT | 否 | — | 来源发布批次标识；`NOT NULL REFERENCES source_releases(release_id)` |
| `image_source` | TEXT | 否 | — | 图片来源引用，不保存图片二进制；`NOT NULL` |
| `image_sha256` | TEXT | 否 | — | 原始图片哈希；`NOT NULL CHECK(length(image_sha256)=64 AND image_sha256 NOT GLOB '*[^0-9a-f]*')` |
| `raw_text` | TEXT | 否 | — | OCR 原始文字；`NOT NULL` |
| `engine_name` | TEXT | 否 | — | OCR 引擎；`NOT NULL` |
| `engine_version` | TEXT | 否 | — | 引擎版本；`NOT NULL` |
| `recognition_confidence` | REAL | 是 | 0..1 | 识别置信度，不代表营养可信度；`CHECK(recognition_confidence IS NULL OR (typeof(recognition_confidence) IN ('integer','real') AND recognition_confidence BETWEEN 0 AND 1))` |
| `review_status` | TEXT | 否 | — | 人工审核结论，与许可及识别置信度独立；`NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected'))` |
| `imported_at` | TEXT | 否 | — | OCR 输入时间；`NOT NULL` |

### food_profiles

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `profile_id` | TEXT | 否 | — | 食品营养版本标识；`PRIMARY KEY NOT NULL` |
| `food_id` | TEXT | 否 | — | 内部食品唯一 ID；`NOT NULL REFERENCES foods(food_id)` |
| `release_id` | TEXT | 否 | — | 来源发布批次标识；`NOT NULL REFERENCES source_releases(release_id)` |
| `source_food_id` | TEXT | 否 | — | USDA FDC ID 或其他来源原始 ID；`NOT NULL CHECK(length(trim(source_food_id)) > 0)` |
| `source_data_type` | TEXT | 否 | — | Foundation、SR Legacy 或其他来源类型；`NOT NULL` |
| `source_locator` | TEXT | 否 | — | 原始行号、JSON 路径或字段位置；`NOT NULL` |
| `nutrition_basis` | TEXT | 否 | — | 整个 profile 的营养分母类别；`NOT NULL CHECK(nutrition_basis IN ('per_100g','per_100ml','per_serving','unknown'))` |
| `basis_quantity` | REAL | 是 | 由 basis_unit 确定 | 分母数值；`无列级附加约束` |
| `basis_unit` | TEXT | 是 | g/ml/serving | 分母单位；`无列级附加约束` |
| `review_status` | TEXT | 否 | — | 人工审核结论，与许可及识别置信度独立；`NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected'))` |
| `value_origin` | TEXT | 否 | — | 分析、估算、标签或 OCR 等来源性质；`NOT NULL CHECK(value_origin IN ('analysis','estimated','label','ocr_label','unknown'))` |
| `ocr_import_id` | TEXT | 是 | — | OCR 输入记录标识；`无列级附加约束` |
| `energy_method` | TEXT | 是 | — | 来源能量计算体系；`无列级附加约束` |
| `carbohydrate_definition` | TEXT | 是 | — | 碳水定义及是否包含纤维；`无列级附加约束` |
| `fiber_definition` | TEXT | 是 | — | 纤维定义或测定方法；`无列级附加约束` |
| `mapping_version` | TEXT | 否 | — | 标准化映射规则版本；`NOT NULL` |

### nutrient_definitions

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `nutrient_code` | TEXT | 否 | — | 标准营养素代码；`PRIMARY KEY NOT NULL` |
| `nutrient_name` | TEXT | 否 | — | 营养素名称；`NOT NULL` |
| `unit` | TEXT | 否 | kcal/g/mg | 标准营养单位；`NOT NULL CHECK(unit IN ('kcal','g','mg'))` |
| `definition` | TEXT | 否 | — | 营养素定义；`NOT NULL` |

### food_nutrients

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `profile_id` | TEXT | 否 | — | 食品营养版本标识；`NOT NULL REFERENCES food_profiles(profile_id)` |
| `nutrient_code` | TEXT | 否 | — | 标准营养素代码；`NOT NULL` |
| `unit` | TEXT | 否 | kcal/g/mg | 标准营养单位；`NOT NULL` |
| `standardized_value` | REAL | 是 | 由 unit 确定 | 标准单位下的营养值；NULL 与 0 不同；`CHECK(standardized_value IS NULL OR (typeof(standardized_value) IN ('integer','real') AND standardized_value BETWEEN 0 AND 1e12))` |
| `raw_value` | TEXT | 是 | 由 raw_unit 确定 | 保留原始文本数值和限定表达；`无列级附加约束` |
| `raw_unit` | TEXT | 是 | 来源单位 | 来源原始单位；`无列级附加约束` |
| `source_nutrient_id` | TEXT | 是 | — | 来源营养素 ID；`无列级附加约束` |
| `value_qualifier` | TEXT | 否 | — | 测量、估算、标签、痕量、低于定量限或缺失；`NOT NULL CHECK(value_qualifier IN ('measured','estimated','label','trace','below_loq','missing'))` |
| `conversion_rule` | TEXT | 是 | — | 原值到标准值的转换规则，未转换为 identity；`无列级附加约束` |

### food_portions

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `portion_id` | TEXT | 否 | — | 份量标识；`PRIMARY KEY NOT NULL` |
| `profile_id` | TEXT | 否 | — | 食品营养版本标识；`NOT NULL REFERENCES food_profiles(profile_id)` |
| `portion_name` | TEXT | 否 | — | 来源份量名称；`NOT NULL` |
| `serving_quantity` | REAL | 否 | 由 serving_unit 确定 | 份量数量；`NOT NULL CHECK(typeof(serving_quantity) IN ('integer','real') AND serving_quantity > 0 AND serving_quantity <= 1e12)` |
| `serving_unit` | TEXT | 否 | 来源份量单位 | g、ml、个、杯等来源单位；`NOT NULL CHECK(length(trim(serving_unit)) > 0)` |
| `serving_weight_g` | REAL | 是 | g | 有证据的份量质量，未知不得补齐；`CHECK(serving_weight_g IS NULL OR (typeof(serving_weight_g) IN ('integer','real') AND serving_weight_g > 0 AND serving_weight_g <= 1e12))` |
| `source_locator` | TEXT | 否 | — | 原始行号、JSON 路径或字段位置；`NOT NULL` |
| `review_status` | TEXT | 否 | — | 人工审核结论，与许可及识别置信度独立；`NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected'))` |

### food_aliases

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `alias_id` | TEXT | 否 | — | 别名标识；`PRIMARY KEY NOT NULL` |
| `food_id` | TEXT | 否 | — | 内部食品唯一 ID；`NOT NULL REFERENCES foods(food_id)` |
| `alias` | TEXT | 否 | — | 食品别名，不做跨食品唯一约束；`NOT NULL CHECK(length(trim(alias)) > 0)` |
| `language` | TEXT | 否 | — | 语言标识，例如 zh、en；`NOT NULL` |
| `evidence` | TEXT | 否 | — | 审核或标注依据；`NOT NULL` |
| `review_status` | TEXT | 否 | — | 人工审核结论，与许可及识别置信度独立；`NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected'))` |

### food_application_tags

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `tag_id` | TEXT | 否 | — | 应用标签标识；`PRIMARY KEY NOT NULL` |
| `food_id` | TEXT | 否 | — | 内部食品唯一 ID；`NOT NULL REFERENCES foods(food_id)` |
| `tag_name` | TEXT | 否 | — | 餐次、素食或纯素类别；`NOT NULL CHECK(tag_name IN ('meal_type','vegetarian','vegan'))` |
| `tag_value` | TEXT | 否 | — | 标签值，unknown 不等于 false；`NOT NULL` |
| `evidence` | TEXT | 否 | — | 审核或标注依据；`NOT NULL` |
| `review_status` | TEXT | 否 | — | 人工审核结论，与许可及识别置信度独立；`NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected'))` |

### review_events

| 字段 | 类型 | 可空 | 单位 | 含义与列约束 |
|---|---|---|---|---|
| `review_event_id` | TEXT | 否 | — | 审核事件标识；`PRIMARY KEY NOT NULL` |
| `profile_id` | TEXT | 是 | — | 食品营养版本标识；`REFERENCES food_profiles(profile_id)` |
| `ocr_import_id` | TEXT | 是 | — | OCR 输入记录标识；`REFERENCES ocr_imports(ocr_import_id)` |
| `review_status` | TEXT | 否 | — | 人工审核结论，与许可及识别置信度独立；`NOT NULL CHECK(review_status IN ('pending','approved','rejected'))` |
| `reviewer` | TEXT | 否 | — | 审核者内部标识，避免不必要个人信息；`NOT NULL` |
| `reviewed_at` | TEXT | 否 | — | 审核时间；`NOT NULL` |
| `evidence` | TEXT | 否 | — | 审核或标注依据；`NOT NULL` |

## 表级约束与后续兼容

- source_releases 的来源、版本和哈希组合唯一；food_profiles 的 release、数据类型及来源食品 ID 唯一。
- food_nutrients 的 profile 和营养素组合唯一，并使用营养素代码与单位的复合外键。非空标准值必须保留非空白的原值、原单位和转换规则；missing 必须为 NULL。
- food_profiles 的 OCR 引用使用复合外键，保证与 OCR 原始记录属于同一 release。
- food_aliases 仅对同一食品、别名、语言去重；food_application_tags 对食品、标签和值去重。标签冲突仍需后续业务校验。
- review_events 必须且只能指向一个 profile 或 OCR 记录；份量和别名的审核历史暂不在该表中，后续按需要扩展。
- 食品、份量、别名和标签的审核、单位等列约束以 schema.sql 为可执行依据。

未来适配器应显式输出旧 13 列，不能把元数据直接添加到现有 DataFrame；当前代码的 to_dict 会把新增列传出。Meal_Type、Vegetarian、Vegan 来自独立审核标签，不从营养值猜测。正式推荐必须核实实际份量、选择一种 profile 基准并换算，再累加；旧数据继续仅用于演示。

所有测试食品与营养值均为 TEST_ONLY 人工夹具，临时数据库在 TemporaryDirectory 中创建并删除。本次不存储真实食品数值、不新增运行数据库文件、不改变现有依赖。

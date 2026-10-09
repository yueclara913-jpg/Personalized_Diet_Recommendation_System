# USDA Foundation 快照离线验证 v1

## 状态和证据边界

开发基线 ef3ed03ee53d219986d9afa7edfc6a83e42743c1，独立分支 feature/usda-real-data-validation。官方页面仍列出 Foundation 2026-04 JSON；官方直链为 https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_json_2026-04-30.zip 。许可 CC0，依据 https://fdc.nal.usda.gov/api-guide/ 。官方字段/营养定义参考 https://fdc.nal.usda.gov/data-documentation/ 和 https://fdc.nal.usda.gov/Foundation_Foods_Documentation/ 。

2026-10-10（Asia/Shanghai）正常官方访问返回 CONNECT tunnel failed, response 403，停止获取，没有镜像或绕过。没有真实快照、真实食品数量或真实文件哈希；原 manifest 不改写为已下载。测试全部为 TEST_ONLY 人工夹具，不能与真实数据验证混淆。

本工具不联网、不要求 API Key、不写 SQLite、不修改任何食品数值或现有数据来源。复用现有解析器与营养素映射，不更改已合并解析器。固定快照实际顶层结构仍待官方文件验证；结构不匹配时直接报错，不自动猜测另一格式。

## Windows 用法

在 Windows 从官方页面手动下载指定 ZIP，保留官方来源地址。将 GitHub 分支代码下载或克隆至电脑；在项目根目录运行（需 Python 3）：

```powershell
python scripts/validate_usda_snapshot.py "C:\USDA\FoodData_Central_foundation_food_json_2026-04-30.zip" --source-version 2026-04 --output-dir "C:\USDA\validation-2026-04-run1"
```

输出目录必须不存在，父目录必须存在。可直接传入解压后的 .json，程序同样校验顶层 FoundationFoods 数组。不支持 CSV 或多个 JSON 的 ZIP；有多个 JSON 时拒绝选择，用户需明确提供目标 JSON。

默认 nutrition_basis 为 unknown。可以在核实来源与类型之后增加：

```powershell
--basis per_100g --basis-evidence https://fdc.nal.usda.gov/Foundation_Foods_Documentation/
```

这只声明分母依据，不能自动证明每种食品是可食状态。Foundation 可能包含 0% 水分干物质记录，不通过食品名称猜测干湿状态，也不进行干重到熟食换算。所有 food_state_status 都要求人工核实。

可使用 --expected-zip-sha256 或 --expected-json-sha256 核对先前独立保存的哈希。哈希相等只说明文件一致，不能代替官方来源验证。source_url、source_version 为调用方声明；source_authenticity 始终为 not_independently_verified。版本不是2026-04时，应显式传入对应 --source-url，不能使用默认2026-04链接。

## 安全规则

- JSON 最大32 MiB；ZIP 最大10 MiB、256成员、总解压64 MiB、单成员32 MiB、压缩比最大2000。
- 拒绝绝对路径、上级目录、Windows盘符/反斜杠、符号链接、特殊文件、加密成员及重复/大小写冲突成员名。
- 每个成员完整读取，ZIP 标准库检查 CRC；不使用 extractall，也不采用归档路径写文件。
- 只将唯一 JSON 写入仓库外 TemporaryDirectory 的固定 snapshot.json，结束或异常自动清理。不保留解压大文件。
- 原始输入只读；处理前后哈希一致检查。拒绝输入符号链接。
- 报告目录排他创建，文件使用 x 模式；既有目录、文件或链接拒绝覆盖。写出失败清理本次新建目录。
- 不提供对抗恶意并发替换文件、输出目录或祖先链接的保证；应在用户控制的本地目录运行，不在多用户共享可写目录处理。
- 报告不含本机输入绝对路径；来源URL禁止凭据、查询串和片段。不会修改来源清单或下载任何材料。

## 输出与统计

validation.json 包含来源声明、输入类型、ZIP/JSON SHA-256、文件大小、ZIP成员数及JSON成员定位、工具/解析/映射版本、营养ID和原始单位分布、缺失统计、能量和糖分布、重复项、份量统计、逐项 findings 和规则计数。summary.txt 为中文摘要。相同输入、参数在不同新目录运行产生相同输出字节，不含时间戳或临时路径。

总数等于 successfully_parsed + needs_review + parse_failed：

- parse_failed：非对象食品、非法食品ID、类型不符、无描述或非法foodNutrients数组。
- needs_review：结构可解析，但存在重复、未知ID/单位、缺值、非法数值、缺少必要营养项、LOQ、不可用份量或未核实分母。
- successfully_parsed：未触发上述规则的结构解析结果，仍为 pending，绝不意味着营养正确或可正式推荐。

营养素 occurrence 为记录出现次数；energy_food_counts/sugar_food_counts 为包含该 ID 的食品数，同一食品重复 ID 不重复计入食品数。1008/2047/2048、1063/2000 分开计数，不相加生成能量或糖。未知ID见 finding_rule_counts.unknown_nutrient_id，并保留原始位置。

missing_fields 区分食品字段缺失、营养素元字段缺失、amount缺失和未出现的已映射营养项。numeric_states 的 zero 表示无 LOQ 的原始零；qualified_zero 为 LOQ限定零，不当作真实精确零。非法值包括非数值、非有限值、负值及超过技术上限1e12，不自动纠正。未知ID也扫描数值，不仅限于宏量营养素。

份量 positive_weight_and_amount 只证明 amount 和 gramWeight 是正的有限数值，不证明份量经过人工确认；不生成标准份量，也不计算密度。

## 回归与下一步

测试：python -m unittest discover -s tests -p test_usda_snapshot_validation.py 。其他既有 Python 与 Node 测试照常运行。夹具不入正式数据；报告在临时目录生成后删除，不纳入本次 Git。

正式 SQLite 导入前必须取得官方文件、核实来源/版本和双哈希、完成真实扫描、处理异常和干物质/份量问题、批准不同能量/糖定义的选择策略及缺失字段准入。当前schema单一营养项及单文件哈希限制仍待设计解决；审核和导入事务另外实施。本任务没有提前实现导入模块。

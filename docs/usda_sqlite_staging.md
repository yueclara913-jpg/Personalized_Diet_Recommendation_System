# USDA SQLite 暂存导入 v1

## 兼容性分析与最小方案

从最新开发基线 ef3ed03 开发，不依赖未合并 PR #5。原 11 表 schema 支持来源版本、FDC 身份、原始营养素 ID、单位、基准、NULL/零和审核状态，profile 唯一约束支持同版本身份控制。但 food_nutrients 的主键 (profile_id,nutrient_code) 不能同时无损保存三个能量、两种糖定义；profile 缺少完整原始记录、逐营养素推导和食品干湿状态，release 仅一个文件哈希。

本次不修改 schema.sql、不迁移业务数据库。使用物理独立 SQLite 暂存库，创建 staging_releases / staging_records 两表（独立 application_id 和 user_version=1）。完整 JSON 记录包含全部原始食品、营养素、份量、限定符、转换规则及定位；不拆入 food_nutrients，不选择能量/糖，不创建推荐视图或审核通过记录。暂存完整记录优先于逐营养素关系化查询。

这不需确定营养定义取舍，也没有不可逆迁移。未来进入正式表时仍需用户批准：扩展候选营养素和文件清单结构，或明确能量/糖选取策略；食品干湿状态和份量准入也须人工审核。本模块不能证明真实快照兼容性，官方真实数据仍未取得。

## 输入与验证

只接受已合并 parse_snapshot 输出的完整 JSON envelope，不接受原始 Foundation JSON、PR #5 汇总报告或 API 响应。固定 parser_version=1.0.0 / mapping_version=usda-id-v1。整个原始数组按顺序解析后传入；本版不支持重新编号、抽取或拼接的记录集合。空批次拒绝。

来源 URL 必须是无凭据、查询串和片段的 USDA 官方 HTTPS 地址；来源/版本/原始文件 SHA-256 为调用方声明，不能据此验证真实性。release 以完整 URL+版本识别（不同路径视为不同来源流）；同流同版本哈希或解析元数据冲突拒绝。应固定同一官方规范 URL，不能用改路径绕过版本冲突。

逐记录重用现有解析器从 raw_record 重算派生字段，核对身份、原始数组位置、单位、转换结果、版本、基准和审核状态。不信任传入 standardized 数值。缺失值和零保留；未知单位、未知营养素、非法数值、LOQ、未知基准可以无损进入 pending，并出现在异常统计。不把技术解析通过解释为营养正确。非法/重复食品 ID、非法/重复营养素 ID、缺失原始营养数组、记录被改写、来源冲突会拒绝整个批次。

记录完整保存各能量和糖定义；多定义标记为问题，不混合。所有记录即使无异常也只能 pending；CHECK 约束禁止 approved/rejected，本版无审批功能。没有中文名、标签或营养补值生成。

## 命令行

Python 标准库即可运行，无网络、无 API Key。

先将现有解析器的 JSON 输出保存到仓库外（以下仅为命令模板，不表示已取得真实文件）：

```sh
python -m nutrition.sources.usda_foundation /outside/Foundation.json \
  --source-version 2026-04 --source-url https://fdc.nal.usda.gov/download-datasets/ \
  > /outside/parsed.json
python scripts/import_usda_staging.py /outside/parsed.json --mode dry-run
python scripts/import_usda_staging.py /outside/parsed.json --mode staging \
  --database /outside/review-staging.sqlite
# 重复导入必须显式允许操作既有暂存库：
python scripts/import_usda_staging.py /outside/parsed.json --mode staging \
  --database /outside/review-staging.sqlite --confirm-existing-staging
```

Windows 将 /outside 路径换成用户控制的绝对路径，例如 C:\USDA\review-staging.sqlite。父目录必须存在。人工夹具只允许用于临时测试库，禁止写真实业务库。

默认 dry-run 完成相同输入校验但完全不打开数据库；它不检查目标库现有记录冲突，inserted/skipped 均为0。staging 必须明确目标路径，不接受 :memory:、CSV、链接、非 SQLite 文件或业务数据库；已有库必须确认，且 schema/application_id/version/FK/integrity 校验完全通过。已有业务库即使确认也拒绝。不要在不受信任的多用户共享目录运行；不承诺防御恶意并发文件/祖先路径替换。

stdout 输出结构化 JSON 报告，可自行保存为新的仓库外文件；工具不自动创建报告路径，避免覆盖。shell 重定向自身会覆盖文件，调用方应使用新文件或 shell 的防覆盖设置。失败以退出码2和 stderr JSON 拒绝原因报告，不产生成功报告。报告包含总量、插入、跳过、异常记录/规则数量、逐食品位置、来源声明、原文件哈希及计算的完整解析批次哈希，不记录输入本机路径。失败批次不返回部分成功数量。

## 事务与追溯边界

每个写连接开启 foreign_keys，BEGIN IMMEDIATE 覆盖 DDL、release、所有记录；任意失败回滚，失败新库清除，已有库记录保留。成功重复批次只跳过完全一致记录；同来源版本/FDC 内容冲突拒绝，绝不覆盖。同一 FDC 可按不同版本隔离。

原始文件哈希不能从解析后的 raw_record 重新生成（原数字词法已被转换为十进制文本）；暂存保留声明的原始哈希、计算 record_sha256 / batch_sha256。后者验证解析内容一致，不能替代原快照哈希核实。输入只读，API 不改传入对象。文本和未知字段可能包含上游内容，应在真实数据落库前另行检查隐私及许可。

## 测试与后续

全部夹具 TEST_ONLY，不是 USDA 实测。运行：

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py'
node --test tests/frontend_events.test.cjs
sha256sum food_data.csv
```

后续需真实官方快照验证、审核工作流、规范 source_id、双文件哈希结构、候选营养项和分母/食品状态准入后，才能讨论正式导入。当前 Flask、推荐算法、原 CSV、前端、schema 和 PR #5 均保持不变。

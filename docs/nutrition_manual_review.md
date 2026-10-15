# 食品营养人工审核与审计工作流 v1

## 状态边界

本模块使用独立 SQLite 审核数据库记录人工处理过程，不修改 PR #6 暂存库和正式营养数据库。五组概念必须区分：

- 暂存 `pending`：PR #6 数据状态，始终不变。
- `ready_for_manual_review / needs_review / rejected_by_policy`：PR #7 规则引擎建议，不是人工决定。
- `accept_candidate / request_changes / reject_candidate`：本模块人工决定，仅对后续准入候选有效。
- `supersede_decision`：追加一条更正事件，以新的有效决定替代当前投影，不覆盖旧事件。
- 正式数据库 `approved`：未来经过身份认证、授权和正式导入后的状态；本模块永不生成。

`accept_candidate` 只表示审核人声明愿意将记录保留为后续正式准入候选，不表示食品营养正确、USDA 来源已认证、管理员已授权或食品可以参与推荐。

## 与 PR #6、PR #7 的集成

决定前通过 PR #7 的 `open_staging_readonly` 验证暂存库 application ID、版本、完整 schema、外键及完整性，并以 SQLite `mode=ro` 和 `query_only=ON` 打开。随后核对 `(release_key, fdc_id)`、记录摘要、release 元数据、解析器/映射版本和永久 `pending` 状态，再调用 PR #7 的 `evaluate_food_record`。规则不在工作流中重复实现。

审核数据库与暂存库物理隔离，具有独立 application ID `0x4E525657` 和 user_version 1。它只有 `review_events` 与 `current_decisions`：前者是追加事件事实，后者是可重建的当前投影。没有 foods、food_profiles、food_nutrients 或推荐视图，也不修改 `nutrition/schema.sql`。

若暂存记录摘要、来源版本或策略版本变化，已有事件仍保留为历史，但不会自动应用于新输入。再次决定必须显式使用 `supersede_decision`，并将当时重新计算的记录摘要、策略版本和规则结果写入新事件。

## 前置条件和状态转换

首次处理只允许 `accept_candidate`、`request_changes` 或 `reject_candidate`。已有当前决定后，普通决定被拒绝，只能追加 `supersede_decision` 并明确指定替代决定。没有旧决定时不能 supersede。

- `rejected_by_policy` 禁止有效决定为 `accept_candidate`，但可以记录退回或人工拒绝，避免问题记录被静默丢弃。
- `needs_review` 可以退回或拒绝。若有条件接受，必须对每条 issue 的稳定键逐项提供非空处理说明；这些说明不消除原问题，也不产生正式批准。
- `ready_for_manual_review` 可以由人工选择三种决定，但仍只形成候选记录。
- 来源真实性未核验通常产生 `SRC007`。有条件接受时也必须解释该 issue；结果仍明确 `official_source_authentication=not_performed_by_workflow`。

操作必须提供唯一 `operation_id`。相同 operation ID 和完全相同请求返回原事件，不重复追加；同一 ID 对应不同请求则拒绝。审核意见和理由均必填，证据引用以字符串列表保存。

## 事件和审计字段

每条事件保存：单调序号、UUID 事件 ID、幂等操作 ID及其请求摘要、release key、FDC ID、原始记录摘要、策略版本、完整规则评估快照及摘要、事件/有效决定、意见、理由、逐项异常处理、证据引用、操作者声明、UTC 时间、前一决定与被更正事件、前一全局事件摘要、哈希算法及事件摘要。

`review_events` 具有禁止 UPDATE 和 DELETE 的触发器。写入使用 `BEGIN IMMEDIATE`，事件与当前投影在同一事务提交；任一失败完整回滚。`current_decisions` 只保存每个 `(release_key, fdc_id)` 的当前事件引用、记录摘要、策略和有效决定，不能当作正式食品状态。

`verify` 重算规范 JSON 请求摘要、评估摘要、事件摘要和全局前后链，重放首次/更正状态转换，并核对当前投影指向每个候选的最后事件。哈希链只有篡改检测辅助价值：没有外部签名、可信时间戳或外部锚点，拥有数据库完全控制权的攻击者仍可重建整个数据库和哈希链。

跨暂存库与审核库无法建立单个 SQLite 原子事务。工作流在决定前只读核对暂存摘要；PR #6 导入器本身不更新已有记录。若外部工具绕过约束并并发替换整个暂存文件，当前版本不能提供跨文件锁或可信文件系统防护，应在用户控制的非共享目录运行。

## 操作者身份边界

`reviewer_id` 是命令行调用者提供的字符串声明，统一记录 `reviewer_identity_assurance=self_asserted_cli`。即使值为 `admin`，也不表示已认证管理员或具有任何角色权限。本模块不得作为 Flask 公共 API 暴露；真正的账户认证、角色授权、会话校验和授权审计必须在后续独立任务实现。

`official_file_verified` 等字段同样只是调用方提供的外部证据声明。程序只检查它们与 URL、版本和 SHA-256 的一致性，不联网、验签或独立认证 USDA 来源。官方真实快照至今尚未取得和验证。

## 命令行

所有数据库路径必须显式提供，没有默认正式路径，也不接受任意 SQL。

```sh
python scripts/manage_nutrition_reviews.py init --review-db /outside/manual-review.sqlite

python scripts/manage_nutrition_reviews.py evaluate \
  --staging-db /outside/staging.sqlite --release-key RELEASE --fdc-id 123

python scripts/manage_nutrition_reviews.py decide \
  --review-db /outside/manual-review.sqlite --staging-db /outside/staging.sqlite \
  --release-key RELEASE --fdc-id 123 --operation-id operation-001 \
  --decision request_changes --reviewer-id reviewer-declared \
  --comment "需要补充来源" --reason "来源真实性尚未核实"

python scripts/manage_nutrition_reviews.py history \
  --review-db /outside/manual-review.sqlite --release-key RELEASE --fdc-id 123
python scripts/manage_nutrition_reviews.py verify --review-db /outside/manual-review.sqlite
python scripts/manage_nutrition_reviews.py summary --review-db /outside/manual-review.sqlite
```

`evaluate` 和 `decide` 可传 `--policy` 与 `--provenance-evidence` JSON。`decide` 可传 `--issue-resolutions` 字符串键值对象和 `--evidence-references` 字符串数组；审核文本单项限制 4000 字符，证据引用最多 100 项，审计 JSON 限制 1 MiB。需要更正时使用 `--decision supersede_decision --replacement-decision ...`。CLI JSON 输入必须是 1 MiB 以内的常规非链接文件。

CLI 成功输出 JSON；失败以退出码 2 和结构化 stderr 报告，不悄悄忽略失败。`history`、`verify` 和 `summary` 以只读模式打开审核库。创建审核库使用独占文件创建，已有路径拒绝覆盖；既有库必须通过精确结构、版本、外键与完整性检查。

## 尚未完成

本版没有管理员认证、网页界面、正式数据库迁移、正式批准、推荐接入、能量/糖定义选择或 USDA 真实快照验证。下一阶段需要独立设计认证授权和正式准入事务，并由用户批准营养定义及数据库迁移方案。

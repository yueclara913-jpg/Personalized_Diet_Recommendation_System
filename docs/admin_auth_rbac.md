# 管理员认证与 RBAC v1

## 范围和边界

本模块提供独立的 SQLite 管理员账户、密码认证、集中权限规则、安全操作审计，以及 PR #8 人工审核的授权适配层。它不修改 `main.py`、Flask API、正式营养数据库、推荐算法或 `nutrition/schema.sql`，也不创建正式 `approved` 食品。

项目当前没有普通用户账户系统。本模块只用于后端管理员命令行流程，不代表普通用户登录，也不对外开放管理 API。USDA 来源真实性仍由独立证据流程处理；管理员认证不能证明食品来自 USDA 官方快照，真实 USDA 快照仍未验证。

## 账户与密码

认证数据库使用独立 application ID `0x4E415554`、user version 1，并严格核对完整 schema。账户具有不可变 UUID、唯一且不区分大小写的用户名、Werkzeug `pbkdf2:sha256:260000` 加盐密码哈希、哈希方案标识、角色、`active/disabled` 状态、创建时间、最后登录时间和密码变更时间。数据库不保存明文密码、会话令牌或 API Key。

密码必须通过交互式 `getpass` 输入，不能作为命令行参数。认证失败统一返回 `Authentication failed`，避免直接枚举用户名。连续 5 次失败触发 5 分钟冷却；成功登录清除失败计数。

认证成功生成 256 位随机短期票据，有效期 10 分钟。票据明文只保存在当前进程的 `Principal` 中，SQLite 仅保存 SHA-256 摘要、会话 ID、有效期和账户凭据版本。每个敏感函数都在自身事务内重新计算摘要并使用常量时间比较，同时核对会话未过期、未撤销、账户仍 active、当前角色与凭据版本一致。手工构造 `Principal` 并不能伪造数据库中不存在的随机票据。密码修改或重置、角色变更和账户停用都会递增凭据版本并撤销该账户的全部旧票据。

## 权限矩阵

| 权限 | system_admin | nutrition_reviewer | audit_viewer |
|---|---:|---:|---:|
| admin.create / disable / assign_role / reset_password | 是 | 否 | 否 |
| review.evaluate | 是 | 是 | 否 |
| review.decide | 是 | 是 | 否 |
| review.history / verify / summary | 是 | 是 | 是 |
| audit.read | 是 | 是 | 是 |

未知角色、未知权限、未认证身份默认拒绝。调用方提交的角色不会被信任；每次操作都通过认证票据回查当前数据库角色和状态。仅按对象字段判断角色的辅助入口不参与敏感操作。最后一个有效 `system_admin` 不能被禁用或降级。

## PR #8 安全集成

PR #8 的 v1 schema 将 `reviewer_identity_assurance` 固定为 `self_asserted_cli`，且该字段参与事件哈希。批量改写旧事件或放宽 CHECK 会破坏兼容性和审计链，因此本版不迁移审核库。

安全适配器 `authorized_review_decision` 先验证管理员账户当前状态和 `review.decide` 权限，再调用原有状态机。原事件继续明确标记 `self_asserted_cli`；认证数据库随后追加 `authorized_review_actions`，绑定：

- 认证账户内部 ID；
- 原审核事件 ID 和事件哈希；
- release key、FDC ID 和操作 ID；
- 权限 `review.decide`；
- 身份保证等级 `authenticated_admin_db_v1`；
- 对应的追加式安全审计事件。

只有同时通过两个数据库完整性校验和关联核对的组合结果，才是“已认证候选决定”。直接调用旧 `record_decision` 或旧 `manage_nutrition_reviews.py decide` 最多产生 `self_asserted_cli` 事件，没有授权关联，不能被后续正式准入信任。`accept_candidate` 仍不是 `approved`。

审核库和认证库无法组成单一 SQLite 事务。若审核事件成功而授权关联写入失败，会留下一个未授权的 `self_asserted_cli` 事件；它不会被误认为已认证。故障注入测试验证了认证事务完整回滚且关联验证失败。使用同一 operation ID 重试时，原审核事件按 PR #8 幂等返回，再补写授权关联。该设计优先避免错误授权，代价是需要运维识别未关联事件。

账户后来停用或角色撤销不会改写历史授权事实，但会立即阻止新决定。安全事件和授权关联由触发器禁止 UPDATE/DELETE，并通过 SHA-256 摘要链辅助发现篡改。没有外部签名、可信时间戳或外部锚点时，拥有数据库完全控制权的人仍可重建整个数据库和哈希链。

## 命令行

```sh
python scripts/manage_admin_auth.py init --auth-db /outside/admin-auth.sqlite
python scripts/manage_admin_auth.py bootstrap-admin --auth-db /outside/admin-auth.sqlite --username admin
python scripts/manage_admin_auth.py authenticate --auth-db /outside/admin-auth.sqlite --username admin
python scripts/manage_admin_auth.py create-admin --auth-db /outside/admin-auth.sqlite \
  --actor admin --username reviewer --role nutrition_reviewer
python scripts/manage_admin_auth.py list-admins --auth-db /outside/admin-auth.sqlite --actor admin
python scripts/manage_admin_auth.py verify --auth-db /outside/admin-auth.sqlite
```

授权审核使用独立入口，用户名只用于查找账户，角色来自认证数据库：

```sh
python scripts/authorized_nutrition_review.py \
  --auth-db /outside/admin-auth.sqlite --review-db /outside/manual-review.sqlite \
  --staging-db /outside/staging.sqlite --username reviewer \
  --release-key RELEASE --fdc-id 123 --operation-id op-001 \
  --decision request_changes --comment "需要补充证据" --reason "来源尚未核实"
```

所有路径都必须显式传入。管理员列表经过脱敏，不包含密码哈希、失败次数或锁定细节。密码不会写入普通输出、错误日志或 Git。

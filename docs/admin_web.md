# 管理员 Web 登录与食品营养审核管理界面

## 运行与配置

管理员 Web 入口通过 `admin_web.app:create_app` 装配。它复制现有 Flask 普通用户路由并注册独立管理员 Blueprint，因此不修改 `main.py`。生产环境必须显式设置：

- `ADMIN_WEB_SECRET`：Flask 签名密钥；
- `ADMIN_AUTH_DB`：PR #9 管理员认证数据库；
- `ADMIN_WEB_SESSION_DB`：独立 Web 会话数据库；
- `ADMIN_STAGING_DB`：PR #6 暂存数据库；
- `ADMIN_REVIEW_DB`：PR #8 审核数据库。

```sh
python -m admin_web.app
```

生产配置强制 `Secure` Cookie，应只通过 HTTPS 提供管理员页面。测试环境可以显式关闭 Secure 以使用 Flask 测试客户端；生产环境关闭会导致应用拒绝启动。

## 会话与 CSRF

浏览器收到全新生成的随机 Web 会话 Cookie；PR #9 的进程内 Principal 票据绝不发送给浏览器。独立会话数据库只保存 Cookie SHA-256、账户 ID、登录时凭据版本、创建/访问/过期时间、CSRF 摘要及撤销时间。绝对有效期 8 小时、空闲超时 30 分钟。每个请求实时查询认证数据库，账户停用、角色变化或密码变化导致凭据版本不匹配，旧 Web 会话立即失效。

会话 Cookie 使用 HttpOnly、SameSite=Strict、Path=/admin，生产强制 Secure。登录使用 10 分钟签名双提交 CSRF；所有状态变更 POST 使用与服务器会话绑定的随机 CSRF。登录成功总是替换 Cookie，防止会话固定。退出会在服务器端撤销会话。

管理员页面设置 no-store、nosniff、DENY frame 和仅同源 CSP。Jinja 默认转义食品名称、来源字段和审核意见。SQL 查询使用参数，搜索最长 100 字符，每页 20 条，页码有上限。

仪表盘先单独读取暂存记录总数。总数不超过 5000 时，使用现有只读审核扫描器在一个已校验连接中计算完整规则分类；超过 5000 时不会枚举全部食品身份或逐条执行规则，三个分类明确显示“待计算”，并提示数据量超过统计上限。未计算值使用空值语义，不作为真实零值展示。

## 权限和审核桥接

后端逐请求复用 PR #9 RBAC。system_admin 与 nutrition_reviewer 可审核；audit_viewer 只读。前端隐藏操作区只是体验优化，POST 路由仍独立检查 `review.decide`。

Web 会话用于页面访问。写入审核决定时，管理员必须再次输入当前密码；服务器调用 PR #9 `authenticate()`，并核对认证账户内部 ID 与 Web 会话账户一致，再调用 `authorized_review_decision()`。客户端提交的 reviewer_id 或身份保证等级直接拒绝。提交同时绑定 release key、FDC ID、暂存记录 SHA-256 和服务器生成的随机 operation ID。

PR #7 规则引擎及 PR #8 状态机保持不变：策略拒绝记录不能接受，needs_review 条件接受必须逐项说明，历史事件不修改，旧 self_asserted_cli 事件不会自动获得授权。接受只形成候选，不创建 approved，也不写入正式推荐库。

认证库、审核库与会话库无法形成单个跨库事务。授权写入失败时不得把未关联事件视为已认证；未来正式准入必须调用 `verify_authorized_review_action()`。本阶段没有管理员账户管理页面、正式准入发布、推荐数据源切换或 USDA 官方真实快照验证。

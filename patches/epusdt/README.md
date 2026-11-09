# epusdt：PostgreSQL settings 兼容补丁

## 固定上游

- 项目：GMWalletApp/epusdt
- tag：`v2.0.0`
- commit：`619cecb11d6bbd1dcdd1b698ee08b37fc722d3e3`
- 许可证：GPLv3；上游署名与 LICENSE 保留在构建镜像 `/licenses/LICENSE`。

## 为什么必须修改

官方 `v2.0.0` 在 PostgreSQL 下首启无法创建管理员：`admin_user_data.go` 把 MySQL 反引号 `` `key` `` 直接用于 SQL，导致 `EnsureDefaultAdmin` 事务回滚，`admin_users` 仍为 0。`settings_data.go` 还以相同方式处理 `key` 和 PostgreSQL 保留字 `group`，会影响后台设置读取。

本项目按任务要求统一使用 PostgreSQL，因此不能通过改用 SQLite 来掩盖该问题。

此外，官方版本会拒绝所有内网 `notify_url`，使同一 Compose 网络中的 `payment-adapter` 无法作为回调接收方。补丁增加 `EPUSDT_INTERNAL_NOTIFY_URL` **精确完整 URL** 白名单；只有完全相等的一个内部地址可绕过公网校验，其他内网/环回地址仍被拒绝。adapter 收到回调后仍强制校验来源容器 IP 与 HMAC-SHA256。

## 改动范围

补丁：`0001-postgres-settings-compat.patch`：

- `src/model/data/admin_user_data.go`：六处 `key` 条件移除 MySQL 专用反引号；`key` 在 PostgreSQL、MySQL、SQLite 中均可直接作为此查询的列名。
- `src/model/data/settings_data.go`：`key` 查询/排序移除反引号；`group` 条件改用 GORM `clause.Eq` + `clause.Column`，由当前数据库驱动负责引用。
- `src/model/service/order_service.go`：新增精确内部回调白名单，默认未配置时不改变上游行为。
- `src/model/service/order_service_test.go`：覆盖白名单命中、其他内网地址及路径变体拒绝。

不改变签名算法、金额、钱包、汇率或入账逻辑。

## 验证与构建

```bash
cd src
go test ./model/data ./model/service
```

项目根目录构建：

```bash
docker build -f epusdt/Dockerfile .
```

Dockerfile 克隆固定 tag、核对完整 commit，然后执行 `git apply --check` 并应用补丁。升级上游前必须重新审查补丁，不要只改镜像 tag。

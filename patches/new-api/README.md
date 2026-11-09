# New API：GMPay v2 精确额度结算补丁

## 固定上游

- 项目：QuantumNous/new-api
- release：`v1.0.0-rc.49`（发布页：https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.49）
- commit：`48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0`
- commit URL：https://github.com/QuantumNous/new-api/commit/48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0
- 上游 Dockerfile：https://github.com/QuantumNous/new-api/blob/48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0/Dockerfile

`UPSTREAM_COMMIT` 与 `new-api/Dockerfile` 中的 `UPSTREAM_COMMIT` 必须一致。补丁由该 commit 的真实 Git 工作树通过 `git diff` 生成，不适用于其他上游版本。

## 应用与构建

在上游源码根目录（HEAD 必须是上述 commit）：

```bash
git apply --check /path/to/patches/new-api/0001-exact-gmpay-settlement.patch
git apply /path/to/patches/new-api/0001-exact-gmpay-settlement.patch
```

或使用本仓库根目录的 Dockerfile；它会克隆固定 tag、核对完整 commit，然后执行 `git apply --check` 和应用补丁：

```bash
docker build -f new-api/Dockerfile .
```

运行时仅在需要 GMPay v2 精确结算时设置：

```text
EPAY_HMAC_KEY=<GMPay v2 HMAC 密钥>
EPAY_EXACT_QUOTA_ENABLED=true
```

两个条件必须同时满足。带 `signature` 字段的回调在开关未启用时也会被拒绝，不会回退到 MD5 或建单 `Amount`。不带 `signature` 的旧 EPay 回调继续使用上游 go-epay MD5 验签和原结算逻辑。

## 改动明细

补丁：`0001-exact-gmpay-settlement.patch`

### `controller/topup.go`

上游文件：https://github.com/QuantumNous/new-api/blob/48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0/controller/topup.go

- 新增 GMPay v2 验签：非空参数按 key 的 ASCII 顺序排列，排除 `signature`，将 `key=value` 用 `&` 连接（末尾没有 `&`），使用 `EPAY_HMAC_KEY` 计算 HMAC-SHA256，小写 hex，并用 `hmac.Equal` 常量时间比较。规范化规则与 `adapter/app/signing.py` 的 `canonical_params`/`gmpay_hmac` 一致。
- 只有精确开关和 HMAC key 同时配置时才接受带 `signature` 的回调；验签失败返回 HTTP 401。原 MD5 验签失败也改为 HTTP 401。
- 校验 `pid == EpayId`、`out_trade_no` 非空、`trade_status=TRADE_SUCCESS`；`credit_quota` 必须为正十进制整数，并经 `WalletQuotaFromDecimalStrict` 限制在 int/钱包额度范围内。
- `out_trade_no` 作为 New API 的 `tradeNo`；`type` 仅用于更新实际支付方式。`settled_usd` 可选，非空时必须是非负 decimal 且可表示为有限 float64，结算时更新 `TopUp.Money` 供记录。
- HMAC 路径调用新增的 `model.RechargeEpayExact`，只使用本次 `credit_quota`，不使用建单 `Amount`；其余订单状态、错误处理及成功响应保持原流程。
- 回调参数日志脱敏 `sign`、`signature`、key/secret 字段；GET 日志只记录 URL path，不记录可能带签名的 query；建单日志不再输出带签名的支付 URL。

### `model/topup.go`

上游文件：https://github.com/QuantumNous/new-api/blob/48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0/model/topup.go

- 新增 `RechargeEpayExact`，与原 `RechargeEpay` 共用内部事务实现；原函数签名和 MD5 金额计算方式不变，其他支付路径不变。
- 精确路径沿用订单行锁、事务内状态检查、钱包上限原子更新和缓存同步，仅将入账值替换为回调 `credit_quota`。
- `TopUp` 新增 `CreditedQuota`（GORM 随既有 `AutoMigrate(&TopUp{})` 添加列），记录首次精确入账额度。重复回调返回 `alreadyDone=true`，不重复加款，也不改写首次 `Money`/额度；不同 quota 的重复回调记录 warn 日志。
- `settled_usd` 仅影响首次结算记录，不破坏原 EPay 建单金额及 MD5 路径。

### 新增测试

- `controller/topup_gmpay_test.go`：固定 HMAC 向量、篡改/错误 key/大写签名拒绝、精确字段和钱包范围校验、401 响应辅助函数、日志字段脱敏。
- `controller/payment_webhook_availability_test.go`：更新上游 EPay 回调测试，无效/缺失 MD5 签名及含分隔符的歧义参数均断言 HTTP 401；合法非成功事件仍断言 HTTP 200。
- `model/topup_exact_test.go`：精确 quota 入账、`settled_usd` 记录、相同及不同 quota 重复回调幂等、非法 quota 不入账。

建议在有 Go 工具链的环境运行：

```bash
go test ./controller ./model
```

本机 Windows 实测：`model` 全量通过；补丁相关的 `controller` / `model` 定向测试均通过。全量 `controller` 在上游多组 SQLite 测试的 `TempDir RemoveAll` 清理阶段因 `audit.db` 文件仍被占用而失败；没有通过修改这些无关上游测试来规避。部署到 Linux/Docker 后应再跑上述全量命令。

## 许可

上游及修改后程序均使用 AGPLv3，署名和许可文件保留。详见 `AGPLv3-NOTICE.md`。

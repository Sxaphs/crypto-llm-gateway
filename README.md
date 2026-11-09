# Crypto LLM API MVP

基于 **QuantumNous/new-api** 与 **GMWalletApp/epusdt (GM Pay)** 的纯 Crypto 收款中转站。流程为：邮箱验证码注册 → USDT/USDC 充值 → 创建 API 令牌 → OpenAI 兼容接口 / Seedance 视频 → 按量扣额度。

不包含 KYC、法币通道、提现、退款、分销或白标功能。epusdt 只需要观察钱包地址，不需要私钥或助记词。

**第一次部署请直接阅读：[傻瓜式新手部署与使用指南](docs/新手部署与使用指南.md)。** 其中包含 Windows/Docker/WSL、代理、本地域名、证书、管理员、收款地址、GMPay、充值、API 池、验收和常见报错的逐步操作。

## 架构

```text
用户 → SITE_DOMAIN ───────────────→ New API
  └─ /crypto-pay/submit.php ───────→ payment-adapter
                                      │ GMPay v2 HMAC-SHA256
                                      ▼
PAY_DOMAIN（独立收银台）─────────────→ epusdt ── 链上确认
                                      │ GMPay v2 HMAC-SHA256
                                      ▼
                              payment-adapter
                              1. 验签 + 容器 IP 白名单
                              2. 回查订单和链上 tx/from
                              3. 固化确认时外部汇率
                              4. HMAC + credit_quota
                                      ▼
                              New API 事务入账

PostgreSQL：new_api / epusdt / adapter 三库
Redis：New API 缓存与限流，AOF 持久化
reconciler：每日 adapter ↔ New API ↔ epusdt ↔ 链上对账
```

epusdt v2 的运行时队列/锁使用自身持久化 SQLite；Redis 是 New API 的依赖，不向 epusdt 添加已移除的旧 Redis 配置。

## 安全边界

- epusdt 与 adapter 之间的建单、回调均为 **GMPay v2 HMAC-SHA256**；验签失败返回 401。
- New API 官方 EPay 下单客户端只支持 MD5，因此 MD5 仅保留在 `New API → adapter` 的建单兼容边界。链上支付回调和 adapter → New API 的精确结算通知都使用 HMAC-SHA256。
- New API 最小补丁复用原有 PostgreSQL 行锁、唯一订单号和 `pending → success` 事务；adapter 另外保存不可变结算快照和通知 outbox。重复回调不会重新取价或重复入账。
- 汇率只从 epusdt 当前确认时刷新得到的外部 CNY 汇率快照计算，必须同时含目标 token 和 USD；失败、过期或缺字段时不结算，**没有 1:1 fallback**。计算公式为 `usd_per_token = usd_per_cny / token_per_cny`，`credit_quota = ROUND_HALF_UP(actual_amount × usd_per_token × 500000)`。
- 首次结算必须按 tx hash 回查链，核对代币合约、收款地址、实际金额，并保存真实付款地址。链查失败时不入账，等待安全重试。
- 公网用户域拦截管理 API 和支付回调。`/api/user/epay/notify` 仅允许 adapter 固定容器 IP；adapter 回调仅允许 epusdt 固定容器 IP。
- 管理页面使用独立域名 + Basic Auth。由于浏览器和 New API/epusdt API 都使用 `Authorization: Bearer`，管理 API 保留其原生 session/Bearer/JWT 鉴权，不再叠加会发生 header 冲突的 Basic Auth。
- PostgreSQL 仅绑定宿主机 `127.0.0.1:15432` 供验收/对账脚本使用；Redis 不发布宿主端口。若不需要宿主脚本直连，可移除 Compose 中 postgres 的 `ports`。
- 所有服务固定 UTC；PG、Redis、New API、epusdt、Caddy、对账报告均持久化。

## 前置条件

- Docker Engine + Docker Compose v2.23.1+（使用 `configs.content`）。
- 四个 DNS 名称指向服务器，并开放 80/443：
  - `SITE_DOMAIN`：New API 用户站；
  - `PAY_DOMAIN`：epusdt 收银台；
  - `NEW_API_ADMIN_DOMAIN`：New API 管理页面；
  - `EPUSDT_ADMIN_DOMAIN`：epusdt 管理页面。
- SMTP、TronGrid、Etherscan、Telegram、OpenAI 兼容上游和火山方舟凭据。
- 三个自有收款地址：TRON USDT、Ethereum USDC、Polygon 原生 USDC。

本机测试可使用 `.env.example` 中的 `localhost` / `*.localhost`，并将这些名称写入 hosts。Caddy 会使用内部 CA，浏览器需信任其本地根证书；生产必须使用公网 DNS 和有效 ACME 邮箱。

## 1. 配置环境

```bash
cp .env.example .env
```

填写所有 `CHANGE_ME`。密钥可用 `openssl rand -hex 32` 生成。`POSTGRES_PASSWORD` 会嵌入 DSN，请使用 URL 安全随机串，不要使用 `@ : / ? #` 或 `${`。

生成管理页面 bcrypt 哈希：

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext '替换为强密码'
```

将输出填入 `ADMIN_BASIC_AUTH_HASH`，并用**单引号包住整个哈希**（例如 `ADMIN_BASIC_AUTH_HASH='$2a$...'`），避免 Compose 将 `$` 片段插值。Basic Auth 用户名为 `admin`。同时把 `ADAPTER_PG_DSN` 中的密码改成与 `POSTGRES_PASSWORD` 一致。

## 2. 第一阶段：启动底座

Linux/macOS：

```bash
chmod +x start.sh
./start.sh --base
```

Windows PowerShell：

```powershell
.\start.ps1 -Base
```

base 模式只启动 PostgreSQL、Redis、New API、epusdt 和 gateway，允许 GMPay/管理员密码暂缺。

1. 打开 `https://NEW_API_ADMIN_DOMAIN`，完成 New API 首次 root 初始化，把账号密码回填 `NEW_API_ADMIN_USERNAME/PASSWORD`。
2. 查看 epusdt 首启日志获取初始 admin 密码：

   ```bash
   docker compose logs epusdt
   ```

3. 打开 `https://EPUSDT_ADMIN_DOMAIN`，登录并立即修改密码，回填 `EPUSDT_ADMIN_PASSWORD`。

## 3. 配置 epusdt

在 epusdt 管理后台完成：

1. **链与代币**：只启用 `USDT/TRON`、`USDC/Ethereum`、`USDC/Polygon`；禁用其他收款组合。Polygon 使用原生 USDC 合约 `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`。
2. **钱包**：分别添加 `.env` 中的三个观察地址并启用。不要提交私钥。
3. **RPC**：TRON 节点配置 TronGrid API Key；Ethereum/Polygon 配置稳定的 WSS RPC，并执行健康检查。
4. **汇率**：设置 `rate.mode=auto`，`rate.api_url` 为可靠的外部源，`rate.cache_ttl_seconds=300`。默认记录值见 `EPUSDT_RATE_API_URL`；生产建议换成有 SLA 的付费源。执行手动刷新，确认 CNY 快照同时含 `usd/usdt/usdc` 且时间新鲜。
5. **API Key**：创建 GMPay API Key，回填 `GMPAY_PID/GMPAY_SECRET_KEY`；IP 白名单设为 adapter `172.30.250.30`。
6. **通知**：创建 Telegram notification channel，启用支付成功、过期和日报事件。

## 4. 初始化 New API 并全量启动

`NEW_API_BASE_URL` 应指向 New API 管理域。先检查再写入：

```bash
python scripts/bootstrap.py --check
python scripts/bootstrap.py --apply
python scripts/validate_pricing.py
```

`--apply` 会通过官方 session API 确认 New API 的支付合规条款，然后配置：

- 系统名称和文字 Logo；
- SMTP、公开邮箱注册和强制邮箱验证码；
- 三种 Crypto 支付方式；
- 1–2 个 OpenAI 兼容渠道；
- 内置豆包视频渠道（type 54），包含四个 Seedance 模型和 Seedream；
- `default` 分组及定价。

启动支付与每日对账服务：

```bash
./start.sh          # Windows: .\start.ps1
docker compose ps
```

## 定价

`pricing/model_ratios.json` 是唯一事实来源，包含 `model_ratio`、`completion_ratio` 和 `model_price`。文本按 token，Seedance/Seedream 默认按次。初始化脚本使用 New API 内置 `CUSTOM` 货币展示，默认把价格与额度标为 **USDT**，USDC 可作为等价付款方式；无需额外前端补丁。

`1 USD = 1 USDT` 仅是稳定币标价与显示约定，不是支付结算的 1:1 fallback。充值仍按链上实收数量和确认时外部汇率折算额度，详见 `pricing/README.md`。

**文件中的非零价格只是初始运营值，不代表火山引擎当前成本。营业前必须按实际上游价格、分辨率/时长成本和利润率核对。** 修改后重新执行 `python scripts/bootstrap.py --apply`。

## 对账

每日由 reconciler 自动执行，报告写入 `reports` 卷。手工执行：

```bash
python scripts/reconcile.py
```

可选参数：`--since <unix秒> --until <unix秒>`。`--no-chain` 仅用于诊断，结果只能是 PARTIAL，不能作为 T5 通过证据。

检查内容包括：三方订单状态、email、收/付款地址、tx hash、代币合约、实际金额、确认时汇率、精确 quota、重复记录，以及配置钱包上的无订单链上入账。差异写 JSON/CSV 并发送 Telegram。脚本不自动补单、不改余额。

## T5 验收

详见 `acceptance/README.md`。典型命令：

```bash
python scripts/acceptance.py --all --payment-timeout 900 --restart
```

也可逐项使用 `--only email_register|order_payment|openai_chat|seedance_video|epusdt_resend_callback|restart|reconcile`。真实链上付款需要人工从测试钱包发起；脚本会打印收银台和收款地址，不会伪造付款。

证据写 `acceptance/evidence/`，状态写 `acceptance/status.json`。只有真实检查成功才写 PASS；缺凭据为 BLOCKED，失败为 FAIL。

**当前交付状态：七项均为 NOT RUN。** 本工作环境没有 Docker 守护进程、真实钱包付款、SMTP 和上游密钥，因此没有生成虚假的通过截图或日志。adapter 的本地 mock 单元测试通过不代表真实 T5 已通过。

## 常用运维

```bash
docker compose ps
docker compose logs -f new-api
docker compose logs -f epusdt
docker compose logs -f payment-adapter
docker compose logs -f reconciler
docker compose restart
```

不要删除命名卷。备份至少覆盖 PostgreSQL、epusdt 的 `/data`（含运行时 SQLite）、New API `/data` 和 reports。`.env` 与备份均需按秘密信息保护。

## 源码补丁与许可

New API 固定 `v1.0.0-rc.49`，commit `48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0`。补丁、改动说明和校验方式见 `patches/new-api/README.md`；构建时核对 commit 并应用 patch。不要只修改 `NEW_API_VERSION` 升级，必须先重放并审查补丁。

epusdt 固定 `v2.0.0`，commit `619cecb11d6bbd1dcdd1b698ee08b37fc722d3e3`。官方版本存在 PostgreSQL 管理员初始化失败及拒绝内网回调的问题，因此本项目构建时应用 `patches/epusdt/0001-postgres-settings-compat.patch`：修正 settings/admin 查询，并仅允许 `EPUSDT_INTERNAL_NOTIFY_URL` 精确指定的 adapter 内部回调。不修改签名、金额、汇率或入账逻辑。详见 `patches/epusdt/README.md`。

- New API：AGPLv3，保留 QuantumNous 署名、LICENSE、NOTICE 和第三方许可。
- epusdt：GPLv3，保留 GMWalletApp 署名和 LICENSE；使用上述最小兼容性补丁。
- adapter 为本项目独立协议适配代码，不包含上游商标或白标能力。

## 官方参考

- New API：https://github.com/QuantumNous/new-api
- New API 文档：https://docs.newapi.ai
- epusdt / GM Pay：https://github.com/GMWalletApp/epusdt
- epusdt Docker：https://epusdt.com/guide/installation/docker
- GMPay API：https://epusdt.com/api/reference
- 火山方舟：https://www.volcengine.com/docs/ark/model-list

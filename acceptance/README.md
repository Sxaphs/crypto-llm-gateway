# 验收测试说明（严格版）

`scripts/acceptance.py` 逐项实测 New API 七条关键路径。**只有实测成功才写 PASS**，
失败 FAIL，缺输入 BLOCKED，从未运行 NOT RUN——绝不伪造。

## 七项

| id | 说明 | 通过条件（实测） |
| --- | --- | --- |
| `email_register` | `GET /api/verification` 发码（IMAP 或 `--email-code` 取码）→ `POST /api/user/register` → **再用新账号真实 login** | 注册成功且随后登录成功（并把 user id 写入 `ACCEPTANCE_USER_ID`） |
| `order_payment` | 登录下单账号 `POST /api/user/pay`，把返回 `params` 以 form POST 到 `url` 真正进入收银台（**断言 302 + Location 为有效收银台 URL**），按 `out_trade_no` keyword 查 epusdt（不拿最近单），记录下单前 quota，等待真实付款 | 付款后 New API topup success（超时 FAIL）、adapter 结算，`quota 增量 == credit_quota`，汇率源/tx/email/地址/payer/结算时间齐全；写 `EPUSDT_ORDER_TRADE_ID` / `EPUSDT_ORDER_OUT_TRADE_NO` |
| `openai_chat` | **用 openai SDK（openai.OpenAI）调 `base+/v1`**，读 `resp.usage.model_dump()`，按 `usage` + `pricing/model_ratios.json` 比率计算预期扣费（`ROUND_HALF_UP`），并比较前后 quota | 未安装 openai SDK → BLOCKED（`pip install -r scripts/requirements.txt`）；必须读得到前后 quota；实际扣费 == 预期 |
| `seedance_video` | `POST /v1/video/generations` → `GET /v1/video/generations/{task_id}` 轮询 | 视频 URL（`content.video_url` / `metadata.url` / `url`）可访问，且 `quota 增量 == model_price * 500000`（group ratio=1） |
| `epusdt_resend_callback` | 调 `POST /admin/api/v1/orders/{trade_id}/resend-callback` 重放 | 重放成功且 quota 前后不变（必须读得到前后 quota） |
| `restart` | 需 `--restart`：`docker compose restart` → 检查 New API 健康、quota、topup 订单、adapter 支付订单仍在 | 缺 `EPUSDT_ORDER_OUT_TRADE_NO` 或 topup 重启前不存在 → 不得 PASS |
| `reconcile` | 调 `scripts/reconcile.py` | `--no-chain` 时**不得 PASS**（标 BLOCKED）；完整跑 exit 0 才 PASS |

## 自动串联（同次 `--all`）

- `email_register` 成功后把 user id 写入进程环境 `ACCEPTANCE_USER_ID`。
- `order_payment` 创建后写 `EPUSDT_ORDER_OUT_TRADE_NO`、拿到 epusdt 订单后写 `EPUSDT_ORDER_TRADE_ID`。
- `_get_relay_token` 创建 token 时 meta 带 `user_id`；`openai_chat` / `seedance_video` 在 `ACCEPTANCE_USER_ID` 为空时用 `meta.user_id`。
- 以上只写进程环境变量，不进入 evidence / 日志，避免泄密。

## 令牌来源

- 优先 `ACCEPTANCE_API_TOKEN`；
- 否则用测试账号 session 调 `POST /api/token/` 创建，再 `GET /api/token/` 找新 token id，
  `POST /api/token/{id}/key` 取完整 key。evidence 只记 `token_id/token_name/key_prefix/user_id`，**不记完整 key**。

## 用法

```bash
# 完整验收（缺 openai SDK 会先安装依赖）
pip install -r scripts/requirements.txt
python scripts/acceptance.py --list
python scripts/acceptance.py --only openai_chat
python scripts/acceptance.py --all --email-code 123456 --payment-timeout 600 --restart
```

> 完整验收示例**不带 `--no-chain`**。`--no-chain` 只用于跳过链上核验时的 PARTIAL 对账，
> 此时 `reconcile` 项会被标记 BLOCKED、不得 PASS。

## 产物与安全

- 状态：`acceptance/status.json`；证据：`acceptance/evidence/<id>-<UTC>.json`。
- evidence 不记录 `epay_params`（含 MD5 签名）、密钥、完整 token key。

## 所需环境变量（除 config.py 契约外）

`ACCEPTANCE_API_TOKEN` / `ACCEPTANCE_USER_ID` / `ACCEPTANCE_CHAT_MODEL` / `ACCEPTANCE_VIDEO_MODEL` /
`EMAIL_REGISTER_*` / `IMAP_*` / `EPUSDT_ORDER_TRADE_ID` / `EPUSDT_ORDER_OUT_TRADE_NO` /
`COMPOSE_PROJECT_DIR` / `VIDEO_POLL_TIMEOUT`。

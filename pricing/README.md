# 定价配置说明

`model_ratios.json` 是 New API 计价 option 的唯一事实来源，被 `scripts/bootstrap.py` 导入为
`ModelRatio` / `CompletionRatio` / `ModelPrice` 三个 option（均为 JSON 字符串）。

## 单位与语义

New API 的额度换算基准固定为 `QuotaPerUnit = 500000`，即 **500000 quota = 1 USD 内部计价单位**。初始化脚本使用 New API 内置的 `CUSTOM` 展示，将同一数值直接标为 **USDT**（`1 USD = 1 USDT` 的显示比例）；USDC 是等价付款方式。

这个显示比例不是支付结算的 1:1 fallback。实际到账额度仍按 epusdt 确认时刻的外部汇率、链上实收数量计算，并保存汇率快照。

- `model_ratio`（文本模型，按 token 计费）
  - 值是“倍率”，单位语义为：**倍率 1 = 2 USDT / 1M tokens**（等价于 0.002 USDT / 1K tokens）。
  - 输入侧扣费 = `prompt_tokens / 1e6 * ratio * 2 * QuotaPerUnit`。
- `completion_ratio`（文本模型，输出 token 的倍率乘数）
  - 输出侧扣费 = `completion_tokens / 1e6 * model_ratio * completion_ratio * 2 * QuotaPerUnit`。
  - 没写进 `completion_ratio` 的模型，New API 用硬编码默认值（多数为 1，GPT-4o 系为 4 等）。
- `model_price`（视频 / 图像模型，按次计费）
  - 值是**每次调用的 USDT 标价**（内部使用等价 USD 数值），与 token 无关。
  - 每次生成扣费 = `model_price * QuotaPerUnit`（再乘分组倍率，若有）。

## 覆盖的模型

| 模型 | 计费方式 | 默认值 |
| --- | --- | --- |
| 4 个 doubao seedance 视频模型 | `model_price`（按次 USDT） | 见 `model_ratios.json` |
| doubao-seedream-4-0-250828（图像） | `model_price`（按次 USDT） | 0.05 |
| 若干常见文本模型（gpt-4o 系 / deepseek / claude-3.5） | `model_ratio` + `completion_ratio`（token） | 见文件 |

## ⚠️ 上线前必须核对

当前数值是**保守非零默认值**，不是官方实时价格。上线前必须逐项核对：

1. 4 个 `doubao-seedance-*` 与 `doubao-seedream-4-0-250828` 的**实际单次售价**
   （火山方舟 `ark.cn-beijing.volces.com` 控制台，按分辨率 / 时长 / 秒数计费会不同），
   并把 `model_price` 改成不低于成本价的数值。
2. 文本模型的 `model_ratio` / `completion_ratio` 与所选 OpenAI 兼容上游的真实价格一致。
3. `Price`（建单 CNY 汇率）与 `USDExchangeRate` 在 `bootstrap.py` 中用环境变量注入，
   需与财务记账汇率一致。

不要留 `0` 或 TODO 占位；验证脚本 `scripts/validate_pricing.py` 会拒绝任何非正数值。

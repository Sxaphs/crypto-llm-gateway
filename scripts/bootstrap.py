#!/usr/bin/env python3
"""New API 初始化/幂等配置脚本（仅标准库 + requirements 明示）。

通过 New API 官方 API 完成：
  1. 登录 root 管理员（已手工创建）并生成 access_token；
  2. 配置 SMTP、EmailVerificationEnabled / RegisterEnabled / PasswordRegisterEnabled、
     SystemName / Logo（文字 Logo）、PayAddress、EpayId/EpayKey、Price、MinTopUp、
     PayMethods（仅 usdt.tron / usdc.ethereum / usdc.polygon）、CustomCallbackAddress；
  3. 配置 1~2 个 OpenAI 兼容渠道 + 1 个豆包视频渠道（channel type 54）；
  4. 从 pricing/model_ratios.json 导入 ModelRatio / CompletionRatio / ModelPrice。

用法:
    python scripts/bootstrap.py --check    # 只读核对，打印将要变更的内容
    python scripts/bootstrap.py --apply    # 实际写入（幂等：按名称/模型更新，不重复建渠道）

退出码: 0 成功；1 校验失败（--check 发现有差异也算 1）；2 运行错误。

密钥绝不打印。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Windows 控制台可能是 GBK；强制 UTF-8 输出，避免中文乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import (  # noqa: E402
    load_channels_config,
    load_doubao_channel,
    load_new_api_config,
    load_payment_config,
    load_site_config,
    load_smtp_config,
)
from lib.newapi import NewApiClient, NewApiError  # noqa: E402

# GET /api/option 会隐藏以这些后缀结尾的密钥列，因此对这些键只能"总是写入"，
# 无法读取回来做差异比较。
SENSITIVE_OPTION_KEYS = {"SMTPToken", "EpayKey"}

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing" / "model_ratios.json"


def _bool_str(v: bool) -> str:
    return "true" if v else "false"


def build_desired_options(cfg: Dict[str, Any]) -> Dict[str, str]:
    """返回 {option_key: string_value}。密钥列的值已包含在内，但绝不打印。"""
    smtp = cfg["smtp"]
    site = cfg["site"]
    pay = cfg["pay"]

    pay_methods = json.dumps(
        [
            {"name": "USDT-TRC20", "color": "rgba(var(--semi-teal-5), 1)", "type": "usdt.tron"},
            {"name": "USDC-ERC20", "color": "rgba(var(--semi-blue-5), 1)", "type": "usdc.ethereum"},
            {"name": "USDC-Polygon", "color": "rgba(var(--semi-purple-5), 1)", "type": "usdc.polygon"},
        ],
        ensure_ascii=False,
    )

    # go-epay Purchase 会用 path.Join 自行追加 /submit.php。
    pay_address = f"https://{pay.site_domain}/crypto-pay"

    options: Dict[str, str] = {
        "SystemName": site.system_name,
        "Logo": site.logo_text,
        "ServerAddress": f"https://{pay.site_domain}",
        "RegisterEnabled": _bool_str(site.register_enabled),
        "PasswordRegisterEnabled": _bool_str(site.password_register_enabled),
        "EmailVerificationEnabled": _bool_str(site.email_verification_enabled),
        # rc.49 内置自定义货币展示：按稳定币标价，不改变结算时的外部汇率折算。
        "general_setting.quota_display_type": "CUSTOM",
        "general_setting.custom_currency_symbol": "USDT",
        "general_setting.custom_currency_exchange_rate": "1.0",
        "PayAddress": pay_address,
        "EpayId": pay.epay_id,
        "EpayKey": pay.epay_key,
        "Price": repr(float(pay.price_cny)),
        "USDExchangeRate": repr(float(pay.price_cny)),
        "MinTopUp": str(pay.min_topup),
        "PayMethods": pay_methods,
        "CustomCallbackAddress": pay.callback_address,
    }

    if smtp.host:
        options["SMTPServer"] = smtp.host
        options["SMTPPort"] = str(smtp.port)
        options["SMTPAccount"] = smtp.username
        options["SMTPToken"] = smtp.password
        options["SMTPFrom"] = smtp.sender or smtp.username
        options["SMTPSSLEnabled"] = _bool_str(smtp.ssl)
        options["SMTPForceAuthLogin"] = _bool_str(smtp.force_auth_login)

    return options


def build_pricing_options(pricing_path: Path) -> Dict[str, str]:
    """读取 pricing/model_ratios.json，产出 ModelRatio/CompletionRatio/ModelPrice 字符串。"""
    if not pricing_path.exists():
        raise RuntimeError(f"定价文件不存在: {pricing_path}")
    data = json.loads(pricing_path.read_text(encoding="utf-8"))
    out: Dict[str, str] = {}
    for key in ("ModelRatio", "CompletionRatio", "ModelPrice"):
        src = {"ModelRatio": "model_ratio", "CompletionRatio": "completion_ratio", "ModelPrice": "model_price"}[key]
        if src not in data:
            raise RuntimeError(f"定价文件缺少 {src}")
        out[key] = json.dumps(data[src], ensure_ascii=False)
    return out


def _merge_json_option(current: str, incoming: str) -> str:
    """合并 JSON map：incoming 覆盖同名模型，保留其它已有模型（幂等、不误删运营配置）。"""
    try:
        cur = json.loads(current) if current else {}
        inc = json.loads(incoming)
    except json.JSONDecodeError:
        return incoming
    if not isinstance(cur, dict) or not isinstance(inc, dict):
        return incoming
    merged = dict(cur)
    merged.update(inc)
    return json.dumps(merged, ensure_ascii=False)


def describe_value(key: str, value: Optional[str]) -> str:
    if key in SENSITIVE_OPTION_KEYS:
        return "[已配置]" if value else "[空]"
    if key == "SMTPAccount":
        return value or "[空]"
    if value is None:
        return "[空]"
    if len(value) > 80:
        return value[:80] + "..."
    return value or "[空]"


def apply_channels(client: NewApiClient, cfg: Dict[str, Any], apply: bool) -> List[str]:
    """幂等配置渠道：按名称（OpenAI）与 type（豆包视频）匹配，命中则更新，否则新建。"""
    changes: List[str] = []
    existing = client.list_channels()

    desired_channels: List[Dict[str, Any]] = []

    # OpenAI 兼容渠道
    for ch in cfg["openai_channels"]:
        if not ch.key:
            changes.append(f"OPENAI 渠道 {ch.name}: 未配置 key，跳过")
            continue
        desired_channels.append(
            {
                "name": ch.name,
                "type": ch.type,
                "key": ch.key,
                "base_url": ch.base_url or None,
                "models": ",".join(ch.models),
                "group": ch.group,
                "model_mapping": ch.model_mapping or "",
                "status": 1,
            }
        )

    # 豆包视频渠道 type 54
    doubao = cfg["doubao_channel"]
    if not doubao.key:
        changes.append(f"豆包视频渠道 {doubao.name}: 未配置 ARK_API_KEY，跳过")
    else:
        desired_channels.append(
            {
                "name": doubao.name,
                "type": 54,
                "key": doubao.key,
                "base_url": doubao.base_url,
                "models": ",".join(doubao.models),
                "group": doubao.group,
                "model_mapping": "",
                "status": 1,
            }
        )

    for desired in desired_channels:
        matched = None
        if desired["type"] == 54:
            for ch in existing:
                if int(ch.get("type", 0)) == 54:
                    matched = ch
                    break
        else:
            for ch in existing:
                if ch.get("name") == desired["name"] and int(ch.get("type", 0)) == desired["type"]:
                    matched = ch
                    break

        if matched is None:
            changes.append(f"新建渠道: name={desired['name']} type={desired['type']} models={desired['models']}")
            if apply:
                client.add_channel(desired)
        else:
            update = dict(desired)
            update["id"] = int(matched["id"])
            # 模型/名称/base_url/分组变化时才更新；密钥始终覆盖（可能轮换）
            diff_fields = [
                f for f in ("name", "base_url", "models", "group")
                if str(matched.get(f) or "") != str(desired.get(f) or "")
            ]
            changes.append(
                f"更新渠道: id={matched['id']} name={desired['name']}"
                + (f"（变更: {', '.join(diff_fields)}）" if diff_fields else "（无字段差异，仍刷新密钥）")
            )
            if apply:
                client.update_channel(update)

    return changes


def run(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只读核对，不写入")
    parser.add_argument("--apply", action="store_true", help="实际写入")
    parser.add_argument("--pricing-path", default=str(PRICING_PATH), help="model_ratios.json 路径")
    args = parser.parse_args(argv)

    if args.check == args.apply:
        print("必须且只能指定 --check 或 --apply 之一", file=sys.stderr)
        return 2

    apply = args.apply
    pricing_path = Path(args.pricing_path)

    new_api = load_new_api_config()
    if not new_api.admin_password:
        print("RUN ERROR: 未设置 NEW_API_ADMIN_PASSWORD", file=sys.stderr)
        return 2

    cfg = {
        "new_api": new_api,
        "pay": load_payment_config(),
        "smtp": load_smtp_config(),
        "site": load_site_config(),
        "openai_channels": load_channels_config(),
        "doubao_channel": load_doubao_channel(),
    }

    client = NewApiClient(
        new_api.base_url,
        basic_auth_username=new_api.basic_auth_username,
        basic_auth_password=new_api.basic_auth_password,
    )
    try:
        client.connect(new_api.admin_username, new_api.admin_password)
    except NewApiError as exc:
        print(f"RUN ERROR: 登录失败（{new_api.base_url}）: {exc}", file=sys.stderr)
        return 2

    if client.role < 100:
        print(
            f"RUN ERROR: 账号 {new_api.admin_username} 角色为 {client.role}，"
            "系统选项(/api/option)需要 root 角色(100)",
            file=sys.stderr,
        )
        return 2

    # ---- 支付合规确认（rc.49 充值/支付前置） ----
    compliance_done = False
    if apply:
        try:
            data = client.confirm_payment_compliance()
            compliance_done = True
            print(f"[PAYMENT_COMPLIANCE] 已确认支付合规条款: {data}")
        except NewApiError as exc:
            print(f"RUN ERROR: 支付合规确认失败: {exc}", file=sys.stderr)
            return 2
    else:
        print("[PAYMENT_COMPLIANCE] --check 只读：将在 --apply 时确认支付合规条款（POST /api/option/payment_compliance）")

    # ---- 系统选项 ----
    desired = build_desired_options(cfg)
    current_options: Dict[str, str] = {}
    for item in client.get_options():
        if isinstance(item, dict) and "key" in item:
            current_options[str(item["key"])] = str(item.get("value", ""))

    option_changes: List[str] = []
    for key, value in sorted(desired.items()):
        if key in SENSITIVE_OPTION_KEYS:
            option_changes.append(f"设置 {key} -> {describe_value(key, value)}")
            if apply:
                client.put_option(key, value)
            continue
        cur = current_options.get(key)
        if "." in key:
            # 分层配置（如 general_setting.quota_display_type）：从父配置 JSON 取叶子比较
            cfg_name, leaf = key.split(".", 1)
            try:
                cfg_obj = json.loads(current_options.get(cfg_name, "{}") or "{}")
                cur = str(cfg_obj.get(leaf, ""))
            except (json.JSONDecodeError, AttributeError):
                cur = None
        if cur != value:
            option_changes.append(
                f"设置 {key}: {describe_value(key, cur)} -> {describe_value(key, value)}"
            )
            if apply:
                client.put_option(key, value)

    # ---- 定价 option（合并导入） ----
    pricing = build_pricing_options(pricing_path)
    for key, incoming in sorted(pricing.items()):
        cur = current_options.get(key, "")
        merged = _merge_json_option(cur, incoming)
        if merged != cur:
            option_changes.append(f"合并导入 {key}（覆盖/新增模型，保留其它已有项）")
            if apply:
                client.put_option(key, merged)

    # ---- 渠道 ----
    channel_changes = apply_channels(client, cfg, apply)

    total = len(option_changes) + len(channel_changes)
    mode = "APPLY（已写入）" if apply else "CHECK（未写入，仅核对）"
    print(f"=== {mode} ===")
    print(f"New API: {new_api.base_url}  账号: {new_api.admin_username}  角色: {client.role}")
    print(f"变更项 {total} 个：")
    for c in option_changes + channel_changes:
        print(f"  - {c}")
    if total == 0:
        print("无任何变更，配置已就绪。")

    if not apply and total > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

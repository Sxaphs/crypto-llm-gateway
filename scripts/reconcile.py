#!/usr/bin/env python3
"""三方只读对账：adapter PG ↔ New API ↔ epusdt ↔ 链上。

数据源（字段已对照真实 schema）:
  - adapter PostgreSQL:  payment_orders / rate_snapshots（app/models.py，金额列为 TEXT 存 Decimal）
  - New API v1.0.0-rc.49: top_ups（含 credited_quota）/ users（email）/ logs
  - epusdt:              /admin/api/v1/orders
  - 链上:                TronGrid(TRC20) + Etherscan v2(ERC20)

检查项: email、收/付款地址（payer_address_pending 报差异）、tx、确认时汇率、金额/地址/合约/链状态、
        New API credited_quota == adapter credit_quota、链上入账无订单、重复。
        pending/expired 不会像已付款一样误报。链查询失败不判 FULL PASS。

用法:
    python scripts/reconcile.py [--since 秒] [--until 秒] [--no-chain]

退出码: 0 对平；1 差异；2 运行错误。报告 JSON+CSV 到 REPORT_DIR（文件名带 UTC）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Windows 控制台可能是 GBK；强制 UTF-8 输出，避免中文乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.chain import (  # noqa: E402
    ChainError,
    contract_for,
    query_erc20_transfers,
    query_trc20_transfers,
)
from lib.config import load_reconcile_config  # noqa: E402
from lib.epusdt import EpusdtAdminClient, EpusdtError  # noqa: E402
from lib.newapi import NewApiClient, NewApiError  # noqa: E402
from lib.pg import read_payment_orders, read_rate_snapshots  # noqa: E402
from lib.report import write_report  # noqa: E402

DEFAULT_WINDOW_SECONDS = 24 * 3600
AMOUNT_TOLERANCE = Decimal("0.000001")
CHAIN_AMOUNT_TOLERANCE = Decimal("0")
ADAPTER_SETTLED_STATUSES = {"settled", "notified"}
EPUSDT_PAID_STATUS = 2
NEWAPI_SUCCESS = "success"


def to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def to_epoch(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        return int(value)
    return None


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--since", type=int, default=0, help="起始 Unix 秒")
    p.add_argument("--until", type=int, default=0, help="结束 Unix 秒")
    p.add_argument("--no-chain", action="store_true", help="跳过链上核验（报告 PARTIAL，非 FULL PASS）")
    return p.parse_args(argv)


def fetch_newapi(client: NewApiClient, since: int, until: int) -> Dict[str, Any]:
    topups = client.list_topups()
    by_trade: Dict[str, Dict[str, Any]] = {}
    dups: List[Dict[str, Any]] = []
    for t in topups:
        trade_no = str(t.get("trade_no", "") or "")
        if not trade_no:
            continue
        create_time = int(t.get("create_time", 0) or 0)
        if since and create_time and create_time < since:
            continue
        if until and create_time and create_time > until:
            continue
        if trade_no in by_trade:
            dups.append({"out_trade_no": trade_no, "findings": ["New API top_ups 重复 trade_no"]})
        by_trade[trade_no] = t
    users: Dict[int, Dict[str, Any]] = {}
    for u in client.list_users():
        if u.get("id"):
            users[int(u["id"])] = u
    return {"topups": by_trade, "users": users, "dups": dups}


def fetch_epusdt(client: EpusdtAdminClient, since: int, until: int) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    dups: List[Dict[str, Any]] = []
    page = 1
    while True:
        data = client.list_orders(page=page, page_size=200, start_at=since, end_at=until)
        rows = data["list"]
        if not rows:
            break
        for o in rows:
            key = str(o.get("order_id", "") or "") or str(o.get("trade_id", "") or "")
            if not key:
                continue
            if key in by_key:
                dups.append({"out_trade_no": key, "findings": ["epusdt 重复 order_id"]})
            by_key[key] = o
        if len(rows) < 200:
            break
        page += 1
        if page > 500:
            break
    return by_key, dups


def fetch_adapter(dsn: str, since: int, until: int) -> Dict[str, Any]:
    orders = read_payment_orders(dsn, since=since, until=until)["rows"]
    rates = read_rate_snapshots(dsn, since=since, until=until)["rows"]
    by_trade: Dict[str, Dict[str, Any]] = {}
    dups: List[Dict[str, Any]] = []
    for o in orders:
        key = str(o.get("out_trade_no", "") or "")
        if not key:
            continue
        if key in by_trade:
            dups.append({"out_trade_no": key, "findings": ["adapter payment_orders 重复 out_trade_no"]})
        by_trade[key] = o
    rates_by_order: Dict[int, Dict[str, Any]] = {}
    for r in rates:
        pid = to_int(r.get("payment_order_id"))
        if pid is not None:
            rates_by_order[pid] = r
    return {"orders": by_trade, "rates_by_order": rates_by_order, "dups": dups}


def query_chain(
    address: str,
    network: str,
    token: str,
    cfg: Any,
    since: int,
    until: int,
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """返回 (transfers 或 None, error)。None 表示查询失败/不支持。"""
    network = (network or "").lower()
    token = (token or "").lower()
    if not address:
        return None, "无收款地址"
    try:
        if network == "tron":
            transfers = query_trc20_transfers(
                address,
                contract=contract_for(network, token),
                api_key=cfg.trongrid_api_key,
                min_timestamp=since,
                max_timestamp=until,
            )
        elif network in ("ethereum", "eth", "polygon", "matic"):
            chain_id = 1 if network in ("ethereum", "eth") else 137
            contract = contract_for(network, token, cfg.usdc_polygon_contract)
            transfers = query_erc20_transfers(
                chain_id,
                address,
                contract,
                cfg.etherscan_api_key,
                min_timestamp_sec=since,
                max_timestamp_sec=until,
            )
        else:
            return None, f"不支持的链: {network}"
        return transfers, ""
    except ChainError as exc:
        return None, str(exc)


def match_chain(
    transfers: List[Dict[str, Any]],
    tx_hash: str,
    address: str,
    expected_amount: Optional[Decimal],
) -> List[Dict[str, Any]]:
    """有 tx_hash 时也必须同时匹配 to 地址与金额，不得只凭同 hash 任意转账。"""
    out = []
    for t in transfers:
        if tx_hash and t["tx_hash"].lower() != tx_hash.lower():
            continue
        if address and t["to"].lower() != address.lower():
            continue
        if expected_amount is not None and abs(t["amount"] - expected_amount) > CHAIN_AMOUNT_TOLERANCE:
            continue
        out.append(t)
    return out


def check_rate(order: Dict[str, Any], rate_snapshot: Optional[Dict[str, Any]], cfg: Any, findings: List[str]) -> None:
    for f in ("usd_per_token", "token_per_cny", "usd_per_cny"):
        v = order.get(f)
        if v in (None, ""):
            findings.append(f"adapter 字段缺失: {f}")
    usd_per_token = to_decimal(order.get("usd_per_token"))
    if usd_per_token is not None and usd_per_token <= 0:
        findings.append("usd_per_token 非正")

    observed = order.get("rate_observed_at")
    confirmed = order.get("confirmed_at")
    if isinstance(observed, datetime) and isinstance(confirmed, datetime):
        age = abs((confirmed - observed).total_seconds())
        if cfg.rate_max_age_seconds > 0 and age > cfg.rate_max_age_seconds:
            findings.append(f"确认时汇率时间间隔 {int(age)}s 超限(>{cfg.rate_max_age_seconds})")

    if rate_snapshot is None:
        findings.append("缺少 rate_snapshot 记录")
    else:
        for f in ("usd_per_token", "token_per_cny", "usd_per_cny"):
            o = to_decimal(order.get(f))
            s = to_decimal(rate_snapshot.get(f))
            if s is None or s <= 0:
                findings.append(f"rate_snapshot 字段缺失/非正: {f}")
            elif o is not None and abs(o - s) > AMOUNT_TOLERANCE:
                findings.append(f"order 与 snapshot 汇率不一致: {f}")
        o_src = str(order.get("rate_source") or "")
        s_src = str(rate_snapshot.get("source") or "")
        if o_src and s_src and o_src != s_src:
            findings.append("order 与 snapshot source 不一致")

    # 用 settled_usd * QUOTA_PER_USD（ROUND_HALF_UP）重算 credit_quota 并比对
    settled_usd = to_decimal(order.get("settled_usd"))
    credit_quota = order.get("credit_quota")
    if settled_usd is not None and credit_quota not in (None, ""):
        recomputed = int(
            (settled_usd * Decimal(str(cfg.quota_per_usd))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if recomputed != int(credit_quota):
            findings.append(f"credit_quota 与 settled_usd*QUOTA_PER_USD 不符: order={int(credit_quota)} 重算={recomputed}")


def check_order(
    key: str,
    n: Optional[Dict[str, Any]],
    e: Optional[Dict[str, Any]],
    a: Optional[Dict[str, Any]],
    users: Dict[int, Dict[str, Any]],
    rates_by_order: Dict[int, Dict[str, Any]],
    cfg: Any,
    chain_verified: bool,
    since: int,
    until: int,
    chain_state: Dict[str, Any],
) -> Dict[str, Any]:
    findings: List[str] = []

    newapi_email = ""
    if n is not None:
        uid = to_int(n.get("user_id"))
        if uid is not None and uid in users:
            newapi_email = str(users[uid].get("email", "") or "")

    adapter_settled = a is not None and (
        a.get("confirmed_at") is not None or str(a.get("status", "") or "") in ADAPTER_SETTLED_STATUSES
    )
    epusdt_paid = e is not None and int(e.get("status", 0) or 0) == EPUSDT_PAID_STATUS
    newapi_success = n is not None and str(n.get("status", "") or "") == NEWAPI_SUCCESS

    row: Dict[str, Any] = {
        "out_trade_no": key,
        "newapi_status": (n or {}).get("status", "") if n else "",
        "newapi_credited_quota": to_int((n or {}).get("credited_quota")) if n else None,
        "newapi_money": to_decimal((n or {}).get("money")) if n else None,
        "newapi_amount": to_int((n or {}).get("amount")) if n else None,
        "newapi_email": newapi_email,
        "epusdt_status": (e or {}).get("status", "") if e else "",
        "epusdt_actual_amount": to_decimal((e or {}).get("actual_amount")) if e else None,
        "epusdt_receive_address": (e or {}).get("receive_address", "") if e else "",
        "epusdt_token": (e or {}).get("token", "") if e else "",
        "epusdt_network": (e or {}).get("network", "") if e else "",
        "epusdt_tx": (e or {}).get("block_transaction_id", "") if e else "",
        "adapter_status": (a or {}).get("status", "") if a else "",
        "adapter_email": (a or {}).get("email", "") if a else "",
        "adapter_money_cny": to_decimal((a or {}).get("money_cny")) if a else None,
        "adapter_actual_amount": to_decimal((a or {}).get("actual_amount")) if a else None,
        "adapter_receive_address": (a or {}).get("receive_address", "") if a else "",
        "adapter_payer_address": (a or {}).get("payer_address", "") if a else "",
        "adapter_payer_pending": bool((a or {}).get("payer_address_pending")) if a else None,
        "adapter_tx": (a or {}).get("block_transaction_id", "") if a else "",
        "adapter_credit_quota": to_int((a or {}).get("credit_quota")) if a else None,
        "adapter_settled_usd": to_decimal((a or {}).get("settled_usd")) if a else None,
        "adapter_usd_per_token": to_decimal((a or {}).get("usd_per_token")) if a else None,
        "adapter_confirmed_at": str(a.get("confirmed_at") or "") if a else "",
        "adapter_confirmation_time_source": str(a.get("confirmation_time_source") or "") if a else "",
        "adapter_callback_received_at": str(a.get("callback_received_at") or "") if a else "",
        "adapter_notified_at": str(a.get("notified_at") or "") if a else "",
        "chain_found": None,
        "chain_note": "",
        "findings": findings,
    }

    # ---- 三方存在性 ----
    # 仅 New API pending 且尚未进入 adapter 的订单不报缺失；支付/结算相关状态才报。
    not_yet_started = (
        a is None and e is None and n is not None and str(n.get("status", "") or "") == "pending"
    )
    if not not_yet_started:
        if a is None:
            findings.append("adapter 无 payment_orders 记录")
        if e is None and (a is not None or newapi_success):
            findings.append("epusdt 无对应订单")

    # ---- email ----
    if a is not None:
        if not a.get("email"):
            findings.append("adapter 字段缺失: email")
        elif newapi_email and str(a["email"]).lower() != newapi_email.lower():
            findings.append(f"email 不一致: adapter={a['email']} NewAPI={newapi_email}")

    # ---- 收/付款地址 / tx / token / network / amount ----
    if a is not None and e is not None:
        a_addr = str(a.get("receive_address", "") or "").lower()
        e_addr = str(e.get("receive_address", "") or "").lower()
        if a_addr and e_addr and a_addr != e_addr:
            findings.append(f"收款地址不一致 adapter vs epusdt")
        if adapter_settled and not a_addr:
            findings.append("adapter 字段缺失: receive_address")
        a_tx = str(a.get("block_transaction_id", "") or "").lower()
        e_tx = str(e.get("block_transaction_id", "") or "").lower()
        if a_tx and e_tx and a_tx != e_tx:
            findings.append("tx 不一致 adapter vs epusdt")
        if str(a.get("token", "") or "").lower() and str(e.get("token", "") or "").lower() and str(a["token"]).lower() != str(e["token"]).lower():
            findings.append("token 不一致 adapter vs epusdt")
        if str(a.get("network", "") or "").lower() and str(e.get("network", "") or "").lower() and str(a["network"]).lower() != str(e["network"]).lower():
            findings.append("network 不一致 adapter vs epusdt")
        a_amt = row["adapter_actual_amount"]
        e_amt = row["epusdt_actual_amount"]
        if a_amt is not None and e_amt is not None and abs(a_amt - e_amt) > AMOUNT_TOLERANCE:
            findings.append(f"金额不一致 adapter={a_amt} epusdt={e_amt}")

    # ---- payer pending（仅 adapter 已结算时才报，首次结算前链上补 payer 属正常） ----
    if adapter_settled and a is not None and bool(a.get("payer_address_pending")):
        findings.append("payer_address_pending 未确定（应报差异）")

    # ---- 状态迁移 ----
    if epusdt_paid and not adapter_settled:
        findings.append("epusdt 已支付但 adapter 未结算")
    if adapter_settled and not newapi_success:
        findings.append("adapter 已结算但 New API 非 success（订单无 New API 成功）")
    if newapi_success and not adapter_settled:
        findings.append("New API success 但 adapter 未结算")

    # ---- credit_quota / money ----
    if adapter_settled and newapi_success:
        if a.get("credit_quota") in (None, ""):
            findings.append("adapter 字段缺失: credit_quota")
        elif n.get("credited_quota") in (None, ""):
            findings.append("New API 字段缺失: credited_quota")
        elif int(a["credit_quota"]) != int(n["credited_quota"]):
            findings.append(f"credit_quota 不一致: NewAPI={n['credited_quota']} adapter={a['credit_quota']}")
        n_money = to_decimal((n or {}).get("money"))
        settled_usd = to_decimal((a or {}).get("settled_usd"))
        if n_money is not None and settled_usd is not None and abs(n_money - settled_usd) > AMOUNT_TOLERANCE:
            findings.append(f"New API money({n_money}) != adapter settled_usd({settled_usd})")

    # ---- 确认时汇率 ----
    if adapter_settled:
        rs = rates_by_order.get(to_int(a.get("id"))) if a is not None else None
        check_rate(a or {}, rs, cfg, findings)

    # ---- 链上 ----
    if chain_verified and (adapter_settled or epusdt_paid):
        addr = str((a or {}).get("receive_address", "") or (e or {}).get("receive_address", "") or "")
        network = str((a or {}).get("network", "") or (e or {}).get("network", "") or "")
        token = str((a or {}).get("token", "") or (e or {}).get("token", "") or "")
        tx = str((a or {}).get("block_transaction_id", "") or (e or {}).get("block_transaction_id", "") or "")
        expect = row["adapter_actual_amount"] or row["epusdt_actual_amount"]
        transfers, err = query_chain(addr, network, token, cfg, since, until)
        if err:
            chain_state["complete"] = False
            row["chain_note"] = f"链上查询失败: {err}"
            if adapter_settled:
                findings.append("链上核验失败，无法确认（非 FULL PASS）")
        else:
            matched = match_chain(transfers, tx, addr, expect)
            row["chain_found"] = bool(matched)
            if matched:
                t = matched[0]
                row["chain_amount"] = str(t["amount"])
                row["chain_from"] = t["from"]
                row["chain_to"] = t["to"]
                row["chain_tx"] = t["tx_hash"]
                row["chain_token"] = t["token"]
                # 付款地址一致性
                payer = str((a or {}).get("payer_address", "") or "")
                if payer and t["from"].lower() != payer.lower():
                    findings.append(f"付款地址不一致 adapter.payer={payer} chain.from={t['from']}")
                # 合约/链状态
                if not t["confirmed"]:
                    findings.append("链上转账未确认")
            else:
                row["chain_note"] = "未找到匹配转账"
                if adapter_settled:
                    findings.append("adapter 已结算但链上无对应转账（链上无订单）")
    elif not chain_verified:
        row["chain_note"] = "skipped(--no-chain)"

    return row


def wallet_inbound_check(
    wallets: List[Dict[str, str]],
    adapter_orders: Dict[str, Dict[str, Any]],
    cfg: Any,
    since: int,
    until: int,
    chain_state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """检测配置钱包地址的链上入账是否有对应订单（时间窗过滤）。"""
    findings: List[Dict[str, Any]] = []
    if not wallets:
        return findings
    known_txs = {str(o.get("block_transaction_id", "") or "").lower() for o in adapter_orders.values()}
    seen_wallets = set()
    for w in wallets:
        addr = str(w.get("address", "") or "").strip()
        network = str(w.get("network", "") or "").lower()
        wallet_key = (network, addr.lower())
        if not addr or not network or wallet_key in seen_wallets:
            continue
        seen_wallets.add(wallet_key)
        token = "usdt" if network == "tron" else "usdc"
        transfers, err = query_chain(addr, network, token, cfg, since, until)
        if err:
            chain_state["complete"] = False
            continue
        for t in transfers:
            # 只按已知 tx hash 排除，不得用同地址同金额吞掉无订单入账
            if t["tx_hash"].lower() in known_txs:
                continue
            findings.append(
                {
                    "out_trade_no": f"wallet:{addr}",
                    "findings": [f"链上入账无订单: tx={t['tx_hash']} amount={t['amount']} to={addr}"],
                }
            )
    return findings


def run(argv: List[str]) -> int:
    args = parse_args(argv)
    cfg = load_reconcile_config()

    now = int(time.time())
    since = args.since or (now - DEFAULT_WINDOW_SECONDS)
    until = args.until or now
    chain_verified = not args.no_chain

    if not cfg.adapter_pg_dsn:
        print("RUN ERROR: 未设置 ADAPTER_PG_DSN / DATABASE_URL", file=sys.stderr)
        return 2
    if not cfg.new_api.admin_password:
        print("RUN ERROR: 未设置 NEW_API_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    if not cfg.epusdt_base_url:
        print("RUN ERROR: 未设置 EPUSDT_BASE_URL", file=sys.stderr)
        return 2

    # ---- 读三份数据 ----
    newapi_client = NewApiClient(
        cfg.new_api.base_url,
        basic_auth_username=cfg.new_api.basic_auth_username,
        basic_auth_password=cfg.new_api.basic_auth_password,
    )
    try:
        newapi_client.connect(cfg.new_api.admin_username, cfg.new_api.admin_password)
        newapi = fetch_newapi(newapi_client, since, until)
    except NewApiError as exc:
        print(f"RUN ERROR: New API 读取失败: {exc}", file=sys.stderr)
        return 2

    epusdt_client = EpusdtAdminClient(cfg.epusdt_base_url)
    wallets: List[Dict[str, str]] = []
    wallets_ok = False
    try:
        epusdt_client.login(cfg.epusdt_admin_username, cfg.epusdt_admin_password)
        epusdt_views, epusdt_dups = fetch_epusdt(epusdt_client, since, until)
        try:
            for w in epusdt_client.list_wallets():
                if w.get("address"):
                    wallets.append({"address": w["address"], "network": w.get("network", "") or ""})
            wallets_ok = True
        except EpusdtError:
            wallets_ok = False
    except EpusdtError as exc:
        print(f"RUN ERROR: epusdt 读取失败: {exc}", file=sys.stderr)
        return 2

    try:
        adapter = fetch_adapter(cfg.adapter_pg_dsn, since, until)
    except Exception as exc:  # noqa: BLE001
        print(f"RUN ERROR: adapter PG 读取失败: {exc}", file=sys.stderr)
        return 2

    # ---- 统一订单 key ----
    keys: List[str] = []
    seen = set()
    for k in list(newapi["topups"].keys()) + list(epusdt_views.keys()) + list(adapter["orders"].keys()):
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    keys.sort()

    chain_state: Dict[str, Any] = {"complete": True}
    # 全链对账必须拿到 epusdt 钱包列表；失败或无钱包不能判 FULL PASS
    if chain_verified and (not wallets_ok or not wallets):
        chain_state["complete"] = False
        chain_state["wallet_note"] = "epusdt 钱包列表缺失或获取失败"

    rows: List[Dict[str, Any]] = []
    for key in keys:
        rows.append(
            check_order(
                key,
                newapi["topups"].get(key),
                epusdt_views.get(key),
                adapter["orders"].get(key),
                newapi["users"],
                adapter["rates_by_order"],
                cfg,
                chain_verified,
                since,
                until,
                chain_state,
            )
        )

    # ---- 链上入账无订单检测 ----
    wallet_findings = []
    if chain_verified:
        wallet_findings = wallet_inbound_check(wallets, adapter["orders"], cfg, since, until, chain_state)

    # ---- 重复 ----
    dup_findings = newapi["dups"] + epusdt_dups + adapter["dups"]

    diff_rows = [r for r in rows if r.get("findings")] + [
        r for r in (wallet_findings + dup_findings) if r.get("findings")
    ]
    balanced = len(diff_rows) == 0
    chain_complete = chain_verified and chain_state["complete"]

    fieldnames = [
        "out_trade_no", "newapi_status", "newapi_credited_quota", "newapi_money",
        "newapi_amount", "newapi_email", "epusdt_status", "epusdt_actual_amount",
        "epusdt_receive_address", "epusdt_token", "epusdt_network", "epusdt_tx",
        "adapter_status", "adapter_email", "adapter_money_cny", "adapter_actual_amount",
        "adapter_receive_address", "adapter_payer_address", "adapter_payer_pending",
        "adapter_tx", "adapter_credit_quota", "adapter_settled_usd", "adapter_usd_per_token",
        "adapter_confirmed_at", "adapter_confirmation_time_source", "adapter_callback_received_at",
        "adapter_notified_at", "chain_found", "chain_note",
        "findings",
    ]
    summary = {
        "window_since": since,
        "window_until": until,
        "chain_verified": chain_verified,
        "chain_complete": chain_complete,
        "wallet_note": chain_state.get("wallet_note", ""),
        "total_orders": len(keys),
        "balanced": balanced,
        "difference_count": len(diff_rows),
        "quota_per_usd": cfg.quota_per_usd,
    }
    all_rows = rows + wallet_findings + dup_findings
    paths = write_report(cfg.report_dir, "reconcile", summary, all_rows, fieldnames)

    if balanced and chain_complete:
        status_word = "FULL PASS（对平）"
    elif balanced:
        status_word = "PARTIAL（链上未核验/查询未完整，非 FULL PASS）"
    else:
        status_word = "差异"
    print(f"=== 对账结果: {status_word} ===")
    print(f"窗口: {since} ~ {until}  订单 {len(keys)}  差异 {len(diff_rows)}  链上完整: {chain_complete}")
    print(f"报告: {paths['json']}")
    print(f"报告: {paths['csv']}")
    for r in diff_rows:
        print(f"  - {r['out_trade_no']}: {'; '.join(r['findings'])}")

    if not balanced:
        lines = [f"对账发现 {len(diff_rows)} 项差异（{utc_iso()}）"]
        for r in diff_rows[:20]:
            lines.append(f"- {r['out_trade_no']}: {'; '.join(r['findings'])}")
        if len(diff_rows) > 20:
            lines.append(f"... 其余 {len(diff_rows)-20} 项见报告")
        send_telegram(cfg.tg_bot_token, cfg.tg_chat_id, "\n".join(lines))
        return 1
    return 0


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=10)
    except Exception:  # noqa: BLE001
        pass


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

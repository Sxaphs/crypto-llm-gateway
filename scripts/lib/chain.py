"""链上查询：TronGrid(TRC20) 与 Etherscan v2(ERC20)。

只读，只做转账查询与归一化；绝不写库、绝不补单。金额统一用 decimal.Decimal。
时间参数一律为 Unix 秒；TronGrid 内部毫秒换算在本模块完成。

TRC20 USDT 主合约: TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t
ERC20 USDC 主合约: 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 (Ethereum chainid=1)
Polygon USDC 原生合约: 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359 (chainid=137)
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Dict, List

TRONGRID_BASE = "https://api.trongrid.io"
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"

TRC20_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
ERC20_USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
ERC20_USDC_POLYGON = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"


class ChainError(RuntimeError):
    pass


def _http_get_json(url: str, timeout: int = 30) -> Dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise ChainError(f"链上查询失败: {url}: {exc}") from exc


def query_trc20_transfers(
    address: str,
    contract: str = TRC20_USDT_CONTRACT,
    api_key: str = "",
    min_timestamp: int = 0,
    max_timestamp: int = 0,
    limit: int = 200,
) -> List[Dict]:
    """查询某地址的 TRC20 转账（USDT），返回归一化后的记录列表。"""
    params: Dict[str, str] = {
        "only_confirmed": "true",
        "limit": str(limit),
        "contract_address": contract,
    }
    if min_timestamp:
        params["min_timestamp"] = str(min_timestamp * 1000)  # TronGrid 用毫秒
    if max_timestamp:
        params["max_timestamp"] = str(max_timestamp * 1000)
    qs = urllib.parse.urlencode(params)
    url = f"{TRONGRID_BASE}/v1/accounts/{address}/transactions/trc20?{qs}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise ChainError(f"TronGrid 查询失败: {address}: {exc}") from exc

    out: List[Dict] = []
    for item in body.get("data", []):
        token_info = item.get("token_info") or {}
        decimals = int(token_info.get("decimals", 6) or 6)
        raw_value = item.get("value") or "0"
        try:
            amount = Decimal(str(raw_value)) / (Decimal(10) ** decimals)
        except Exception:  # noqa: BLE001
            amount = Decimal("0")
        out.append(
            {
                "chain": "tron",
                "tx_hash": item.get("transaction_id", ""),
                "from": item.get("from", ""),
                "to": item.get("to", ""),
                "contract": token_info.get("address", contract),
                "token": (token_info.get("symbol") or "USDT").upper(),
                "amount": amount,
                "confirmed": bool(item.get("confirmed", False)),
                "block_timestamp": int(item.get("block_timestamp", 0) or 0),
                "block_number": int(item.get("block_number", 0) or 0),
            }
        )
    return out


def query_erc20_transfers(
    chain_id: int,
    address: str,
    contract: str,
    api_key: str,
    start_block: int = 0,
    end_block: int = 99999999,
    offset: int = 100,
    min_timestamp_sec: int = 0,
    max_timestamp_sec: int = 0,
) -> List[Dict]:
    """通过 Etherscan v2 查询某地址的 ERC20 转账，返回归一化后的记录列表。
    时间过滤（Unix 秒）在取回后按 timeStamp 二次过滤。"""
    params: Dict[str, str] = {
        "chainid": str(chain_id),
        "module": "account",
        "action": "tokentx",
        "address": address,
        "contractaddress": contract,
        "startblock": str(start_block),
        "endblock": str(end_block),
        "page": "1",
        "offset": str(offset),
        "sort": "desc",
        "apikey": api_key,
    }
    qs = urllib.parse.urlencode(params)
    url = f"{ETHERSCAN_V2_BASE}?{qs}"
    body = _http_get_json(url)
    if body.get("status") != "1":
        raise ChainError(f"Etherscan 查询失败: {body.get('message', body.get('result', ''))}")
    out: List[Dict] = []
    for item in body.get("result", []):
        decimals = int(item.get("tokenDecimal", 18) or 18)
        ts = int(item.get("timeStamp", 0) or 0)
        if min_timestamp_sec and ts and ts < min_timestamp_sec:
            continue
        if max_timestamp_sec and ts and ts > max_timestamp_sec:
            continue
        try:
            amount = Decimal(str(item.get("value") or "0")) / (Decimal(10) ** decimals)
        except Exception:  # noqa: BLE001
            amount = Decimal("0")
        out.append(
            {
                "chain": "ethereum" if chain_id == 1 else "polygon",
                "tx_hash": item.get("hash", ""),
                "from": item.get("from", ""),
                "to": item.get("to", ""),
                "contract": item.get("contractAddress", contract),
                "token": (item.get("tokenSymbol") or "").upper(),
                "amount": amount,
                "confirmed": int(item.get("confirmations", 0) or 0) > 0,
                "block_timestamp": ts,
                "block_number": int(item.get("blockNumber", 0) or 0),
            }
        )
    return out


def contract_for(network: str, token: str, polygon_contract: str = ERC20_USDC_POLYGON) -> str:
    network = (network or "").lower()
    token = (token or "").lower()
    if network == "tron" or network == "trc20":
        return TRC20_USDT_CONTRACT
    if network in ("ethereum", "eth", "erc20"):
        return ERC20_USDC_ETH
    if network in ("polygon", "matic"):
        return polygon_contract
    return ""

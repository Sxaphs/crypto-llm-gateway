from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from .clients import UpstreamError, _json_response
from .config import Settings

TRON_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
ETHEREUM_USDC_CONTRACT = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
TOKEN_DECIMALS = 6


def _atomic_amount(actual_amount: Decimal) -> str:
    atomic = actual_amount * (Decimal(10) ** TOKEN_DECIMALS)
    if not atomic.is_finite() or atomic <= 0 or atomic != atomic.to_integral_value():
        raise UpstreamError("actual_amount cannot be represented exactly at six decimals")
    return str(int(atomic))


def _single_match(rows: list[dict[str, Any]], matches: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise UpstreamError("chain transaction query returned no transfers")
    if not matches:
        raise UpstreamError("no chain transfer matches tx, token, recipient and amount")
    if len(matches) != 1:
        raise UpstreamError("chain transaction has ambiguous matching transfers")
    payer = matches[0].get("from")
    if not isinstance(payer, str) or not payer.strip():
        raise UpstreamError("matching chain transfer has no payer address")
    return matches[0]


class ChainClient:
    """Read-only confirmed TRC20/EVM token transfer lookup for settlement audit."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.http = httpx.Client(timeout=settings.http_timeout_seconds, transport=transport)

    def close(self) -> None:
        self.http.close()

    def _get(self, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.http.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamError("chain transaction query failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise UpstreamError(f"chain transaction query returned HTTP {response.status_code}")
        return _json_response(response)

    def get_payer(
        self,
        token: str,
        network: str,
        receive_address: str,
        tx_hash: str,
        actual_amount: Decimal,
    ) -> str:
        expected_value = _atomic_amount(actual_amount)
        if token.lower() == "usdt" and network.lower() == "tron":
            return self._tron_payer(receive_address, tx_hash, expected_value)
        if token.lower() == "usdc" and network.lower() == "ethereum":
            return self._evm_payer(1, ETHEREUM_USDC_CONTRACT, receive_address, tx_hash, expected_value)
        if token.lower() == "usdc" and network.lower() == "polygon":
            return self._evm_payer(
                137, self.settings.usdc_polygon_contract, receive_address, tx_hash, expected_value
            )
        raise UpstreamError("unsupported chain/token for payer lookup")

    def _tron_payer(self, receive_address: str, tx_hash: str, expected_value: str) -> str:
        headers = {}
        if self.settings.trongrid_api_key:
            headers["TRON-PRO-API-KEY"] = self.settings.trongrid_api_key
        payload = self._get(
            f"https://api.trongrid.io/v1/accounts/{quote(receive_address, safe='')}/transactions/trc20",
            params={
                "only_confirmed": "true",
                "contract_address": TRON_USDT_CONTRACT,
                "limit": "200",
            },
            headers=headers,
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise UpstreamError("TronGrid transfer response is invalid")
        matches = []
        for row in rows:
            token_info = row.get("token_info")
            contract_matches = isinstance(token_info, dict) and str(
                token_info.get("address", "")
            ).lower() == TRON_USDT_CONTRACT.lower()
            if (
                row.get("transaction_id") == tx_hash
                and row.get("to") == receive_address
                and str(row.get("value", "")) == expected_value
                and contract_matches
            ):
                matches.append(row)
        return _single_match(rows, matches)["from"].strip()

    def _evm_payer(
        self,
        chain_id: int,
        contract: str,
        receive_address: str,
        tx_hash: str,
        expected_value: str,
    ) -> str:
        params = {
            "chainid": str(chain_id),
            "module": "account",
            "action": "tokentx",
            "address": receive_address,
            "page": "1",
            "offset": "200",
            "sort": "desc",
        }
        if self.settings.etherscan_api_key:
            params["apikey"] = self.settings.etherscan_api_key
        payload = self._get("https://api.etherscan.io/v2/api", params=params)
        rows = payload.get("result")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise UpstreamError("Etherscan transfer response is invalid")
        if payload.get("status") not in (None, "1", 1, Decimal(1)):
            raise UpstreamError("Etherscan transfer query was unsuccessful")
        matches = [
            row
            for row in rows
            if str(row.get("hash", "")).lower() == tx_hash.lower()
            and str(row.get("contractAddress", "")).lower() == contract.lower()
            and str(row.get("to", "")).lower() == receive_address.lower()
            and str(row.get("value", "")) == expected_value
            and str(row.get("tokenDecimal", "")) == str(TOKEN_DECIMALS)
        ]
        return _single_match(rows, matches)["from"].strip()

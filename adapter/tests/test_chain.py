from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.chain import (
    ChainClient,
    ETHEREUM_USDC_CONTRACT,
    TRON_USDT_CONTRACT,
)
from app.clients import UpstreamError


def test_trongrid_lookup_matches_confirmed_usdt_transfer(settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "transaction_id": "tx-tron",
                        "to": "TReceiveAddress",
                        "from": "TRealPayer",
                        "value": "10000000",
                        "token_info": {"address": TRON_USDT_CONTRACT},
                    }
                ]
            },
        )

    client = ChainClient(settings, transport=httpx.MockTransport(handler))
    payer = client.get_payer("usdt", "tron", "TReceiveAddress", "tx-tron", Decimal("10"))
    assert payer == "TRealPayer"
    request = seen[0]
    assert request.url.path == "/v1/accounts/TReceiveAddress/transactions/trc20"
    assert request.url.params["only_confirmed"] == "true"
    assert request.url.params["contract_address"] == TRON_USDT_CONTRACT
    client.close()


@pytest.mark.parametrize(
    "network,chain_id,contract",
    [
        ("ethereum", "1", ETHEREUM_USDC_CONTRACT),
        ("polygon", "137", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
    ],
)
def test_etherscan_v2_lookup_matches_usdc_transfer(settings, network, chain_id, contract):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": "1",
                "message": "OK",
                "result": [
                    {
                        "hash": "0xabc",
                        "to": "0xreceive",
                        "from": "0xpayer",
                        "value": "10000001",
                        "contractAddress": contract,
                        "tokenDecimal": "6",
                    }
                ],
            },
        )

    client = ChainClient(settings, transport=httpx.MockTransport(handler))
    payer = client.get_payer("usdc", network, "0xreceive", "0xabc", Decimal("10.000001"))
    assert payer == "0xpayer"
    assert seen[0].url.path == "/v2/api"
    assert seen[0].url.params["chainid"] == chain_id
    assert seen[0].url.params["module"] == "account"
    assert seen[0].url.params["action"] == "tokentx"
    client.close()


@pytest.mark.parametrize("field,value", [("value", "999"), ("to", "TWrong"), ("from", "")])
def test_tron_transfer_mismatch_or_missing_payer_fails(settings, field, value):
    transfer = {
        "transaction_id": "tx-tron",
        "to": "TReceiveAddress",
        "from": "TRealPayer",
        "value": "10000000",
        "token_info": {"address": TRON_USDT_CONTRACT},
    }
    transfer[field] = value
    client = ChainClient(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [transfer]})),
    )
    with pytest.raises(UpstreamError):
        client.get_payer("usdt", "tron", "TReceiveAddress", "tx-tron", Decimal("10"))
    client.close()


def test_evm_wrong_contract_fails(settings):
    row = {
        "hash": "0xabc",
        "to": "0xreceive",
        "from": "0xpayer",
        "value": "10000000",
        "contractAddress": "0xwrong",
        "tokenDecimal": "6",
    }
    client = ChainClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "1", "result": [row]})
        ),
    )
    with pytest.raises(UpstreamError, match="no chain transfer matches"):
        client.get_payer("usdc", "ethereum", "0xreceive", "0xabc", Decimal("10"))
    client.close()

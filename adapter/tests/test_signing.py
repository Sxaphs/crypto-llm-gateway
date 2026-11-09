from decimal import Decimal

import pytest

from app.signing import SignatureValueError, canonical_params, decimal_to_plain, epay_md5, gmpay_hmac


def test_official_gmpay_hmac_vector():
    params = {
        "pid": "1000",
        "order_id": "ORD202605230001",
        "currency": "cny",
        "token": "usdt",
        "network": "tron",
        "amount": "100",
        "notify_url": "https://merchant.example/notify",
        "redirect_url": "https://merchant.example/return",
        "name": "VIP",
    }
    assert canonical_params(params, frozenset({"signature"})) == (
        "amount=100&currency=cny&name=VIP&network=tron&"
        "notify_url=https://merchant.example/notify&order_id=ORD202605230001&"
        "pid=1000&redirect_url=https://merchant.example/return&token=usdt"
    )
    assert gmpay_hmac(params, "epusdt_secret_key") == (
        "6f874b1919d95081835e2809b620e354a5866f5a6dbb2e432d1627f1eb10059d"
    )


def test_new_api_patch_cross_language_notification_vector():
    # Shared with the Go New API patch. Independently checked with:
    # printf %s "$canonical" | openssl dgst -sha256 -hmac new-api-hmac-test-key
    params = {
        "pid": "1000",
        "out_trade_no": "ORDER-001",
        "trade_id": "TRADE-001",
        "trade_status": "TRADE_SUCCESS",
        "type": "usdt.tron",
        "money": "0.2",
        "credit_quota": "100000",
        "settled_usd": "0.2",
        "actual_amount": "10",
        "usd_per_token": "0.02",
        "block_transaction_id": "tx-abc-123",
    }
    canonical = canonical_params(params, frozenset({"signature"}))
    assert canonical == (
        "actual_amount=10&block_transaction_id=tx-abc-123&credit_quota=100000&"
        "money=0.2&out_trade_no=ORDER-001&pid=1000&settled_usd=0.2&"
        "trade_id=TRADE-001&trade_status=TRADE_SUCCESS&type=usdt.tron&usd_per_token=0.02"
    )
    assert not canonical.endswith("&")
    assert gmpay_hmac(params, "new-api-hmac-test-key") == (
        "3ca187dee0ab715be166e5770f0be60722a2cb714c24409b4edc22ecc4d1653f"
    )


def test_official_epay_md5_vector():
    params = {
        "money": "100",
        "name": "VIP",
        "notify_url": "https://merchant.example/notify",
        "out_trade_no": "ORD202605230001",
        "pid": "1000",
        "return_url": "https://merchant.example/return",
        "type": "alipay",
        "sign": "ignored",
        "sign_type": "MD5",
    }
    assert epay_md5(params, "epusdt_secret_key") == "b865b0acbb2b01554c35a1bd33351452"


def test_json_decimal_normalization_preserves_precision_and_empty_rules():
    params = {
        "amount": Decimal("100.00"),
        "actual_amount": Decimal("14.2900"),
        "small": Decimal("0.000000000000000000123400"),
        "large": Decimal("12345678901234567890.123456789"),
        "zero": Decimal("-0.00"),
        "empty": "",
        "none": None,
        "signature": "excluded",
    }
    assert canonical_params(params, frozenset({"signature"})) == (
        "actual_amount=14.29&amount=100&large=12345678901234567890.123456789&"
        "small=0.0000000000000000001234&zero=0"
    )
    assert decimal_to_plain(Decimal("1E+3")) == "1000"
    with pytest.raises(SignatureValueError):
        decimal_to_plain(Decimal("NaN"))

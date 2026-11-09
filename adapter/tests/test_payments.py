from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.clients import UpstreamError
from app.db import Database
from app.main import create_app
from app.models import CallbackEvent, PaymentOrder, RateSnapshot
from app.signing import epay_md5, gmpay_hmac
from fastapi.testclient import TestClient


def submit_params(sign: bool = True) -> dict[str, str]:
    params = {
        "pid": "1000",
        "money": "100.00",
        "out_trade_no": "ORDER-001",
        "notify_url": "http://new-api/api/user/epay/notify",
        "return_url": "http://new-api/usage-logs",
        "name": "TUC100",
        "type": "usdt.tron",
        "sign_type": "MD5",
    }
    if sign:
        params["sign"] = epay_md5(params, "epay-md5-test-key")
    return params


def make_callback() -> dict:
    payload = {
        "pid": "1000",
        "trade_id": "TRADE-001",
        "order_id": "ORDER-001",
        "amount": 100,
        "actual_amount": 10,
        "receive_address": "TReceiveAddress",
        "token": "USDT",
        "block_transaction_id": "tx-abc-123",
        "status": 2,
    }
    payload["signature"] = gmpay_hmac(payload, "gmpay-test-key")
    return payload


def callback_content(payload: dict) -> bytes:
    return json.dumps(payload, default=str, separators=(",", ":")).encode()


def create_order(client) -> None:
    response = client.post("/crypto-pay/submit.php", data=submit_params(), follow_redirects=False)
    assert response.status_code == 302, response.text


def test_md5_submit_creates_order_and_duplicate_returns_same_cashier(client, epusdt):
    response = client.post("/crypto-pay/submit.php", data=submit_params(), follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://pay.example.com/cashier/TRADE-001"
    assert epusdt.last_create_fields["notify_url"] == "http://payment-adapter:8080/callbacks/gmpay"
    assert epusdt.last_create_fields["currency"] == "cny"

    duplicate = client.get("/crypto-pay/submit.php", params=submit_params(), follow_redirects=False)
    assert duplicate.status_code == 302
    assert duplicate.headers["location"] == response.headers["location"]
    assert epusdt.create_calls == 1


def test_md5_submit_rejects_bad_signature_and_cross_order_money(client, epusdt):
    bad = submit_params()
    bad["sign"] = "0" * 32
    assert client.post("/crypto-pay/submit.php", data=bad).status_code == 401

    forged = submit_params()
    forged["money"] = "1"
    forged["sign"] = epay_md5(forged, "epay-md5-test-key")
    response = client.post("/crypto-pay/submit.php", data=forged)
    assert response.status_code == 400
    assert "money" in response.json()["detail"]
    assert epusdt.create_calls == 0


def test_callback_rejects_bad_hmac_and_missing_fields(client, epusdt, notifier):
    create_order(client)
    payload = make_callback()
    payload["signature"] = "0" * 64
    response = client.post("/callbacks/gmpay", content=callback_content(payload))
    assert response.status_code == 401
    del payload["signature"]
    assert client.post("/callbacks/gmpay", content=callback_content(payload)).status_code == 401
    assert epusdt.detail_calls == 0
    assert notifier.calls == 0


def test_callback_rejects_wrong_source_ip_before_processing(settings, new_api_row, epusdt, chain, notifier):
    app_settings = SettingsWithIP(settings, "10.0.0.9")
    app = create_app(settings=app_settings, epusdt_client=epusdt, chain_client=chain, notifier=notifier)
    with TestClient(app) as other_client:
        response = other_client.post("/callbacks/gmpay", content=callback_content(make_callback()))
    assert response.status_code == 403
    assert epusdt.detail_calls == 0


def SettingsWithIP(settings, ip):
    from dataclasses import replace

    return replace(settings, epusdt_container_ip=ip)


def test_non_one_to_one_rate_exact_settlement_and_audit(client, settings, epusdt, notifier):
    create_order(client)
    payload = make_callback()
    response = client.post("/callbacks/gmpay", content=callback_content(payload))
    assert response.status_code == 200, response.text
    assert response.text == "ok"

    database = Database(settings.database_url)
    with database.session() as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.out_trade_no == "ORDER-001"))
        assert order.email == "payer@example.com"
        assert order.receive_address == "TReceiveAddress"
        assert order.payer_address == "TPayerAddress123"
        assert order.payer_address_pending is False
        assert order.token_per_cny == Decimal("7")
        assert order.usd_per_cny == Decimal("0.14")
        assert order.usd_per_token == Decimal("0.02")
        assert order.settled_usd == Decimal("0.2")
        assert order.credit_quota == 100000
        assert order.block_transaction_id == "tx-abc-123"
        assert order.confirmed_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 11, 9, 14, 58, tzinfo=timezone.utc
        )
        assert order.confirmation_time_source == "epusdt.updated_at"
        assert order.callback_received_at is not None
        assert order.notified_at is not None
        snapshot = session.scalar(select(RateSnapshot).where(RateSnapshot.payment_order_id == order.id))
        assert snapshot.source == "https://rates.example.com/cny.json"
        assert snapshot.observed_at is not None
        event = session.scalar(select(CallbackEvent).where(CallbackEvent.payment_order_id == order.id))
        assert event.raw_sha256 == hashlib.sha256(callback_content(payload)).hexdigest()
    database.dispose()
    assert notifier.last_fields["money"] == Decimal("0.2")
    assert notifier.last_fields["credit_quota"] == 100000


def test_missing_epusdt_confirmation_time_uses_callback_receipt(client, settings, epusdt):
    create_order(client)
    original_get_order = epusdt.get_order

    def without_updated_at(trade_id):
        detail = original_get_order(trade_id)
        del detail["updated_at"]
        return detail

    epusdt.get_order = without_updated_at
    assert client.post("/callbacks/gmpay", content=callback_content(make_callback())).status_code == 200
    database = Database(settings.database_url)
    with database.session() as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.out_trade_no == "ORDER-001"))
        assert order.confirmation_time_source == "callback_received_at"
        assert order.confirmed_at == order.callback_received_at
        assert order.payer_address == "TPayerAddress123"
        assert order.payer_address_pending is False
    database.dispose()


def test_duplicate_callback_does_not_reprice_or_renotify(client, epusdt, chain, notifier):
    create_order(client)
    payload = make_callback()
    content = callback_content(payload)
    assert client.post("/callbacks/gmpay", content=content).status_code == 200
    epusdt.rates = {"usd": Decimal("99"), "usdt": Decimal("1")}
    assert client.post("/callbacks/gmpay", content=content).status_code == 200
    assert epusdt.rate_calls == 1
    assert epusdt.detail_calls == 1
    assert chain.calls == 1
    assert notifier.calls == 1


def test_notification_failure_is_retryable_without_repricing(client, settings, epusdt, chain, notifier):
    create_order(client)
    notifier.fail = True
    content = callback_content(make_callback())
    first = client.post("/callbacks/gmpay", content=content)
    assert first.status_code == 503
    database = Database(settings.database_url)
    with database.session() as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.out_trade_no == "ORDER-001"))
        assert order.confirmed_at is not None
        assert order.notified_at is None
    database.dispose()

    notifier.fail = False
    second = client.post("/callbacks/gmpay", content=content)
    assert second.status_code == 200
    assert notifier.calls == 2
    assert epusdt.rate_calls == 1
    assert epusdt.detail_calls == 1
    assert chain.calls == 1


@pytest.mark.parametrize("message", ["transfer not found", "transfer amount mismatch"])
def test_chain_lookup_failure_blocks_settlement_and_can_retry(
    client, settings, epusdt, chain, notifier, message
):
    create_order(client)
    chain.error = UpstreamError(message)
    content = callback_content(make_callback())
    assert client.post("/callbacks/gmpay", content=content).status_code == 503
    database = Database(settings.database_url)
    with database.session() as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.out_trade_no == "ORDER-001"))
        assert order.confirmed_at is None
        assert order.payer_address is None
        assert order.credit_quota is None
    database.dispose()
    assert epusdt.rate_calls == 0
    assert notifier.calls == 0

    chain.error = None
    assert client.post("/callbacks/gmpay", content=content).status_code == 200
    assert chain.calls == 2


def test_callback_json_decimal_canonicalization(client):
    create_order(client)
    unsigned = make_callback()
    del unsigned["signature"]
    signature = gmpay_hmac(unsigned, "gmpay-test-key")
    raw = (
        '{"pid":"1000","trade_id":"TRADE-001","order_id":"ORDER-001",'
        '"amount":100.00,"actual_amount":10.0000,"receive_address":"TReceiveAddress",'
        '"token":"USDT","block_transaction_id":"tx-abc-123","status":2,'
        f'"signature":"{signature}"}}'
    ).encode()
    response = client.post("/callbacks/gmpay", content=raw)
    assert response.status_code == 200, response.text


def test_missing_or_stale_rates_never_settle(client, settings, epusdt):
    create_order(client)
    epusdt.rates = {"usdt": Decimal("7")}
    response = client.post("/callbacks/gmpay", content=callback_content(make_callback()))
    assert response.status_code == 503
    database = Database(settings.database_url)
    with database.session() as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.out_trade_no == "ORDER-001"))
        assert order.confirmed_at is None
        assert order.credit_quota is None
    database.dispose()


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}

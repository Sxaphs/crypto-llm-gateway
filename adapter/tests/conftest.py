from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.clients import NotificationError, RateQuote, UpstreamError
from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'adapter.db'}",
        new_api_database_url=f"sqlite:///{tmp_path / 'new-api.db'}",
        epay_md5_key="epay-md5-test-key",
        gmpay_secret_key="gmpay-test-key",
        epay_hmac_key="new-api-hmac-test-key",
        gmpay_pid="1000",
        epay_pid="1000",
        epusdt_container_ip="testclient",
        epusdt_base_url="http://epusdt:8080",
        epusdt_admin_username="admin",
        epusdt_admin_password="admin-password",
        new_api_notify_url="http://new-api/api/user/epay/notify",
        quota_per_usd=Decimal("500000"),
        notify_background_retry=False,
        report_dir=str(tmp_path / "reports"),
    )


@pytest.fixture
def new_api_row(settings):
    engine = create_engine(settings.new_api_database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(320))"))
        connection.execute(
            text(
                "CREATE TABLE top_ups ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, money NUMERIC NOT NULL, "
                "trade_no VARCHAR(255) UNIQUE NOT NULL, payment_method VARCHAR(50) NOT NULL, "
                "payment_provider VARCHAR(50) NOT NULL, status VARCHAR(32) NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO users (id, email) VALUES (7, 'payer@example.com')"))
        connection.execute(
            text(
                "INSERT INTO top_ups "
                "(id, user_id, money, trade_no, payment_method, payment_provider, status) "
                "VALUES (11, 7, 100, 'ORDER-001', 'usdt.tron', 'epay', 'pending')"
            )
        )
    yield {"id": 11, "trade_no": "ORDER-001"}
    engine.dispose()


class FakeEpusdt:
    def __init__(self):
        self.create_calls = 0
        self.detail_calls = 0
        self.rate_calls = 0
        self.last_create_fields = None
        self.callback = None
        self.rates = {"usd": Decimal("0.14"), "usdt": Decimal("7")}
        self.observed_at = datetime.now(timezone.utc)

    def create_transaction(self, fields):
        self.create_calls += 1
        self.last_create_fields = fields
        return {
            "trade_id": "TRADE-001",
            "order_id": fields["order_id"],
            "amount": fields["amount"],
            "currency": "CNY",
            "actual_amount": Decimal("10"),
            "receive_address": "TReceiveAddress",
            "token": fields["token"].upper(),
            "status": 1,
            "payment_url": "https://pay.example.com/cashier/TRADE-001",
        }

    def get_order(self, trade_id):
        self.detail_calls += 1
        assert trade_id == "TRADE-001"
        return {
            "trade_id": trade_id,
            "order_id": "ORDER-001",
            "amount": Decimal("100"),
            "currency": "CNY",
            "actual_amount": Decimal("10"),
            "receive_address": "TReceiveAddress",
            "token": "USDT",
            "network": "tron",
            "status": 2,
            "block_transaction_id": "tx-abc-123",
            "updated_at": "2026-11-09T14:58:00Z",
        }

    def get_fresh_cny_rates(self):
        self.rate_calls += 1
        return RateQuote(self.rates.copy(), "https://rates.example.com/cny.json", self.observed_at)


class FakeChain:
    def __init__(self):
        self.calls = 0
        self.error = None
        self.last_lookup = None

    def get_payer(self, token, network, receive_address, tx_hash, actual_amount):
        self.calls += 1
        self.last_lookup = (token, network, receive_address, tx_hash, actual_amount)
        if self.error is not None:
            raise self.error
        return "TPayerAddress123"


class FakeNotifier:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.last_fields = None

    def notify(self, fields):
        self.calls += 1
        self.last_fields = fields
        if self.fail:
            raise NotificationError("simulated notification failure")


@pytest.fixture
def epusdt():
    return FakeEpusdt()


@pytest.fixture
def chain():
    return FakeChain()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def client(settings, new_api_row, epusdt, chain, notifier):
    app = create_app(settings=settings, epusdt_client=epusdt, chain_client=chain, notifier=notifier)
    with TestClient(app) as test_client:
        yield test_client

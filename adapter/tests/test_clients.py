from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.clients import EpusdtClient, NewAPINotifier, NotificationError, UpstreamError


def envelope(data):
    return {"status_code": 200, "message": "success", "data": data, "request_id": "test"}


def test_epusdt_client_uses_official_admin_order_and_rate_routes(settings):
    calls = []
    now = datetime.now(timezone.utc).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/admin/api/v1/auth/login":
            assert json.loads(request.content)["username"] == "admin"
            return httpx.Response(200, json=envelope({"token": "jwt-test"}))
        assert request.headers["authorization"] == "Bearer jwt-test"
        if request.url.path == "/admin/api/v1/orders/TRADE-001":
            return httpx.Response(200, json=envelope({"trade_id": "TRADE-001"}))
        if request.url.path == "/admin/api/v1/settings/rate/refresh":
            assert json.loads(request.content) == {"bases": ["cny"]}
            return httpx.Response(200, json=envelope([{"base": "cny", "ok": True}]))
        if request.url.path == "/admin/api/v1/settings/rate/status":
            return httpx.Response(
                200,
                json=envelope(
                    {
                        "bases": [
                            {
                                "base": "cny",
                                "rates": {"usd": 0.14, "usdt": 7},
                                "source_url": "https://rates.example.com/",
                                "is_stale": False,
                                "last_refresh_ok": True,
                                "last_success_at": now,
                            }
                        ]
                    }
                ),
            )
        return httpx.Response(404)

    client = EpusdtClient(settings, transport=httpx.MockTransport(handler))
    assert client.get_order("TRADE-001")["trade_id"] == "TRADE-001"
    quote = client.get_fresh_cny_rates()
    assert quote.rates == {"usd": Decimal("0.14"), "usdt": Decimal("7")}
    assert [path for _, path in calls] == [
        "/admin/api/v1/auth/login",
        "/admin/api/v1/orders/TRADE-001",
        "/admin/api/v1/settings/rate/refresh",
        "/admin/api/v1/settings/rate/status",
    ]
    client.close()


def test_epusdt_refresh_failure_is_not_replaced_with_cached_rates(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/api/v1/auth/login":
            return httpx.Response(200, json=envelope({"token": "jwt-test"}))
        return httpx.Response(200, json=envelope([{"base": "cny", "ok": False}]))

    client = EpusdtClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError, match="refresh failed"):
        client.get_fresh_cny_rates()
    client.close()


def test_new_api_notify_requires_2xx_and_exact_success(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="success")

    notifier = NewAPINotifier(settings, transport=httpx.MockTransport(handler))
    notifier.notify({"pid": "1000", "credit_quota": 1, "money": Decimal("0.20")})
    assert len(requests) == 1
    body = requests[0].content.decode()
    assert "money=0.2" in body
    assert "signature=" in body
    notifier.close()

    bad = NewAPINotifier(
        settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    )
    with pytest.raises(NotificationError):
        bad.notify({"pid": "1000"})
    bad.close()

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .signing import decimal_to_plain, gmpay_hmac, signature_value


class UpstreamError(RuntimeError):
    pass


class NotificationError(RuntimeError):
    pass


def _json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json(parse_float=Decimal, parse_int=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpstreamError(f"invalid upstream JSON (HTTP {response.status_code})") from exc
    if not isinstance(payload, dict):
        raise UpstreamError("upstream JSON response is not an object")
    return payload


def _response_data(response: httpx.Response) -> Any:
    payload = _json_response(response)
    if response.status_code < 200 or response.status_code >= 300:
        raise UpstreamError(f"upstream returned HTTP {response.status_code}")
    if payload.get("status_code") != 200:
        raise UpstreamError("upstream returned a non-success business status")
    return payload.get("data")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise UpstreamError(f"invalid numeric field: {field}")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise UpstreamError(f"invalid numeric field: {field}") from exc
    if not result.is_finite():
        raise UpstreamError(f"invalid numeric field: {field}")
    return result


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise UpstreamError("rate snapshot has no last_success_at")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpstreamError("rate snapshot has an invalid last_success_at") from exc
    if parsed.tzinfo is None:
        raise UpstreamError("rate snapshot timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class RateQuote:
    rates: dict[str, Decimal]
    source: str
    observed_at: datetime


class EpusdtClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.epusdt_base_url,
            timeout=settings.http_timeout_seconds,
            transport=transport,
        )
        self._token: str | None = None
        self._token_lock = threading.Lock()

    def close(self) -> None:
        self.http.close()

    def create_transaction(self, fields: dict[str, Any]) -> dict[str, Any]:
        data = {key: signature_value(value) or "" for key, value in fields.items()}
        data["signature"] = gmpay_hmac(data, self.settings.gmpay_secret_key)
        try:
            response = self.http.post("/payments/gmpay/v1/order/create-transaction", data=data)
            result = _response_data(response)
        except (httpx.HTTPError, UpstreamError) as exc:
            raise UpstreamError("epusdt order creation failed") from exc
        if not isinstance(result, dict):
            raise UpstreamError("epusdt order creation returned no order data")
        return result

    def _login(self, force: bool = False) -> str:
        with self._token_lock:
            if self._token is not None and not force:
                return self._token
            try:
                response = self.http.post(
                    "/admin/api/v1/auth/login",
                    json={
                        "username": self.settings.epusdt_admin_username,
                        "password": self.settings.epusdt_admin_password,
                    },
                )
                data = _response_data(response)
            except (httpx.HTTPError, UpstreamError) as exc:
                raise UpstreamError("epusdt admin login failed") from exc
            if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
                raise UpstreamError("epusdt admin login returned no token")
            self._token = data["token"]
            return self._token

    def _admin_request(self, method: str, path: str, **kwargs: Any) -> Any:
        for retry in (False, True):
            token = self._login(force=retry)
            headers = dict(kwargs.pop("headers", {}))
            headers["Authorization"] = f"Bearer {token}"
            try:
                response = self.http.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise UpstreamError("epusdt admin request failed") from exc
            if response.status_code == 401 and not retry:
                continue
            return _response_data(response)
        raise UpstreamError("epusdt admin authentication failed")

    def get_order(self, trade_id: str) -> dict[str, Any]:
        data = self._admin_request("GET", f"/admin/api/v1/orders/{quote(trade_id, safe='')}")
        if not isinstance(data, dict):
            raise UpstreamError("epusdt order detail is not an object")
        return data

    def get_fresh_cny_rates(self) -> RateQuote:
        refreshed = self._admin_request(
            "POST", "/admin/api/v1/settings/rate/refresh", json={"bases": ["cny"]}
        )
        if not isinstance(refreshed, list):
            raise UpstreamError("rate refresh response is not a list")
        cny_refresh = next(
            (row for row in refreshed if isinstance(row, dict) and str(row.get("base", "")).lower() == "cny"),
            None,
        )
        if cny_refresh is None or cny_refresh.get("ok") is not True:
            raise UpstreamError("CNY rate refresh failed")

        status = self._admin_request("GET", "/admin/api/v1/settings/rate/status")
        if not isinstance(status, dict) or not isinstance(status.get("bases"), list):
            raise UpstreamError("rate status response is invalid")
        base = next(
            (
                row
                for row in status["bases"]
                if isinstance(row, dict) and str(row.get("base", "")).lower() == "cny"
            ),
            None,
        )
        if base is None or base.get("is_stale") is not False or base.get("last_refresh_ok") is not True:
            raise UpstreamError("CNY rate status is stale or unsuccessful")
        raw_rates = base.get("rates")
        source = base.get("source_url")
        if not isinstance(raw_rates, dict) or not isinstance(source, str) or not source.strip():
            raise UpstreamError("CNY rate snapshot lacks rates or source")
        rates = {str(key).strip().lower(): _decimal(value, f"rates.{key}") for key, value in raw_rates.items()}
        return RateQuote(rates=rates, source=source.strip(), observed_at=_parse_time(base.get("last_success_at")))


class NewAPINotifier:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.http = httpx.Client(timeout=settings.http_timeout_seconds, transport=transport)

    def close(self) -> None:
        self.http.close()

    def notify(self, fields: dict[str, Any]) -> None:
        data = {key: signature_value(value) or "" for key, value in fields.items()}
        data["signature"] = gmpay_hmac(data, self.settings.epay_hmac_key)
        try:
            response = self.http.post(self.settings.new_api_notify_url, data=data)
        except httpx.HTTPError as exc:
            raise NotificationError("New API notification request failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise NotificationError(f"New API notification returned HTTP {response.status_code}")
        if response.text.strip().lower() != "success":
            raise NotificationError("New API notification body was not success")


def format_decimal(value: Decimal) -> str:
    return decimal_to_plain(value)

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager, suppress
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from .chain import ChainClient
from .clients import EpusdtClient, NewAPINotifier, NotificationError, UpstreamError
from .config import Settings, get_settings
from .db import Database, NewAPIReadonly
from .logging_config import configure_logging
from .services import (
    AdapterError,
    OrderConflict,
    OrderNotFound,
    SettlementError,
    handle_callback,
    retry_pending_notifications,
    submit_payment,
)
from .signing import SignatureValueError, epay_md5, gmpay_hmac, signatures_equal

logger = logging.getLogger(__name__)
CALLBACK_BODY_LIMIT = 64 * 1024
SUBMIT_BODY_LIMIT = 64 * 1024
CALLBACK_FIELDS = frozenset(
    {
        "pid",
        "trade_id",
        "order_id",
        "amount",
        "actual_amount",
        "receive_address",
        "token",
        "block_transaction_id",
        "status",
        "signature",
    }
)


def _decode_json(raw: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    payload = json.loads(
        raw.decode("utf-8"), parse_float=Decimal, parse_int=Decimal, parse_constant=reject_constant
    )
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def _single_values(pairs: list[tuple[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"duplicate parameter: {key}")
        result[key] = value
    return result


async def _submit_params(request: Request) -> dict[str, str]:
    try:
        params = _single_values(list(request.query_params.multi_items()))
        if request.method == "POST":
            body = await request.body()
            if len(body) > SUBMIT_BODY_LIMIT:
                raise AdapterError("request body is too large")
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if body and content_type != "application/x-www-form-urlencoded":
                raise AdapterError("POST body must be application/x-www-form-urlencoded")
            form = _single_values(
                parse_qsl(
                    body.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=False,
                    encoding="utf-8",
                    errors="strict",
                    max_num_fields=50,
                )
            )
            params.update(form)
        return params
    except (UnicodeDecodeError, ValueError) as exc:
        raise AdapterError("invalid form encoding") from exc


def create_app(
    settings: Settings | None = None,
    epusdt_client: Any | None = None,
    chain_client: Any | None = None,
    notifier: Any | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = app.state.settings or get_settings()
        configure_logging(app.state.settings.log_level)
        app.state.database = app.state.database or Database(app.state.settings.database_url)
        app.state.database.create_schema()
        app.state.new_api = app.state.new_api or NewAPIReadonly(app.state.settings.new_api_database_url)
        app.state.epusdt = app.state.epusdt or EpusdtClient(app.state.settings)
        app.state.chain = app.state.chain or ChainClient(app.state.settings)
        app.state.notifier = app.state.notifier or NewAPINotifier(app.state.settings)
        retry_task = None

        async def retry_loop() -> None:
            while True:
                await asyncio.sleep(app.state.settings.notify_retry_seconds)
                await asyncio.to_thread(
                    retry_pending_notifications, app.state.settings, app.state.database, app.state.notifier
                )

        if app.state.settings.notify_background_retry:
            retry_task = asyncio.create_task(retry_loop())
        try:
            yield
        finally:
            if retry_task is not None:
                retry_task.cancel()
                with suppress(asyncio.CancelledError):
                    await retry_task
            app.state.database.dispose()
            app.state.new_api.dispose()
            close_epusdt = getattr(app.state.epusdt, "close", None)
            close_chain = getattr(app.state.chain, "close", None)
            close_notifier = getattr(app.state.notifier, "close", None)
            if callable(close_epusdt):
                close_epusdt()
            if callable(close_chain):
                close_chain()
            if callable(close_notifier):
                close_notifier()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.database = None
    app.state.new_api = None
    app.state.epusdt = epusdt_client
    app.state.chain = chain_client
    app.state.notifier = notifier

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            await asyncio.to_thread(app.state.database.ping)
            return JSONResponse({"status": "ready"})
        except Exception:
            logger.warning("database readiness check failed", extra={"event": "readiness_failed"})
            return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.api_route("/crypto-pay/submit.php", methods=["GET", "POST"], response_model=None)
    async def submit(request: Request) -> RedirectResponse | JSONResponse:
        try:
            params = await _submit_params(request)
            required = {"pid", "money", "out_trade_no", "notify_url", "return_url", "name", "type", "sign", "sign_type"}
            if not required.issubset(params):
                raise AdapterError("missing required EPay parameters")
            if params["pid"] != app.state.settings.epay_pid:
                raise AdapterError("EPay pid mismatch")
            if params["sign_type"].upper() != "MD5":
                raise AdapterError("sign_type must be MD5")
            if not signatures_equal(epay_md5(params, app.state.settings.epay_md5_key), params["sign"]):
                return JSONResponse({"detail": "invalid signature"}, status_code=401)
            if params["type"] not in {"usdt.tron", "usdc.ethereum", "usdc.polygon"}:
                raise AdapterError("unsupported payment type")
            order = await asyncio.to_thread(
                submit_payment,
                params,
                app.state.settings,
                app.state.database,
                app.state.new_api,
                app.state.epusdt,
            )
            if not order.payment_url:
                raise OrderConflict("cashier URL is not available yet")
            return RedirectResponse(order.payment_url, status_code=302)
        except OrderConflict as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except (AdapterError, SignatureValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except UpstreamError:
            logger.warning("order creation upstream failure", extra={"event": "order_create_failed"})
            return JSONResponse({"detail": "payment gateway unavailable"}, status_code=502)

    @app.post("/callbacks/gmpay", response_model=None)
    async def callback(request: Request) -> PlainTextResponse | JSONResponse:
        if request.client is None or request.client.host != app.state.settings.epusdt_container_ip:
            return PlainTextResponse("forbidden", status_code=403)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > CALLBACK_BODY_LIMIT:
                    return PlainTextResponse("request body too large", status_code=413)
            except ValueError:
                return PlainTextResponse("invalid content length", status_code=400)
        raw = await request.body()
        if len(raw) > CALLBACK_BODY_LIMIT:
            return PlainTextResponse("request body too large", status_code=413)
        try:
            payload = _decode_json(raw)
        except (ValueError, UnicodeDecodeError):
            return PlainTextResponse("invalid JSON", status_code=400)
        if not CALLBACK_FIELDS.issubset(payload):
            return PlainTextResponse("unauthorized", status_code=401)
        try:
            expected = gmpay_hmac(payload, app.state.settings.gmpay_secret_key)
        except SignatureValueError:
            return PlainTextResponse("unauthorized", status_code=401)
        if not signatures_equal(expected, payload.get("signature")):
            return PlainTextResponse("unauthorized", status_code=401)
        try:
            if payload["status"] != 2:
                return PlainTextResponse("unsupported callback status", status_code=400)
            await asyncio.to_thread(
                handle_callback,
                payload,
                hashlib.sha256(raw).hexdigest(),
                app.state.settings,
                app.state.database,
                app.state.epusdt,
                app.state.chain,
                app.state.notifier,
            )
            return PlainTextResponse("ok")
        except OrderNotFound as exc:
            return PlainTextResponse(str(exc), status_code=404)
        except (NotificationError, UpstreamError, SettlementError):
            logger.warning("callback processing failed", extra={"event": "callback_failed"})
            return PlainTextResponse("retry", status_code=503)
        except AdapterError as exc:
            return PlainTextResponse(str(exc), status_code=400)

    return app


app = create_app()

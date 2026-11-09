from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .chain import ChainClient
from .clients import EpusdtClient, NewAPINotifier, NotificationError, UpstreamError
from .config import Settings
from .db import Database, NewAPIReadonly
from .models import CallbackEvent, PaymentOrder, RateSnapshot, utcnow

logger = logging.getLogger(__name__)

ALLOWED_PAYMENT_TYPES = {
    "usdt.tron": ("usdt", "tron"),
    "usdc.ethereum": ("usdc", "ethereum"),
    "usdc.polygon": ("usdc", "polygon"),
}


class AdapterError(RuntimeError):
    pass


class OrderConflict(AdapterError):
    pass


class SettlementError(AdapterError):
    pass


class OrderNotFound(AdapterError):
    pass


def as_decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise AdapterError(f"invalid {field}")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise AdapterError(f"invalid {field}") from exc
    if not result.is_finite():
        raise AdapterError(f"invalid {field}")
    return result


def as_text(value: Any, field: str) -> str:
    if isinstance(value, str):
        result = value
    elif isinstance(value, Decimal) and value == value.to_integral_value():
        result = str(value.quantize(Decimal("1")))
    else:
        raise AdapterError(f"invalid {field}")
    if not result:
        raise AdapterError(f"missing {field}")
    return result


def _validate_new_api_order(row: dict | None, params: dict[str, str], token_type: str) -> dict:
    if row is None:
        raise AdapterError("New API top-up does not exist")
    if str(row.get("status", "")).lower() != "pending":
        raise AdapterError("New API top-up is not pending")
    if row.get("payment_method") != token_type:
        raise AdapterError("payment method does not match New API top-up")
    if str(row.get("payment_provider", "")).lower() != "epay":
        raise AdapterError("payment provider does not match New API top-up")
    expected_money = as_decimal(row.get("money"), "New API money")
    actual_money = as_decimal(params["money"], "money")
    if expected_money != actual_money:
        raise AdapterError("money does not match New API top-up")
    return row


def submit_payment(
    params: dict[str, str],
    settings: Settings,
    database: Database,
    new_api: NewAPIReadonly,
    epusdt: EpusdtClient,
) -> PaymentOrder:
    token_type = params["type"]
    token, network = ALLOWED_PAYMENT_TYPES[token_type]
    money = as_decimal(params["money"], "money")
    if money <= 0:
        raise AdapterError("money must be positive")
    top_up = _validate_new_api_order(new_api.get_pending_top_up(params["out_trade_no"]), params, token_type)

    with database.session() as session:
        existing = session.scalar(
            select(PaymentOrder).where(PaymentOrder.out_trade_no == params["out_trade_no"]).with_for_update()
        )
        if existing is not None:
            if existing.new_api_user_id != int(top_up["user_id"]) or existing.money_cny != money:
                raise OrderConflict("local order does not match New API top-up")
            if existing.payment_url:
                return existing
            if existing.status == "creating":
                raise OrderConflict("order creation is already in progress")
            order = existing
            order.status = "creating"
            session.commit()
        else:
            order = PaymentOrder(
                out_trade_no=params["out_trade_no"],
                new_api_top_up_id=int(top_up["top_up_id"]),
                new_api_user_id=int(top_up["user_id"]),
                email=str(top_up.get("email") or ""),
                payment_type=token_type,
                token=token,
                network=network,
                money_cny=money,
                name=params.get("name", ""),
                return_url=params.get("return_url", ""),
                status="creating",
            )
            session.add(order)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                existing = session.scalar(
                    select(PaymentOrder).where(PaymentOrder.out_trade_no == params["out_trade_no"])
                )
                if existing is not None and existing.payment_url:
                    return existing
                raise OrderConflict("order was concurrently submitted") from exc

    fields = {
        "pid": settings.gmpay_pid,
        "order_id": order.out_trade_no,
        "currency": "cny",
        "amount": money,
        "token": token,
        "network": network,
        "notify_url": "http://payment-adapter:8080/callbacks/gmpay",
        "redirect_url": order.return_url,
        "name": order.name,
    }
    try:
        created = epusdt.create_transaction(fields)
        trade_id = as_text(created.get("trade_id"), "trade_id")
        payment_url = as_text(created.get("payment_url"), "payment_url")
        receive_address = as_text(created.get("receive_address"), "receive_address")
        actual_amount = as_decimal(created.get("actual_amount"), "actual_amount")
        if created.get("order_id") != order.out_trade_no:
            raise UpstreamError("created order_id mismatch")
        if str(created.get("token", "")).lower() != token:
            raise UpstreamError("created token mismatch")
        if as_decimal(created.get("amount"), "amount") != money or actual_amount <= 0:
            raise UpstreamError("created amount mismatch")
    except Exception:
        with database.session() as session:
            failed = session.get(PaymentOrder, order.id)
            if failed is not None and failed.status == "creating":
                failed.status = "creation_failed"
                session.commit()
        raise

    with database.session() as session:
        with session.begin():
            locked = session.scalar(select(PaymentOrder).where(PaymentOrder.id == order.id).with_for_update())
            if locked is None:
                raise OrderConflict("local order disappeared")
            locked.trade_id = trade_id
            locked.payment_url = payment_url
            locked.receive_address = receive_address
            locked.actual_amount = actual_amount
            locked.status = "created"
            session.flush()
            return locked


def _check_callback_identity(order: PaymentOrder, payload: dict[str, Any], settings: Settings) -> None:
    if as_text(payload["pid"], "pid") != settings.gmpay_pid:
        raise SettlementError("callback pid mismatch")
    if order.trade_id is None or as_text(payload["trade_id"], "trade_id") != order.trade_id:
        raise SettlementError("callback trade_id mismatch")
    if as_text(payload["order_id"], "order_id") != order.out_trade_no:
        raise SettlementError("callback order_id mismatch")
    if as_text(payload["token"], "token").lower() != order.token:
        raise SettlementError("callback token mismatch")
    if as_decimal(payload["amount"], "amount") != order.money_cny:
        raise SettlementError("callback amount mismatch")


def _verify_admin_order(
    detail: dict[str, Any], order: PaymentOrder, payload: dict[str, Any]
) -> tuple[Decimal, str, str, str]:
    if as_text(detail.get("trade_id"), "detail trade_id") != order.trade_id:
        raise SettlementError("epusdt detail trade_id mismatch")
    if as_text(detail.get("order_id"), "detail order_id") != order.out_trade_no:
        raise SettlementError("epusdt detail order_id mismatch")
    if as_decimal(detail.get("status"), "detail status") != 2:
        raise SettlementError("epusdt order is not paid")
    if str(detail.get("currency", "")).lower() != "cny":
        raise SettlementError("epusdt detail currency mismatch")
    if str(detail.get("network", "")).lower() != order.network:
        raise SettlementError("epusdt detail network mismatch")
    if as_text(detail.get("token"), "detail token").lower() != order.token:
        raise SettlementError("epusdt detail token mismatch")

    receive_address = as_text(payload["receive_address"], "receive_address")
    tx = as_text(payload["block_transaction_id"], "block_transaction_id")
    actual_amount = as_decimal(payload["actual_amount"], "actual_amount")
    if actual_amount <= 0:
        raise SettlementError("actual_amount must be positive")
    if order.receive_address and receive_address != order.receive_address:
        raise SettlementError("callback receive_address mismatch")
    if as_text(detail.get("receive_address"), "detail receive_address") != receive_address:
        raise SettlementError("epusdt detail receive_address mismatch")
    if as_text(detail.get("block_transaction_id"), "detail block_transaction_id") != tx:
        raise SettlementError("epusdt detail transaction mismatch")
    if as_decimal(detail.get("amount"), "detail amount") != order.money_cny:
        raise SettlementError("epusdt detail amount mismatch")
    if as_decimal(detail.get("actual_amount"), "detail actual_amount") != actual_amount:
        raise SettlementError("epusdt detail actual_amount mismatch")
    return actual_amount, receive_address, tx, as_text(payload["token"], "token").lower()


def _confirmation_time(detail: dict[str, Any], callback_received_at: datetime) -> tuple[datetime, str]:
    # Official mdb.Orders exposes BaseModel.updated_at. Prefer an explicit paid
    # time if a future version adds one; updated_at is the current order-update
    # confirmation timestamp, not a claim about an individual chain block time.
    for field in ("paid_at", "payment_confirmed_at", "confirmed_at", "updated_at"):
        value = detail.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise SettlementError(f"epusdt detail {field} is not a timestamp")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise SettlementError(f"epusdt detail {field} is invalid") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc), f"epusdt.{field}"
    return callback_received_at, "callback_received_at"


def _settle_first_callback(
    order: PaymentOrder,
    payload: dict[str, Any],
    settings: Settings,
    epusdt: EpusdtClient,
    chain: ChainClient,
    callback_received_at: datetime,
) -> None:
    detail = epusdt.get_order(as_text(payload["trade_id"], "trade_id"))
    actual_amount, receive_address, tx, token = _verify_admin_order(detail, order, payload)
    confirmed_at, confirmation_time_source = _confirmation_time(detail, callback_received_at)
    payer = chain.get_payer(token, order.network, receive_address, tx, actual_amount)
    if not isinstance(payer, str) or not payer.strip():
        raise SettlementError("chain lookup returned no payer address")
    quote = epusdt.get_fresh_cny_rates()
    now = utcnow()
    age = now - quote.observed_at
    if age > timedelta(seconds=settings.rate_max_age_seconds) or age < timedelta(seconds=-30):
        raise SettlementError("rate snapshot is outside the allowed age")

    token_per_cny = quote.rates.get(token)
    usd_per_cny = quote.rates.get("usd")
    if token_per_cny is None or token_per_cny <= 0 or usd_per_cny is None or usd_per_cny <= 0:
        raise SettlementError("positive token and USD rates are required")
    with localcontext() as context:
        context.prec = 60
        usd_per_token = usd_per_cny / token_per_cny
        settled_usd = actual_amount * usd_per_token
        credit_quota_decimal = (settled_usd * settings.quota_per_usd).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    credit_quota = int(credit_quota_decimal)
    if settled_usd <= 0 or credit_quota <= 0:
        raise SettlementError("settlement produced a non-positive credit")

    order.actual_amount = actual_amount
    order.receive_address = receive_address
    order.payer_address = payer.strip()
    order.payer_address_pending = False
    order.block_transaction_id = tx
    order.token_per_cny = token_per_cny
    order.usd_per_cny = usd_per_cny
    order.usd_per_token = usd_per_token
    order.rate_source = quote.source
    order.rate_observed_at = quote.observed_at
    order.settled_usd = settled_usd
    order.credit_quota = credit_quota
    order.confirmed_at = confirmed_at
    order.confirmation_time_source = confirmation_time_source
    order.callback_received_at = callback_received_at
    order.status = "settled"
    order.rate_snapshot = RateSnapshot(
        base="cny",
        token=token,
        token_per_cny=token_per_cny,
        usd_per_cny=usd_per_cny,
        usd_per_token=usd_per_token,
        source=quote.source,
        observed_at=quote.observed_at,
        rates_json=json.dumps({key: str(value) for key, value in quote.rates.items()}, sort_keys=True),
    )


def _record_event(database: Database, payload: dict[str, Any], raw_sha256: str) -> int:
    with database.session() as session:
        with session.begin():
            order = session.scalar(
                select(PaymentOrder).where(PaymentOrder.out_trade_no == as_text(payload["order_id"], "order_id"))
            )
            event = CallbackEvent(
                payment_order_id=order.id if order else None,
                order_id=as_text(payload["order_id"], "order_id"),
                trade_id=as_text(payload["trade_id"], "trade_id"),
                raw_sha256=raw_sha256,
                block_transaction_id=as_text(payload["block_transaction_id"], "block_transaction_id"),
            )
            session.add(event)
            session.flush()
            return event.id


def _finish_event(database: Database, event_id: int, outcome: str, detail: str = "") -> None:
    with database.session() as session:
        with session.begin():
            event = session.get(CallbackEvent, event_id)
            if event is not None:
                event.outcome = outcome
                event.detail = detail[:500]
                event.processed_at = utcnow()


def notify_order(
    order_id: int,
    settings: Settings,
    database: Database,
    notifier: NewAPINotifier,
) -> bool:
    with database.session() as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update())
        if order is None or order.confirmed_at is None:
            raise NotificationError("order is not settled")
        if order.notified_at is not None:
            return False
        if order.credit_quota is None or order.settled_usd is None or order.usd_per_token is None:
            raise NotificationError("settlement snapshot is incomplete")
        order.notification_attempts += 1
        fields = {
            "pid": settings.epay_pid,
            "out_trade_no": order.out_trade_no,
            "trade_id": order.trade_id,
            "trade_status": "TRADE_SUCCESS",
            "type": order.payment_type,
            "money": order.settled_usd,
            "credit_quota": order.credit_quota,
            "settled_usd": order.settled_usd,
            "actual_amount": order.actual_amount,
            "usd_per_token": order.usd_per_token,
            "block_transaction_id": order.block_transaction_id,
        }
        try:
            notifier.notify(fields)
        except Exception as exc:
            order.last_notification_error = f"{type(exc).__name__}: {exc}"[:500]
            session.commit()
            raise
        order.notified_at = utcnow()
        order.status = "notified"
        order.last_notification_error = None
        session.commit()
        logger.info(
            "New API notified for out_trade_no=%s",
            order.out_trade_no,
            extra={"event": "new_api_notified"},
        )
        return True


def handle_callback(
    payload: dict[str, Any],
    raw_sha256: str,
    settings: Settings,
    database: Database,
    epusdt: EpusdtClient,
    chain: ChainClient,
    notifier: NewAPINotifier,
) -> None:
    callback_received_at = utcnow()
    event_id = _record_event(database, payload, raw_sha256)
    try:
        with database.session() as session:
            with session.begin():
                order = session.scalar(
                    select(PaymentOrder)
                    .where(PaymentOrder.out_trade_no == as_text(payload["order_id"], "order_id"))
                    .with_for_update()
                )
                if order is None:
                    raise OrderNotFound("payment order not found")
                _check_callback_identity(order, payload, settings)
                if order.confirmed_at is None:
                    _settle_first_callback(order, payload, settings, epusdt, chain, callback_received_at)
                    session.flush()
                    order_id = order.id
                else:
                    # Existing confirmed_at/credit_quota is the immutable settlement
                    # snapshot. In particular, never refresh rates on this path.
                    if order.credit_quota is None or order.settled_usd is None:
                        raise SettlementError("existing settlement snapshot is incomplete")
                    order_id = order.id
        notified = notify_order(order_id, settings, database, notifier)
        _finish_event(database, event_id, "notified" if notified else "duplicate")
        logger.info(
            "GMPay callback processed for out_trade_no=%s",
            as_text(payload["order_id"], "order_id"),
            extra={"event": "callback_processed"},
        )
    except Exception as exc:
        _finish_event(database, event_id, "failed", f"{type(exc).__name__}: {exc}")
        raise


def retry_pending_notifications(settings: Settings, database: Database, notifier: NewAPINotifier) -> None:
    with database.session() as session:
        ids = list(
            session.scalars(
                select(PaymentOrder.id).where(
                    PaymentOrder.confirmed_at.is_not(None), PaymentOrder.notified_at.is_(None)
                )
            )
        )
    for order_id in ids:
        try:
            notify_order(order_id, settings, database, notifier)
        except Exception:
            logger.warning("background notification retry failed", extra={"event": "notification_retry_failed"})

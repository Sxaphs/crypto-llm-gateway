from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExactDecimal(TypeDecorator[Decimal]):
    """Text storage avoids SQLite float coercion and keeps retry payloads exact."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else format(value, "f")

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


MONEY = ExactDecimal()
RATE = ExactDecimal()


class Base(DeclarativeBase):
    pass


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    out_trade_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    new_api_top_up_id: Mapped[int] = mapped_column(BigInteger)
    new_api_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    payment_type: Mapped[str] = mapped_column(String(32))
    token: Mapped[str] = mapped_column(String(16))
    network: Mapped[str] = mapped_column(String(32))
    money_cny: Mapped[Decimal] = mapped_column(MONEY)
    name: Mapped[str] = mapped_column(String(255), default="")
    return_url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="creating", index=True)

    trade_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    payment_url: Mapped[str | None] = mapped_column(Text)
    receive_address: Mapped[str | None] = mapped_column(String(256))
    payer_address: Mapped[str | None] = mapped_column(String(256))
    payer_address_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    actual_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    block_transaction_id: Mapped[str | None] = mapped_column(String(256), index=True)

    settled_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    credit_quota: Mapped[int | None] = mapped_column(BigInteger)
    token_per_cny: Mapped[Decimal | None] = mapped_column(RATE)
    usd_per_cny: Mapped[Decimal | None] = mapped_column(RATE)
    usd_per_token: Mapped[Decimal | None] = mapped_column(RATE)
    rate_source: Mapped[str | None] = mapped_column(Text)
    rate_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # confirmed_at is epusdt's paid/updated order time when available, otherwise
    # callback_received_at. confirmation_time_source disambiguates the semantics.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_time_source: Mapped[str | None] = mapped_column(String(64))
    callback_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_attempts: Mapped[int] = mapped_column(default=0)
    last_notification_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    rate_snapshot: Mapped[RateSnapshot | None] = relationship(back_populates="payment_order")


class CallbackEvent(Base):
    __tablename__ = "callback_events"
    __table_args__ = (Index("ix_callback_events_raw_order", "raw_sha256", "order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_order_id: Mapped[int | None] = mapped_column(ForeignKey("payment_orders.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    trade_id: Mapped[str] = mapped_column(String(128), index=True)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    block_transaction_id: Mapped[str | None] = mapped_column(String(256))
    outcome: Mapped[str] = mapped_column(String(32), default="received")
    detail: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RateSnapshot(Base):
    __tablename__ = "rate_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_order_id: Mapped[int] = mapped_column(
        ForeignKey("payment_orders.id"), unique=True, index=True
    )
    base: Mapped[str] = mapped_column(String(16), default="cny")
    token: Mapped[str] = mapped_column(String(16))
    token_per_cny: Mapped[Decimal] = mapped_column(RATE)
    usd_per_cny: Mapped[Decimal] = mapped_column(RATE)
    usd_per_token: Mapped[Decimal] = mapped_column(RATE)
    source: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rates_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    payment_order: Mapped[PaymentOrder] = relationship(back_populates="rate_snapshot")

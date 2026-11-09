"""只读访问 adapter 的 PostgreSQL（payment_orders / rate_snapshots）。

直接按 adapter/app/models.py 的真实 schema 读取，不再做模糊别名猜测。
金额/汇率列在 adapter 里以 TEXT 存储（ExactDecimal），本模块读回后统一转 Decimal。
时间列（created_at / confirmed_at / notified_at / observed_at）为 timestamptz。

依赖 psycopg（见 scripts/requirements.txt）。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

# adapter 用 Text 存储的精确小数列（ExactDecimal），读出后转 Decimal
_DECIMAL_TEXT_COLS = {
    "money_cny",
    "actual_amount",
    "settled_usd",
    "token_per_cny",
    "usd_per_cny",
    "usd_per_token",
}

PAYMENT_ORDER_COLS = [
    "id",
    "out_trade_no",
    "new_api_top_up_id",
    "new_api_user_id",
    "email",
    "payment_type",
    "token",
    "network",
    "money_cny",
    "name",
    "return_url",
    "status",
    "trade_id",
    "payment_url",
    "receive_address",
    "payer_address",
    "payer_address_pending",
    "actual_amount",
    "block_transaction_id",
    "settled_usd",
    "credit_quota",
    "token_per_cny",
    "usd_per_cny",
    "usd_per_token",
    "rate_source",
    "rate_observed_at",
    "confirmed_at",
    "confirmation_time_source",
    "callback_received_at",
    "notified_at",
    "notification_attempts",
    "last_notification_error",
    "created_at",
    "updated_at",
]

RATE_SNAPSHOT_COLS = [
    "id",
    "payment_order_id",
    "base",
    "token",
    "token_per_cny",
    "usd_per_cny",
    "usd_per_token",
    "source",
    "observed_at",
    "rates_json",
    "created_at",
]


class AdapterPgError(RuntimeError):
    pass


def _connect(dsn: str):
    try:
        import psycopg  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise AdapterPgError("缺少 psycopg，请先: pip install -r scripts/requirements.txt") from exc
    try:
        return psycopg.connect(dsn, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        raise AdapterPgError(f"无法连接 adapter PG: {exc}") from exc


def _to_decimal(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value


def _normalize(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if k in _DECIMAL_TEXT_COLS and v is not None:
            out[k] = _to_decimal(v)
        else:
            out[k] = v
    return out


def _select(conn, table: str, cols: List[str], where: str, args: List[Any], limit: int) -> List[Dict[str, Any]]:
    col_sql = ", ".join(f'"{c}"' for c in cols)
    sql = f'SELECT {col_sql} FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    sql += f' ORDER BY "created_at" DESC NULLS LAST LIMIT %s'
    cur = conn.cursor()
    try:
        cur.execute(sql, list(args) + [limit])
    except Exception as exc:  # noqa: BLE001
        raise AdapterPgError(f"读取 {table} 失败（可能缺列）: {exc}") from exc
    return [_normalize(r) for r in cur.fetchall()]


def _time_where(col: str, since: int, until: int) -> tuple:
    where = ""
    args: List[Any] = []
    if since > 0:
        where = f'"{col}" >= to_timestamp(%s)'
        args.append(since)
    if until > 0:
        if where:
            where += " AND "
        where += f'"{col}" <= to_timestamp(%s)'
        args.append(until)
    return where, args


def read_payment_orders(dsn: str, since: int = 0, until: int = 0, limit: int = 10000) -> Dict[str, Any]:
    conn = _connect(dsn)
    try:
        where, args = _time_where("created_at", since, until)
        rows = _select(conn, "payment_orders", PAYMENT_ORDER_COLS, where, args, limit)
        return {"rows": rows}
    finally:
        conn.close()


def read_rate_snapshots(dsn: str, since: int = 0, until: int = 0, limit: int = 100000) -> Dict[str, Any]:
    conn = _connect(dsn)
    try:
        where, args = _time_where("created_at", since, until)
        rows = _select(conn, "rate_snapshots", RATE_SNAPSHOT_COLS, where, args, limit)
        return {"rows": rows}
    finally:
        conn.close()

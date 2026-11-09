from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, class_=Session, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.sessions()
        try:
            yield session
        finally:
            session.close()

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def dispose(self) -> None:
        self.engine.dispose()


class NewAPIReadonly:
    """Only exposes a parameterized, read-only New API top-up lookup."""

    def __init__(self, url: str) -> None:
        self.engine = create_engine(url, pool_pre_ping=True)

    def get_pending_top_up(self, trade_no: str) -> dict | None:
        query = text(
            "SELECT t.id AS top_up_id, t.user_id AS user_id, t.money AS money, "
            "t.status AS status, t.payment_method AS payment_method, "
            "t.payment_provider AS payment_provider, u.email AS email "
            "FROM top_ups AS t JOIN users AS u ON u.id = t.user_id "
            "WHERE t.trade_no = :trade_no"
        )
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            elif connection.dialect.name == "sqlite":
                connection.execute(text("PRAGMA query_only = ON"))
            row = connection.execute(query, {"trade_no": trade_no}).mappings().first()
            return dict(row) if row is not None else None

    def dispose(self) -> None:
        self.engine.dispose()

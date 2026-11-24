from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Firm(Base):
    __tablename__ = "firms"
    __table_args__ = (UniqueConstraint("uuid", name="uq_firms_uuid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)
    balance_cents: Mapped[int] = mapped_column(Integer)
    uuid: Mapped[str] = mapped_column(Text)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, Identity(always=False), primary_key=True)
    payer_firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"))
    payee_firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("payer_firm_uuid", "key", name="uq_idempotency_keys"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    payer_firm_uuid: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    response_body: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

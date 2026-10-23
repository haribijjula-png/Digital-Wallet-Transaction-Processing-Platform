import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import (Direction, IdempotencyState, LedgerType, NotificationType, RefundStatus,
                              ReviewStatus, RiskStatus, RoleName, TransactionType, TxStatus, WalletStatus)


def enum_col(enum_cls):
    return sa.Enum(enum_cls, name=enum_cls.__name__.lower(), native_enum=False, length=32,
                   create_constraint=True, validate_strings=True)


Money = sa.Numeric(18, 2)


def uuid_pk():
    return mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


def created_col():
    return mapped_column(sa.DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_col():
    return mapped_column(sa.DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# --------------------------------------------------------------------------- users
class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[RoleName] = mapped_column(enum_col(RoleName), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    role: Mapped[Role] = relationship(lazy="joined")

    @property
    def role_name(self) -> RoleName:
        return self.role.name


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = uuid_pk()
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_col()


# --------------------------------------------------------------------------- wallets
class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("char_length(currency) = 3 AND currency = upper(currency)", name="currency_iso"),
        Index("ix_wallets_user_status", "user_id", "status"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Main Wallet")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[WalletStatus] = mapped_column(enum_col(WalletStatus), nullable=False, default=WalletStatus.ACTIVE,
                                                 index=True)
    last_frozen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    user: Mapped[User] = relationship(lazy="select")
    balance: Mapped["WalletBalance"] = relationship(back_populates="wallet", uselist=False,
                                                    cascade="all, delete-orphan", lazy="select")

    @property
    def available_balance(self) -> Decimal:
        return self.balance.available_balance

    @property
    def pending_balance(self) -> Decimal:
        return self.balance.pending_balance


class WalletBalance(Base):
    """available_balance = settled funds; pending_balance = part of available currently
    reserved for PENDING outgoing transactions. Spendable = available - pending."""
    __tablename__ = "wallet_balances"
    __table_args__ = (
        CheckConstraint("available_balance >= 0", name="available_non_negative"),
        CheckConstraint("pending_balance >= 0", name="pending_non_negative"),
        CheckConstraint("pending_balance <= available_balance", name="pending_within_available"),
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"), primary_key=True)
    available_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0.00"),
                                                       server_default=text("0"))
    pending_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0.00"),
                                                     server_default=text("0"))
    updated_at: Mapped[datetime] = updated_col()
    wallet: Mapped[Wallet] = relationship(back_populates="balance")


# --------------------------------------------------------------------------- money movement
class _MoneyMovement:
    """Shared columns for deposits / withdrawals."""
    id: Mapped[uuid.UUID] = uuid_pk()
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reference_number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(100))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Deposit(_MoneyMovement, Base):
    __tablename__ = "deposits"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),
                      Index("ix_deposits_wallet_created", "wallet_id", "created_at"))
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[TxStatus] = mapped_column(enum_col(TxStatus), nullable=False, default=TxStatus.PENDING, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    wallet: Mapped[Wallet] = relationship(lazy="select")


class Withdrawal(_MoneyMovement, Base):
    __tablename__ = "withdrawals"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),
                      Index("ix_withdrawals_wallet_created", "wallet_id", "created_at"))
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[TxStatus] = mapped_column(enum_col(TxStatus), nullable=False, default=TxStatus.PENDING, index=True)
    destination: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    wallet: Mapped[Wallet] = relationship(lazy="select")


class Transfer(_MoneyMovement, Base):
    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("sender_wallet_id <> receiver_wallet_id", name="distinct_wallets"),
        Index("ix_transfers_sender_created", "sender_wallet_id", "created_at"),
        Index("ix_transfers_receiver_created", "receiver_wallet_id", "created_at"),
    )
    sender_wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False)
    receiver_wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"),
                                                          nullable=False)
    status: Mapped[TxStatus] = mapped_column(enum_col(TxStatus), nullable=False, default=TxStatus.PENDING, index=True)
    note: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    sender_wallet: Mapped[Wallet] = relationship(foreign_keys=[sender_wallet_id], lazy="select")
    receiver_wallet: Mapped[Wallet] = relationship(foreign_keys=[receiver_wallet_id], lazy="select")


class Transaction(Base):
    """Unified, queryable business-level transaction (one per deposit/withdrawal/transfer/
    refund/adjustment). Powers history, filtering, limits and monitoring."""
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_transactions_wallet_created", "wallet_id", "created_at"),
        Index("ix_transactions_counterparty_created", "counterparty_wallet_id", "created_at"),
        Index("ix_transactions_type_status", "type", "status"),
        Index("ix_transactions_risk", "risk_status"),
        Index("ix_transactions_reference", "reference_entity", "reference_id"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[TransactionType] = mapped_column(enum_col(TransactionType), nullable=False)
    status: Mapped[TxStatus] = mapped_column(enum_col(TxStatus), nullable=False, default=TxStatus.PENDING)
    risk_status: Mapped[RiskStatus] = mapped_column(enum_col(RiskStatus), nullable=False, default=RiskStatus.NORMAL)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False)
    counterparty_wallet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"))
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reference_number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    reference_entity: Mapped[str] = mapped_column(String(30), nullable=False)
    reference_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class LedgerEntry(Base):
    """Immutable, append-only record of every available-balance change."""
    __tablename__ = "transaction_ledger"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("balance_before >= 0 AND balance_after >= 0", name="balances_non_negative"),
        CheckConstraint(
            "(direction = 'CREDIT' AND balance_after = balance_before + amount) OR "
            "(direction = 'DEBIT' AND balance_after = balance_before - amount)", name="balance_arithmetic"),
        Index("ix_ledger_wallet_created", "wallet_id", "created_at"),
        Index("ix_ledger_transaction", "transaction_id"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transactions.id", ondelete="RESTRICT"))
    transaction_type: Mapped[LedgerType] = mapped_column(enum_col(LedgerType), nullable=False)
    direction: Mapped[Direction] = mapped_column(enum_col(Direction), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    balance_before: Mapped[Decimal] = mapped_column(Money, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Money, nullable=False)
    reference_entity: Mapped[str | None] = mapped_column(String(30))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    description: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_col()
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'"))


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        # DB-level guard against duplicate refunds: at most one live refund per original transaction.
        Index("uq_refunds_one_active_per_transaction", "transaction_id", unique=True,
              postgresql_where=text("status IN ('REQUESTED','UNDER_REVIEW','APPROVED','COMPLETED')")),
        Index("ix_refunds_status", "status"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id", ondelete="RESTRICT"),
                                                      nullable=False)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False,
                                                 comment="Wallet that receives the refund (original sender)")
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[RefundStatus] = mapped_column(enum_col(RefundStatus), nullable=False,
                                                 default=RefundStatus.REQUESTED)
    reference_number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    review_notes: Mapped[str | None] = mapped_column(String(500))
    failure_reason: Mapped[str | None] = mapped_column(String(100))
    refund_transaction_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    transaction: Mapped[Transaction] = relationship(lazy="select")


class TransactionLimit(Base):
    """Singleton configuration row (id = 1): limits + monitoring thresholds."""
    __tablename__ = "transaction_limits"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("max_transaction_amount > 0 AND daily_transfer_limit > 0 AND daily_withdrawal_limit > 0 "
                        "AND max_transactions_per_day > 0", name="positive_limits"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    max_transaction_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    daily_transfer_limit: Mapped[Decimal] = mapped_column(Money, nullable=False)
    daily_withdrawal_limit: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_transactions_per_day: Mapped[int] = mapped_column(Integer, nullable=False)
    suspicious_amount_threshold: Mapped[Decimal] = mapped_column(Money, nullable=False)
    velocity_window_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    velocity_review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    velocity_block_count: Mapped[int] = mapped_column(Integer, nullable=False)
    frequency_review_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    recently_frozen_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = updated_col()


class SuspiciousTransaction(Base):
    __tablename__ = "suspicious_transactions"
    __table_args__ = (
        Index("ix_suspicious_review_status", "review_status"),
        Index("ix_suspicious_wallet", "wallet_id"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id", ondelete="RESTRICT"),
                                                      nullable=False, unique=True)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False)
    risk_status: Mapped[RiskStatus] = mapped_column(enum_col(RiskStatus), nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    review_status: Mapped[ReviewStatus] = mapped_column(enum_col(ReviewStatus), nullable=False,
                                                        default=ReviewStatus.PENDING)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    review_notes: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = created_col()

    transaction: Mapped[Transaction] = relationship(lazy="select")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "is_read", "created_at"),
                      Index("ix_notifications_undelivered", "delivered_at"))
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[NotificationType] = mapped_column(enum_col(NotificationType), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=text("false"))
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    created_at: Mapped[datetime] = created_col()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_user_created", "user_id", "created_at"),
                      Index("ix_audit_action", "action"),
                      Index("ix_audit_entity", "entity", "entity_id"))
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    created_at: Mapped[datetime] = created_col()


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "scope", "key", name="uq_idempotency_user_scope_key"),
                      Index("ix_idempotency_created", "created_at"))
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[IdempotencyState] = mapped_column(enum_col(IdempotencyState), nullable=False,
                                                    default=IdempotencyState.IN_PROGRESS)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    created_at: Mapped[datetime] = created_col()

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (LedgerType, Direction, NotificationType, RefundStatus, ReviewStatus, RiskStatus,
                              TransactionType, TxStatus, WalletStatus)
from app.schemas.common import Amount, Currency, ORMModel

SimulatedOutcome = Literal["SUCCESS", "FAILURE", "TIMEOUT"]


# ------------------------------------------------------------------ wallets
class WalletCreate(BaseModel):
    currency: Currency
    name: str = Field(default="Main Wallet", min_length=1, max_length=100)
    model_config = ConfigDict(json_schema_extra={"examples": [{"currency": "INR", "name": "Savings"}]})


class WalletUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    status: WalletStatus | None = None


class WalletOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    currency: str
    available_balance: Decimal
    pending_balance: Decimal
    status: WalletStatus
    created_at: datetime
    updated_at: datetime


class AdjustmentRequest(BaseModel):
    amount: Amount
    direction: Direction
    reason: str = Field(min_length=3, max_length=255)


# ------------------------------------------------------------------ deposits / withdrawals
class DepositCreate(BaseModel):
    amount: Amount
    currency: Currency | None = Field(default=None, description="Optional; must match the wallet currency.")
    simulate_outcome: SimulatedOutcome = Field(default="SUCCESS", description="Payment-gateway simulation hook.")
    model_config = ConfigDict(json_schema_extra={"examples": [{"amount": "1000.00", "currency": "INR"}]})


class DepositOut(ORMModel):
    id: uuid.UUID
    wallet_id: uuid.UUID
    amount: Decimal
    currency: str
    status: TxStatus
    reference_number: str
    failure_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class WithdrawalCreate(BaseModel):
    amount: Amount
    destination: str | None = Field(default=None, max_length=255, description="Bank account / UPI label.")
    simulate_outcome: SimulatedOutcome = "SUCCESS"
    model_config = ConfigDict(json_schema_extra={"examples": [{"amount": "250.00", "destination": "HDFC ****1234"}]})


class WithdrawalOut(ORMModel):
    id: uuid.UUID
    wallet_id: uuid.UUID
    amount: Decimal
    currency: str
    status: TxStatus
    reference_number: str
    destination: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


# ------------------------------------------------------------------ transfers
class TransferCreate(BaseModel):
    sender_wallet_id: uuid.UUID
    receiver_wallet_id: uuid.UUID
    amount: Amount
    currency: Currency | None = Field(default=None, description="Optional; must match both wallets.")
    reference: str | None = Field(default=None, max_length=255, description="Free-text note / reference.")
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "sender_wallet_id": "00000000-0000-0000-0000-000000000001",
        "receiver_wallet_id": "00000000-0000-0000-0000-000000000002",
        "amount": "500.00", "currency": "INR", "reference": "Rent"}]})


class TransferOut(ORMModel):
    id: uuid.UUID
    sender_wallet_id: uuid.UUID
    receiver_wallet_id: uuid.UUID
    amount: Decimal
    currency: str
    status: TxStatus
    reference_number: str
    reference: str | None = Field(default=None, validation_alias="note")
    failure_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


# ------------------------------------------------------------------ transactions / ledger
class TransactionOut(ORMModel):
    id: uuid.UUID
    type: TransactionType
    status: TxStatus
    risk_status: RiskStatus
    wallet_id: uuid.UUID
    counterparty_wallet_id: uuid.UUID | None
    amount: Decimal
    currency: str
    reference_number: str
    reference_entity: str
    reference_id: uuid.UUID
    description: str | None
    created_at: datetime
    completed_at: datetime | None


class LedgerOut(ORMModel):
    id: uuid.UUID
    wallet_id: uuid.UUID
    transaction_id: uuid.UUID | None
    transaction_type: LedgerType
    direction: Direction
    amount: Decimal
    balance_before: Decimal
    balance_after: Decimal
    reference_entity: str | None
    reference_id: uuid.UUID | None
    description: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    metadata: dict = Field(validation_alias="meta")


# ------------------------------------------------------------------ refunds
class RefundCreate(BaseModel):
    transaction_id: uuid.UUID = Field(description="Original completed TRANSFER transaction.")
    amount: Amount | None = Field(default=None, description="Defaults to the full original amount.")
    reason: str = Field(min_length=3, max_length=500)
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "transaction_id": "00000000-0000-0000-0000-000000000003", "amount": "500.00",
        "reason": "Item not delivered"}]})


class RefundDecision(BaseModel):
    notes: str | None = Field(default=None, max_length=500)


class RefundOut(ORMModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    wallet_id: uuid.UUID
    requested_by: uuid.UUID
    amount: Decimal
    currency: str
    reason: str
    status: RefundStatus
    reference_number: str
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_notes: str | None
    failure_reason: str | None
    created_at: datetime
    completed_at: datetime | None


# ------------------------------------------------------------------ limits / suspicious
class LimitsOut(ORMModel):
    max_transaction_amount: Decimal
    daily_transfer_limit: Decimal
    daily_withdrawal_limit: Decimal
    max_transactions_per_day: int
    suspicious_amount_threshold: Decimal
    velocity_window_minutes: int
    velocity_review_count: int
    velocity_block_count: int
    frequency_review_threshold: int
    recently_frozen_hours: int
    updated_by: uuid.UUID | None
    updated_at: datetime


class LimitsUpdate(BaseModel):
    max_transaction_amount: Amount | None = None
    daily_transfer_limit: Amount | None = None
    daily_withdrawal_limit: Amount | None = None
    max_transactions_per_day: int | None = Field(default=None, ge=1)
    suspicious_amount_threshold: Amount | None = None
    velocity_window_minutes: int | None = Field(default=None, ge=1)
    velocity_review_count: int | None = Field(default=None, ge=1)
    velocity_block_count: int | None = Field(default=None, ge=1)
    frequency_review_threshold: int | None = Field(default=None, ge=1)
    recently_frozen_hours: int | None = Field(default=None, ge=0)
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"daily_transfer_limit": "150000.00", "max_transactions_per_day": 40}]})


class SuspiciousOut(ORMModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    wallet_id: uuid.UUID
    risk_status: RiskStatus
    reasons: list
    review_status: ReviewStatus
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_notes: str | None
    created_at: datetime


class SuspiciousReview(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    notes: str | None = Field(default=None, max_length=500)


# ------------------------------------------------------------------ notifications / audit
class NotificationOut(ORMModel):
    id: uuid.UUID
    type: NotificationType
    title: str
    message: str
    is_read: bool
    read_at: datetime | None
    created_at: datetime
    metadata: dict = Field(validation_alias="meta")


class AuditLogOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    action: str
    entity: str | None
    entity_id: str | None
    ip_address: str | None
    metadata: dict = Field(validation_alias="meta")
    created_at: datetime

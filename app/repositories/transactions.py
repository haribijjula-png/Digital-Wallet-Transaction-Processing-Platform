from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (Deposit, LedgerEntry, Transaction, TransactionType, Transfer, TxStatus, Withdrawal)
from app.repositories.base import PageParams, paginate

ACTIVE_STATUSES = (TxStatus.PENDING, TxStatus.PROCESSING, TxStatus.COMPLETED)


class TransactionRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, tx: Transaction) -> Transaction:
        self.db.add(tx)
        self.db.flush()
        return tx

    def get(self, tx_id: uuid.UUID) -> Transaction | None:
        return self.db.get(Transaction, tx_id)

    def get_for_update(self, tx_id: uuid.UUID) -> Transaction | None:
        return self.db.scalar(select(Transaction).where(Transaction.id == tx_id).with_for_update()
                              .execution_options(populate_existing=True))

    def get_by_reference(self, entity: str, ref_id: uuid.UUID) -> Transaction | None:
        return self.db.scalar(select(Transaction).where(Transaction.reference_entity == entity,
                                                        Transaction.reference_id == ref_id))

    # ---- usage queries for limits / monitoring (evaluated while the wallet row is locked)
    def outgoing_total(self, wallet_id: uuid.UUID, tx_type: TransactionType, since: datetime) -> Decimal:
        return self.db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.wallet_id == wallet_id, Transaction.type == tx_type,
            Transaction.status.in_(ACTIVE_STATUSES), Transaction.created_at >= since)) or Decimal("0")

    def outgoing_count(self, wallet_id: uuid.UUID, since: datetime) -> int:
        return self.db.scalar(select(func.count()).select_from(Transaction).where(
            Transaction.wallet_id == wallet_id,
            Transaction.type.in_((TransactionType.TRANSFER, TransactionType.WITHDRAWAL)),
            Transaction.status.in_(ACTIVE_STATUSES), Transaction.created_at >= since)) or 0

    def search(self, params: PageParams, *, wallet_ids: list[uuid.UUID] | None, wallet_id: uuid.UUID | None = None,
               tx_type=None, status=None, risk_status=None, min_amount=None, max_amount=None,
               from_date: date | None = None, to_date: date | None = None, search: str | None = None):
        stmt = select(Transaction)
        if wallet_ids is not None:   # customer scope: own wallets on either side
            stmt = stmt.where(or_(Transaction.wallet_id.in_(wallet_ids),
                                  Transaction.counterparty_wallet_id.in_(wallet_ids)))
        if wallet_id:
            stmt = stmt.where(or_(Transaction.wallet_id == wallet_id, Transaction.counterparty_wallet_id == wallet_id))
        if tx_type:
            stmt = stmt.where(Transaction.type == tx_type)
        if status:
            stmt = stmt.where(Transaction.status == status)
        if risk_status:
            stmt = stmt.where(Transaction.risk_status == risk_status)
        if min_amount is not None:
            stmt = stmt.where(Transaction.amount >= min_amount)
        if max_amount is not None:
            stmt = stmt.where(Transaction.amount <= max_amount)
        if from_date:
            stmt = stmt.where(Transaction.created_at >= datetime.combine(from_date, time.min, tzinfo=timezone.utc))
        if to_date:
            stmt = stmt.where(Transaction.created_at <= datetime.combine(to_date, time.max, tzinfo=timezone.utc))
        if search:
            like = f"%{search}%"
            stmt = stmt.where(or_(Transaction.reference_number.ilike(like), Transaction.description.ilike(like)))
        return paginate(self.db, stmt, params, {
            "created_at": Transaction.created_at, "amount": Transaction.amount, "status": Transaction.status,
            "type": Transaction.type, "completed_at": Transaction.completed_at}, "created_at", Transaction.id)


class LedgerRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, entry: LedgerEntry) -> LedgerEntry:
        self.db.add(entry)
        self.db.flush()
        return entry

    def for_wallet(self, wallet_id: uuid.UUID, params: PageParams, entry_type=None):
        stmt = select(LedgerEntry).where(LedgerEntry.wallet_id == wallet_id)
        if entry_type:
            stmt = stmt.where(LedgerEntry.transaction_type == entry_type)
        return paginate(self.db, stmt, params, {"created_at": LedgerEntry.created_at, "amount": LedgerEntry.amount},
                        "created_at", LedgerEntry.id)

    def for_transaction(self, tx_id: uuid.UUID) -> list[LedgerEntry]:
        return list(self.db.scalars(select(LedgerEntry).where(LedgerEntry.transaction_id == tx_id)
                                    .order_by(LedgerEntry.created_at, LedgerEntry.id)))


class _MovementRepo:
    model = None

    def __init__(self, db: Session):
        self.db = db

    def add(self, obj):
        self.db.add(obj)
        self.db.flush()
        return obj

    def get(self, obj_id: uuid.UUID):
        return self.db.get(self.model, obj_id)

    def get_for_update(self, obj_id: uuid.UUID):
        return self.db.scalar(select(self.model).where(self.model.id == obj_id).with_for_update()
                              .execution_options(populate_existing=True))


class DepositRepository(_MovementRepo):
    model = Deposit

    def list(self, params: PageParams, wallet_ids: list[uuid.UUID] | None, status=None, wallet_id=None):
        stmt = select(Deposit)
        if wallet_ids is not None:
            stmt = stmt.where(Deposit.wallet_id.in_(wallet_ids))
        if wallet_id:
            stmt = stmt.where(Deposit.wallet_id == wallet_id)
        if status:
            stmt = stmt.where(Deposit.status == status)
        return paginate(self.db, stmt, params, {"created_at": Deposit.created_at, "amount": Deposit.amount,
                                                "status": Deposit.status}, "created_at", Deposit.id)

    def stale_pending(self, before: datetime) -> list[Deposit]:
        return list(self.db.scalars(select(Deposit).where(Deposit.status == TxStatus.PENDING,
                                                          Deposit.created_at < before)))

    def retryable(self, max_retries: int) -> list[Deposit]:
        return list(self.db.scalars(select(Deposit).where(
            Deposit.status == TxStatus.FAILED, Deposit.failure_reason == "GATEWAY_TIMEOUT",
            Deposit.retry_count < max_retries)))


class WithdrawalRepository(_MovementRepo):
    model = Withdrawal

    def list(self, params: PageParams, wallet_ids: list[uuid.UUID] | None, status=None, wallet_id=None):
        stmt = select(Withdrawal)
        if wallet_ids is not None:
            stmt = stmt.where(Withdrawal.wallet_id.in_(wallet_ids))
        if wallet_id:
            stmt = stmt.where(Withdrawal.wallet_id == wallet_id)
        if status:
            stmt = stmt.where(Withdrawal.status == status)
        return paginate(self.db, stmt, params, {"created_at": Withdrawal.created_at, "amount": Withdrawal.amount,
                                                "status": Withdrawal.status}, "created_at", Withdrawal.id)

    def retryable(self, max_retries: int) -> list[Withdrawal]:
        return list(self.db.scalars(select(Withdrawal).where(
            Withdrawal.status == TxStatus.FAILED, Withdrawal.failure_reason == "GATEWAY_TIMEOUT",
            Withdrawal.retry_count < max_retries)))


class TransferRepository(_MovementRepo):
    model = Transfer

    def list(self, params: PageParams, wallet_ids: list[uuid.UUID] | None, status=None, wallet_id=None):
        stmt = select(Transfer)
        if wallet_ids is not None:
            stmt = stmt.where(or_(Transfer.sender_wallet_id.in_(wallet_ids),
                                  Transfer.receiver_wallet_id.in_(wallet_ids)))
        if wallet_id:
            stmt = stmt.where(or_(Transfer.sender_wallet_id == wallet_id, Transfer.receiver_wallet_id == wallet_id))
        if status:
            stmt = stmt.where(Transfer.status == status)
        return paginate(self.db, stmt, params, {"created_at": Transfer.created_at, "amount": Transfer.amount,
                                                "status": Transfer.status}, "created_at", Transfer.id)

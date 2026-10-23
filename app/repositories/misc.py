from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import (AuditLog, IdempotencyKey, Notification, Refund, RefundStatus, ReviewStatus,
                        SuspiciousTransaction, TransactionLimit, Transaction)
from app.repositories.base import PageParams, paginate


class RefundRepository:
    LIVE = (RefundStatus.REQUESTED, RefundStatus.UNDER_REVIEW, RefundStatus.APPROVED, RefundStatus.COMPLETED)

    def __init__(self, db: Session):
        self.db = db

    def add(self, refund: Refund) -> Refund:
        self.db.add(refund)
        self.db.flush()
        return refund

    def get(self, refund_id: uuid.UUID) -> Refund | None:
        return self.db.get(Refund, refund_id)

    def get_for_update(self, refund_id: uuid.UUID) -> Refund | None:
        return self.db.scalar(select(Refund).where(Refund.id == refund_id).with_for_update()
                              .execution_options(populate_existing=True))

    def live_for_transaction(self, tx_id: uuid.UUID) -> Refund | None:
        return self.db.scalar(select(Refund).where(Refund.transaction_id == tx_id, Refund.status.in_(self.LIVE)))

    def list(self, params: PageParams, requested_by: uuid.UUID | None = None, status: RefundStatus | None = None,
             transaction_id: uuid.UUID | None = None):
        stmt = select(Refund)
        if requested_by:
            stmt = stmt.where(Refund.requested_by == requested_by)
        if status:
            stmt = stmt.where(Refund.status == status)
        if transaction_id:
            stmt = stmt.where(Refund.transaction_id == transaction_id)
        return paginate(self.db, stmt, params, {"created_at": Refund.created_at, "amount": Refund.amount,
                                                "status": Refund.status}, "created_at", Refund.id)

    def count_open(self) -> int:
        return self.db.scalar(select(func.count()).select_from(Refund).where(
            Refund.status.in_((RefundStatus.REQUESTED, RefundStatus.UNDER_REVIEW)))) or 0


class LimitRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self) -> TransactionLimit:
        return self.db.scalar(select(TransactionLimit).where(TransactionLimit.id == 1))

    def get_for_update(self) -> TransactionLimit:
        return self.db.scalar(select(TransactionLimit).where(TransactionLimit.id == 1).with_for_update()
                              .execution_options(populate_existing=True))


class SuspiciousRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, obj: SuspiciousTransaction) -> SuspiciousTransaction:
        self.db.add(obj)
        self.db.flush()
        return obj

    def get(self, sid: uuid.UUID) -> SuspiciousTransaction | None:
        return self.db.get(SuspiciousTransaction, sid)

    def get_for_update(self, sid: uuid.UUID) -> SuspiciousTransaction | None:
        return self.db.scalar(select(SuspiciousTransaction).where(SuspiciousTransaction.id == sid).with_for_update()
                              .execution_options(populate_existing=True))

    def by_transaction(self, tx_id: uuid.UUID) -> SuspiciousTransaction | None:
        return self.db.scalar(select(SuspiciousTransaction).where(SuspiciousTransaction.transaction_id == tx_id))

    def list(self, params: PageParams, review_status: ReviewStatus | None = None, risk_status=None,
             wallet_id: uuid.UUID | None = None):
        stmt = select(SuspiciousTransaction)
        if review_status:
            stmt = stmt.where(SuspiciousTransaction.review_status == review_status)
        if risk_status:
            stmt = stmt.where(SuspiciousTransaction.risk_status == risk_status)
        if wallet_id:
            stmt = stmt.where(SuspiciousTransaction.wallet_id == wallet_id)
        return paginate(self.db, stmt, params, {"created_at": SuspiciousTransaction.created_at},
                        "created_at", SuspiciousTransaction.id)

    def count(self, review_status: ReviewStatus | None = None) -> int:
        stmt = select(func.count()).select_from(SuspiciousTransaction)
        if review_status:
            stmt = stmt.where(SuspiciousTransaction.review_status == review_status)
        return self.db.scalar(stmt) or 0


class NotificationRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, n: Notification) -> Notification:
        self.db.add(n)
        return n

    def get(self, nid: uuid.UUID) -> Notification | None:
        return self.db.get(Notification, nid)

    def list(self, params: PageParams, user_id: uuid.UUID, unread_only: bool = False):
        stmt = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        return paginate(self.db, stmt, params, {"created_at": Notification.created_at}, "created_at", Notification.id)

    def mark_all_read(self, user_id: uuid.UUID, now: datetime) -> int:
        res = self.db.execute(update(Notification).where(Notification.user_id == user_id,
                                                         Notification.is_read.is_(False))
                              .values(is_read=True, read_at=now))
        return res.rowcount

    def unread_count(self, user_id: uuid.UUID) -> int:
        return self.db.scalar(select(func.count()).select_from(Notification).where(
            Notification.user_id == user_id, Notification.is_read.is_(False))) or 0


class AuditRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, log: AuditLog) -> AuditLog:
        self.db.add(log)
        return log

    def list(self, params: PageParams, user_id=None, action=None, entity=None, entity_id=None):
        stmt = select(AuditLog)
        if user_id:
            stmt = stmt.where(AuditLog.user_id == user_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if entity:
            stmt = stmt.where(AuditLog.entity == entity)
        if entity_id:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        return paginate(self.db, stmt, params, {"created_at": AuditLog.created_at, "action": AuditLog.action},
                        "created_at", AuditLog.id)


class IdempotencyRepository:
    def __init__(self, db: Session):
        self.db = db

    def try_insert(self, user_id: uuid.UUID, scope: str, key: str, request_hash: str) -> uuid.UUID | None:
        """INSERT .. ON CONFLICT DO NOTHING. A concurrent identical insert blocks on the unique
        index until the first transaction commits/rolls back, then either conflicts (-> replay)
        or inserts (first attempt failed and rolled back)."""
        stmt = (pg_insert(IdempotencyKey)
                .values(id=uuid.uuid4(), user_id=user_id, scope=scope, key=key, request_hash=request_hash,
                        state="IN_PROGRESS")
                .on_conflict_do_nothing(index_elements=["user_id", "scope", "key"])
                .returning(IdempotencyKey.id))
        return self.db.execute(stmt).scalar_one_or_none()

    def get(self, row_id: uuid.UUID) -> IdempotencyKey:
        return self.db.scalar(select(IdempotencyKey).where(IdempotencyKey.id == row_id)
                              .execution_options(populate_existing=True))

    def find(self, user_id: uuid.UUID, scope: str, key: str) -> IdempotencyKey | None:
        return self.db.scalar(select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id, IdempotencyKey.scope == scope, IdempotencyKey.key == key)
            .execution_options(populate_existing=True))

"""Shared building blocks for deposits, withdrawals, transfers and refunds."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.security import utcnow
from app.domain.state_machines import TX_TRANSITIONS, transition
from app.models import (NotificationType, ReviewStatus, RiskStatus, SuspiciousTransaction, Transaction,
                        TransactionType, TxStatus, Wallet)
from app.repositories.misc import SuspiciousRepository
from app.repositories.transactions import TransactionRepository
from app.repositories.users import UserRepository
from app.repositories.wallets import WalletRepository
from app.services.audit import AuditService
from app.services.idempotency import IdempotencyService
from app.services.ledger import LedgerService
from app.services.notifications import NotificationService
from app.services.policy import PolicyService, RiskDecision


def fmt(amount: Decimal, currency: str) -> str:
    return f"{currency} {amount:.2f}"


class MovementSupport:
    def __init__(self, db: Session):
        self.db = db
        self.wallets = WalletRepository(db)
        self.txs = TransactionRepository(db)
        self.suspicious = SuspiciousRepository(db)
        self.users = UserRepository(db)
        self.audit = AuditService(db)
        self.notify = NotificationService(db)
        self.ledger = LedgerService(db)
        self.policy = PolicyService(db)
        self.idem = IdempotencyService(db)

    def new_tx(self, *, type_: TransactionType, wallet_id: uuid.UUID, counterparty: uuid.UUID | None,
               amount: Decimal, currency: str, reference_number: str, entity: str, ref_id: uuid.UUID,
               user_id: uuid.UUID | None, description: str | None,
               risk: RiskStatus = RiskStatus.NORMAL) -> Transaction:
        tx = Transaction(type=type_, status=TxStatus.PENDING, risk_status=risk, wallet_id=wallet_id,
                         counterparty_wallet_id=counterparty, amount=amount, currency=currency,
                         reference_number=reference_number, reference_entity=entity, reference_id=ref_id,
                         created_by=user_id, description=description)
        return self.txs.add(tx)

    def set_status(self, entity, tx: Transaction, new: TxStatus, label: str) -> None:
        """Moves the domain object and its unified transaction row in lock-step."""
        transition(entity, new, TX_TRANSITIONS, label)
        transition(tx, new, TX_TRANSITIONS, "transaction")
        if new == TxStatus.COMPLETED:
            now = utcnow()
            entity.completed_at = now
            tx.completed_at = now
        self.db.flush()

    def flag(self, tx: Transaction, wallet: Wallet, decision: RiskDecision, actor_id: uuid.UUID | None,
             ip: str | None) -> SuspiciousTransaction:
        blocked = decision.risk_status == RiskStatus.BLOCKED
        rec = self.suspicious.add(SuspiciousTransaction(
            transaction_id=tx.id, wallet_id=wallet.id, risk_status=decision.risk_status,
            reasons=decision.reasons,
            review_status=ReviewStatus.NOT_REQUIRED if blocked else ReviewStatus.PENDING))
        self.audit.log("TRANSACTION_BLOCKED" if blocked else "TRANSACTION_FLAGGED", user_id=actor_id,
                       entity="transaction", entity_id=tx.id, ip=ip,
                       meta={"reasons": decision.reasons, "amount": tx.amount, "wallet_id": wallet.id})
        staff = self.users.staff_ids()
        title = "Suspicious transaction blocked" if blocked else "Suspicious transaction needs review"
        self.notify.notify_many(staff, NotificationType.SUSPICIOUS_TRANSACTION, title,
                                f"{tx.type.value} {fmt(tx.amount, tx.currency)} ({', '.join(decision.reasons)}).",
                                {"transaction_id": tx.id, "suspicious_id": rec.id})
        if not blocked:
            self.notify.notify(wallet.user_id, NotificationType.TRANSACTION_UNDER_REVIEW, "Transaction under review",
                               f"Your {tx.type.value.lower()} of {fmt(tx.amount, tx.currency)} is under review "
                               f"and will be processed once approved.", {"transaction_id": tx.id})
        return rec

    @staticmethod
    def now() -> datetime:
        return utcnow()


def run_idempotent(db: Session, idem: IdempotencyService, user_id, scope: str, key: str, payload: dict, fn):
    """Common envelope: replay if the key was seen; otherwise run `fn(row)` and roll back
    on any error (the key row disappears with the failed transaction, so the client may retry).
    `fn` returns an ApiResult and is responsible for committing."""
    row, replay = idem.begin(user_id, scope, key, payload)
    if replay:
        return replay
    try:
        return fn(row)
    except AppError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

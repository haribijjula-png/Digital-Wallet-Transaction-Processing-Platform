import uuid

from sqlalchemy.orm import Session

from app.core.exceptions import InvalidStateTransitionError, NotFoundError
from app.core.security import utcnow
from app.models import ReviewStatus, RiskStatus, TxStatus, User
from app.repositories.base import PageParams
from app.repositories.misc import SuspiciousRepository
from app.schemas.wallets import SuspiciousReview
from app.services.audit import AuditService
from app.services.transfers import TransferService
from app.services.withdrawals import WithdrawalService


class SuspiciousService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = SuspiciousRepository(db)
        self.audit = AuditService(db)

    def get(self, sid: uuid.UUID):
        rec = self.repo.get(sid)
        if not rec:
            raise NotFoundError("Suspicious transaction record not found.", code="SUSPICIOUS_NOT_FOUND")
        return rec

    def list(self, params: PageParams, review_status=None, risk_status=None, wallet_id=None):
        return self.repo.list(params, review_status, risk_status, wallet_id)

    def review(self, staff: User, sid: uuid.UUID, payload: SuspiciousReview, ip):
        """APPROVE releases the held funds (settles the pending transfer/withdrawal);
        REJECT releases the reservation and fails the transaction. Atomic with the review record."""
        rec = self.repo.get_for_update(sid)               # lock order: suspicious -> entity -> wallets
        if not rec:
            raise NotFoundError("Suspicious transaction record not found.", code="SUSPICIOUS_NOT_FOUND")
        if rec.review_status != ReviewStatus.PENDING:
            raise InvalidStateTransitionError(f"This record is already {rec.review_status.value}.")
        tx = rec.transaction
        approve = payload.decision == "APPROVE"
        if tx.status == TxStatus.PENDING:                 # held transaction awaiting a decision
            svc = TransferService(self.db) if tx.reference_entity == "transfer" else WithdrawalService(self.db)
            if approve:
                svc.complete_pending(tx.reference_id, staff, ip, commit=False)
            else:
                svc.reject_pending(tx.reference_id, staff, ip)
        # (post-hoc scan flags on already-settled transactions are simply acknowledged)
        rec.review_status = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
        rec.reviewed_by, rec.reviewed_at, rec.review_notes = staff.id, utcnow(), payload.notes
        if approve:
            tx.risk_status = RiskStatus.NORMAL        # cleared by the reviewer
        self.audit.log("SUSPICIOUS_TRANSACTION_REVIEWED", user_id=staff.id, entity="suspicious_transaction",
                       entity_id=rec.id, ip=ip, meta={"decision": payload.decision, "transaction_id": tx.id})
        self.db.commit()
        return rec

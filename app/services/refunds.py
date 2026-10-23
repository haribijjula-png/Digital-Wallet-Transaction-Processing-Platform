import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (DuplicateTransactionError, ForbiddenError, InsufficientBalanceError,
                                 InvalidRefundError, NotFoundError, WalletClosedError)
from app.core.security import utcnow
from app.domain.state_machines import REFUND_TRANSITIONS, ensure_transition, transition
from app.models import (LedgerType, NotificationType, Refund, RefundStatus, TransactionType, TxStatus, User,
                        WalletStatus)
from app.repositories.base import PageParams, new_ref
from app.repositories.misc import RefundRepository
from app.schemas.wallets import RefundCreate, RefundDecision, RefundOut
from app.services.common import ApiResult, is_staff
from app.services.movement import MovementSupport, fmt, run_idempotent


class RefundService:
    """Refund = reversal of (part of) a COMPLETED transfer, requested by the sender and
    approved by staff. On approval funds move receiver -> sender with two REFUND ledger entries."""

    def __init__(self, db: Session):
        self.db = db
        self.s = MovementSupport(db)
        self.refunds = RefundRepository(db)

    # ------------------------------------------------------------------ queries
    def get_authorized(self, user: User, refund_id: uuid.UUID) -> Refund:
        r = self.refunds.get(refund_id)
        if not r:
            raise NotFoundError("Refund not found.", code="REFUND_NOT_FOUND")
        if not is_staff(user) and r.requested_by != user.id:
            raise ForbiddenError("You do not have access to this refund.", code="REFUND_ACCESS_DENIED")
        return r

    def list(self, user: User, params: PageParams, status=None, transaction_id=None):
        return self.refunds.list(params, None if is_staff(user) else user.id, status, transaction_id)

    # ------------------------------------------------------------------ create (idempotent)
    def create(self, user: User, payload: RefundCreate, key: str, ip) -> ApiResult:
        body = payload.model_dump(mode="json")
        return run_idempotent(self.db, self.s.idem, user.id, "refund.create", key, body,
                              lambda row: self._create(row, user, payload, ip))

    def _create(self, row, user: User, payload: RefundCreate, ip) -> ApiResult:
        tx = self.s.txs.get_for_update(payload.transaction_id)        # serialises refunds per transaction
        if not tx:
            raise NotFoundError("Original transaction not found.", code="TRANSACTION_NOT_FOUND")
        sender = self.s.wallets.get(tx.wallet_id)
        if sender.user_id != user.id:
            raise ForbiddenError("Only the sender of the original transfer can request a refund.",
                                 code="TRANSACTION_ACCESS_DENIED")
        if tx.type != TransactionType.TRANSFER or tx.status != TxStatus.COMPLETED:
            raise InvalidRefundError("Only COMPLETED transfers can be refunded.")
        receiver = self.s.wallets.get(tx.counterparty_wallet_id)
        for w in (sender, receiver):
            if w.status == WalletStatus.CLOSED:
                raise WalletClosedError("Refunds involving a closed wallet are not allowed.")
        amount = payload.amount or tx.amount
        if amount > tx.amount:
            raise InvalidRefundError("Refund amount exceeds the original transaction amount.")
        if self.refunds.live_for_transaction(tx.id):
            raise DuplicateTransactionError("A refund for this transaction already exists.")

        try:
            refund = self.refunds.add(Refund(
                id=uuid.uuid4(), transaction_id=tx.id, wallet_id=sender.id, requested_by=user.id, amount=amount,
                currency=tx.currency, reason=payload.reason, reference_number=new_ref("RFD"),
                status=RefundStatus.REQUESTED))
        except IntegrityError:           # DB-level guard (partial unique index) for any residual race
            self.db.rollback()
            raise DuplicateTransactionError("A refund for this transaction already exists.")
        self.s.audit.log("REFUND_CREATED", user_id=user.id, entity="refund", entity_id=refund.id, ip=ip,
                         meta={"transaction_id": tx.id, "amount": amount})
        body = RefundOut.model_validate(refund).model_dump(mode="json")
        self.s.idem.complete(row, 201, body, refund.id)
        self.db.commit()
        return ApiResult(201, body)

    # ------------------------------------------------------------------ review
    def start_review(self, staff: User, refund_id: uuid.UUID, ip) -> Refund:
        r = self._locked(refund_id)
        transition(r, RefundStatus.UNDER_REVIEW, REFUND_TRANSITIONS, "refund")
        r.reviewed_by = staff.id
        self.s.audit.log("REFUND_REVIEW_STARTED", user_id=staff.id, entity="refund", entity_id=r.id, ip=ip)
        self.db.commit()
        return r

    def reject(self, staff: User, refund_id: uuid.UUID, payload: RefundDecision, ip) -> Refund:
        r = self._locked(refund_id)
        transition(r, RefundStatus.REJECTED, REFUND_TRANSITIONS, "refund")
        r.reviewed_by, r.reviewed_at, r.review_notes = staff.id, utcnow(), payload.notes
        self.s.notify.notify(r.requested_by, NotificationType.REFUND_REJECTED, "Refund rejected",
                             f"Your refund request {r.reference_number} was rejected.", {"refund_id": r.id})
        self.s.audit.log("REFUND_REJECTED", user_id=staff.id, entity="refund", entity_id=r.id, ip=ip)
        self.db.commit()
        return r

    def approve(self, staff: User, refund_id: uuid.UUID, payload: RefundDecision, ip) -> Refund:
        r = self._locked(refund_id)
        orig = self.s.txs.get(r.transaction_id)
        sender_id, receiver_id = r.wallet_id, orig.counterparty_wallet_id
        locked = self.s.wallets.lock_many([sender_id, receiver_id])
        sender, receiver = locked[sender_id], locked[receiver_id]

        transition(r, RefundStatus.APPROVED, REFUND_TRANSITIONS, "refund")
        r.reviewed_by, r.reviewed_at, r.review_notes = staff.id, utcnow(), payload.notes
        self.s.audit.log("REFUND_APPROVED", user_id=staff.id, entity="refund", entity_id=r.id, ip=ip,
                         meta={"amount": r.amount})

        failure = None
        if WalletStatus.CLOSED in (sender.status, receiver.status):
            failure = "WALLET_CLOSED"
        elif self.s.ledger.spendable(receiver) < r.amount:
            failure = InsufficientBalanceError.code
        if failure:
            transition(r, RefundStatus.FAILED, REFUND_TRANSITIONS, "refund")
            r.failure_reason = failure
            self.s.audit.log("REFUND_FAILED", user_id=staff.id, entity="refund", entity_id=r.id, ip=ip,
                             meta={"reason": failure})
            self.db.commit()
            return r

        rtx = self.s.new_tx(type_=TransactionType.REFUND, wallet_id=sender.id, counterparty=receiver.id,
                            amount=r.amount, currency=r.currency, reference_number=r.reference_number,
                            entity="refund", ref_id=r.id, user_id=staff.id,
                            description=f"Refund of {orig.reference_number}")
        self.s.ledger.debit(receiver, r.amount, LedgerType.REFUND, transaction=rtx, created_by=staff.id,
                            description=f"Refund {r.reference_number} to wallet {sender.id}",
                            meta={"original_transaction_id": orig.id, "counterparty_wallet_id": sender.id})
        self.s.ledger.credit(sender, r.amount, LedgerType.REFUND, transaction=rtx, created_by=staff.id,
                             description=f"Refund {r.reference_number} from wallet {receiver.id}",
                             meta={"original_transaction_id": orig.id, "counterparty_wallet_id": receiver.id})
        rtx.status = TxStatus.COMPLETED
        rtx.completed_at = utcnow()
        transition(r, RefundStatus.COMPLETED, REFUND_TRANSITIONS, "refund")
        r.completed_at = rtx.completed_at
        r.refund_transaction_id = rtx.id
        self.s.notify.notify(r.requested_by, NotificationType.REFUND_APPROVED, "Refund approved",
                             f"Your refund of {fmt(r.amount, r.currency)} was approved and credited.",
                             {"refund_id": r.id})
        self.s.audit.log("REFUND_COMPLETED", user_id=staff.id, entity="refund", entity_id=r.id, ip=ip)
        self.db.commit()
        return r

    def _locked(self, refund_id: uuid.UUID) -> Refund:
        r = self.refunds.get_for_update(refund_id)
        if not r:
            raise NotFoundError("Refund not found.", code="REFUND_NOT_FOUND")
        return r

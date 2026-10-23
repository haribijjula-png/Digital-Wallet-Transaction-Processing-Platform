import uuid

from sqlalchemy.orm import Session

from app.core.exceptions import (AppError, ForbiddenError, InsufficientBalanceError, InvalidStateTransitionError,
                                 InvalidTransferError, NotFoundError)
from app.core.security import utcnow
from app.models import (LedgerType, NotificationType, ReviewStatus, RiskStatus, Transfer, TransactionType, TxStatus,
                        User)
from app.repositories.base import PageParams, new_ref
from app.repositories.transactions import TransferRepository
from app.schemas.wallets import TransferCreate, TransferOut
from app.services.access import assert_wallet_owner, visible_wallet_ids
from app.services.common import ApiResult, is_admin, is_staff
from app.services.ledger import assert_wallet_active
from app.services.movement import MovementSupport, fmt, run_idempotent
from app.services.policy import RiskDecision


class TransferService:
    def __init__(self, db: Session):
        self.db = db
        self.s = MovementSupport(db)
        self.transfers = TransferRepository(db)

    # ------------------------------------------------------------------ queries
    def get_authorized(self, user: User, transfer_id: uuid.UUID) -> Transfer:
        t = self.transfers.get(transfer_id)
        if not t:
            raise NotFoundError("Transfer not found.", code="TRANSFER_NOT_FOUND")
        if not is_staff(user):
            owners = {self.s.wallets.get(t.sender_wallet_id).user_id, self.s.wallets.get(t.receiver_wallet_id).user_id}
            if user.id not in owners:
                raise ForbiddenError("You do not have access to this transfer.", code="TRANSFER_ACCESS_DENIED")
        return t

    def list(self, user: User, params: PageParams, status=None, wallet_id=None):
        return self.transfers.list(params, visible_wallet_ids(user, self.s.wallets), status, wallet_id)

    # ------------------------------------------------------------------ create (idempotent)
    def create(self, user: User, payload: TransferCreate, key: str, ip) -> ApiResult:
        body = payload.model_dump(mode="json")
        return run_idempotent(self.db, self.s.idem, user.id, "transfer.create", key, body,
                              lambda row: self._create(row, user, payload, ip))

    def _create(self, row, user: User, payload: TransferCreate, ip) -> ApiResult:
        # 1. cheap, lock-free validation (existence / ownership / self-transfer)
        sender_ref = self.s.wallets.get(payload.sender_wallet_id)
        if not sender_ref:
            raise NotFoundError("Sender wallet not found.", code="WALLET_NOT_FOUND")
        assert_wallet_owner(user, sender_ref)
        receiver_ref = self.s.wallets.get(payload.receiver_wallet_id)
        if not receiver_ref:
            raise NotFoundError("Receiver wallet not found.", code="WALLET_NOT_FOUND")
        if sender_ref.id == receiver_ref.id or receiver_ref.user_id == user.id:
            raise InvalidTransferError("You cannot transfer money to yourself.")

        # 2. lock BOTH wallets (ordered by id => no deadlocks) and re-read their state
        locked = self.s.wallets.lock_many([sender_ref.id, receiver_ref.id])
        sender, receiver = locked[sender_ref.id], locked[receiver_ref.id]
        assert_wallet_active(sender, "Sender wallet")
        assert_wallet_active(receiver, "Receiver wallet")
        if sender.currency != receiver.currency:
            raise InvalidTransferError("Sender and receiver wallet currencies must match.")
        if payload.currency and payload.currency.upper() != sender.currency:
            raise InvalidTransferError("Transfer currency must match the wallet currency.")

        # 3. policy (limits + suspicious rules) evaluated under the sender lock
        decision = self.s.policy.evaluate_outgoing(sender, TransactionType.TRANSFER, payload.amount, utcnow())
        t = self.transfers.add(Transfer(
            id=uuid.uuid4(), sender_wallet_id=sender.id, receiver_wallet_id=receiver.id, amount=payload.amount,
            currency=sender.currency, reference_number=new_ref("TRF"), note=payload.reference,
            status=TxStatus.PENDING, created_by=user.id))
        tx = self.s.new_tx(type_=TransactionType.TRANSFER, wallet_id=sender.id, counterparty=receiver.id,
                           amount=t.amount, currency=t.currency, reference_number=t.reference_number,
                           entity="transfer", ref_id=t.id, user_id=user.id, description=payload.reference,
                           risk=decision.risk_status)
        self.s.audit.log("TRANSFER_CREATED", user_id=user.id, entity="transfer", entity_id=t.id, ip=ip,
                         meta={"amount": t.amount, "sender": sender.id, "receiver": receiver.id})

        if self.s.ledger.spendable(sender) < t.amount:
            return self._fail(row, t, tx, sender, InsufficientBalanceError(), user, ip, None)
        if decision.error:
            return self._fail(row, t, tx, sender, decision.error, user, ip, decision)

        if decision.risk_status == RiskStatus.REVIEW_REQUIRED:
            self.s.ledger.reserve(sender, t.amount)      # hold funds; settle on approval
            self.s.flag(tx, sender, decision, user.id, ip)
            status = 202
        else:
            self._settle(t, tx, sender, receiver, from_reserved=False, actor_id=user.id)
            status = 201
        body = TransferOut.model_validate(t).model_dump(mode="json")
        self.s.idem.complete(row, status, body, t.id)
        self.db.commit()
        return ApiResult(status, body)

    def _fail(self, row, t: Transfer, tx, sender, err: AppError, user, ip, decision: RiskDecision | None):
        """Persist the FAILED attempt (history, audit, notification), then surface the error.
        The sender balance is untouched: nothing was debited."""
        t.failure_reason = err.code
        self.s.set_status(t, tx, TxStatus.FAILED, "transfer")
        tx.risk_status = decision.risk_status if decision is not None else RiskStatus.NORMAL
        if decision is not None:
            self.s.flag(tx, sender, decision, user.id, ip)
        self.s.audit.log("TRANSFER_FAILED", user_id=user.id, entity="transfer", entity_id=t.id, ip=ip,
                         meta={"reason": err.code, "message": err.message})
        self.s.notify.notify(sender.user_id, NotificationType.TRANSFER_FAILED, "Transfer failed",
                             f"Your transfer of {fmt(t.amount, t.currency)} failed: {err.message}",
                             {"transfer_id": t.id})
        self.s.idem.complete_with_error(row, err)
        self.db.commit()
        raise err

    # ------------------------------------------------------------------ settlement (atomic debit + credit)
    def _settle(self, t: Transfer, tx, sender, receiver, *, from_reserved: bool, actor_id) -> None:
        """Debit sender + credit receiver inside the caller's single DB transaction: either both
        ledger entries and balance updates commit, or none do."""
        self.s.set_status(t, tx, TxStatus.PROCESSING, "transfer")
        self.s.ledger.debit(sender, t.amount, LedgerType.TRANSFER_DEBIT, transaction=tx, created_by=actor_id,
                            description=f"Transfer {t.reference_number} to wallet {receiver.id}",
                            from_reserved=from_reserved, meta={"counterparty_wallet_id": receiver.id})
        self.s.ledger.credit(receiver, t.amount, LedgerType.TRANSFER_CREDIT, transaction=tx, created_by=actor_id,
                             description=f"Transfer {t.reference_number} from wallet {sender.id}",
                             meta={"counterparty_wallet_id": sender.id})
        self.s.set_status(t, tx, TxStatus.COMPLETED, "transfer")
        self.s.notify.notify(sender.user_id, NotificationType.TRANSFER_COMPLETED, "Transfer completed",
                             f"You sent {fmt(t.amount, t.currency)}.", {"transfer_id": t.id})
        self.s.notify.notify(receiver.user_id, NotificationType.TRANSFER_RECEIVED, "Transfer received",
                             f"You received {fmt(t.amount, t.currency)}.", {"transfer_id": t.id})
        self.s.audit.log("TRANSFER_COMPLETED", user_id=actor_id, entity="transfer", entity_id=t.id,
                         meta={"amount": t.amount})

    # ------------------------------------------------------------------ pending (review) lifecycle
    def complete_pending(self, transfer_id: uuid.UUID, actor: User, ip, commit: bool = True) -> Transfer:
        t = self.transfers.get_for_update(transfer_id)
        locked = self.s.wallets.lock_many([t.sender_wallet_id, t.receiver_wallet_id])
        sender, receiver = locked[t.sender_wallet_id], locked[t.receiver_wallet_id]
        if t.status != TxStatus.PENDING:
            raise InvalidStateTransitionError(f"Transfer is {t.status.value}, not PENDING.")
        tx = self.s.txs.get_by_reference("transfer", t.id)
        if sender.status.value != "ACTIVE" or receiver.status.value != "ACTIVE":
            self.s.ledger.release(sender, t.amount)
            t.failure_reason = "WALLET_NOT_ACTIVE"
            self.s.set_status(t, tx, TxStatus.FAILED, "transfer")
            self.s.notify.notify(sender.user_id, NotificationType.TRANSFER_FAILED, "Transfer failed",
                                 "A wallet involved in the transfer is no longer active.", {"transfer_id": t.id})
            self.s.audit.log("TRANSFER_FAILED", user_id=actor.id, entity="transfer", entity_id=t.id, ip=ip,
                             meta={"reason": "WALLET_NOT_ACTIVE"})
        else:
            self._settle(t, tx, sender, receiver, from_reserved=True, actor_id=actor.id)
        if commit:
            self.db.commit()
        return t

    def reject_pending(self, transfer_id: uuid.UUID, actor: User, ip, reason: str = "REJECTED_BY_REVIEWER") -> Transfer:
        t = self.transfers.get_for_update(transfer_id)
        sender = self.s.wallets.lock_many([t.sender_wallet_id])[t.sender_wallet_id]
        tx = self.s.txs.get_by_reference("transfer", t.id)
        self.s.ledger.release(sender, t.amount)
        t.failure_reason = reason
        self.s.set_status(t, tx, TxStatus.FAILED, "transfer")
        self.s.notify.notify(sender.user_id, NotificationType.TRANSFER_FAILED, "Transfer rejected",
                             f"Your transfer of {fmt(t.amount, t.currency)} was rejected after review.",
                             {"transfer_id": t.id})
        self.s.audit.log("TRANSFER_FAILED", user_id=actor.id, entity="transfer", entity_id=t.id, ip=ip,
                         meta={"reason": reason})
        return t

    def cancel(self, user: User, transfer_id: uuid.UUID, ip, system: bool = False) -> Transfer:
        t = self.transfers.get(transfer_id)
        if not t:
            raise NotFoundError("Transfer not found.", code="TRANSFER_NOT_FOUND")
        sender_ref = self.s.wallets.get(t.sender_wallet_id)
        if not system and not (sender_ref.user_id == user.id or is_admin(user)):
            raise ForbiddenError("Only the sender or an administrator can cancel a transfer.",
                                 code="TRANSFER_ACCESS_DENIED")
        tx = self.s.txs.get_by_reference("transfer", t.id)
        rec = self.s.suspicious.by_transaction(tx.id)
        if rec:                                              # lock order: suspicious -> transfer -> wallet
            self.s.suspicious.get_for_update(rec.id)
        t = self.transfers.get_for_update(transfer_id)
        sender = self.s.wallets.lock_many([t.sender_wallet_id])[t.sender_wallet_id]
        if t.status != TxStatus.PENDING:
            raise InvalidStateTransitionError(f"Only PENDING transfers can be cancelled (current: {t.status.value}).")
        self.s.ledger.release(sender, t.amount)
        self.s.set_status(t, tx, TxStatus.CANCELLED, "transfer")
        if rec:
            rec.review_status = ReviewStatus.CANCELLED
        self.s.audit.log("TRANSFER_CANCELLED", user_id=None if system else user.id, entity="transfer",
                         entity_id=t.id, ip=ip, meta={"system": system})
        self.db.commit()
        return t

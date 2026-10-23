import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (AppError, ForbiddenError, InsufficientBalanceError, InvalidStateTransitionError,
                                 NotFoundError)
from app.models import (LedgerType, NotificationType, ReviewStatus, RiskStatus, TransactionType, TxStatus, User,
                        Withdrawal)
from app.repositories.base import PageParams, new_ref
from app.repositories.transactions import WithdrawalRepository
from app.schemas.wallets import WithdrawalCreate, WithdrawalOut
from app.services.access import assert_wallet_access, assert_wallet_owner, visible_wallet_ids
from app.services.common import ApiResult, is_admin
from app.services.ledger import assert_wallet_active
from app.services.movement import MovementSupport, fmt, run_idempotent
from app.services.policy import RiskDecision
from app.core.security import utcnow


class WithdrawalService:
    def __init__(self, db: Session):
        self.db = db
        self.s = MovementSupport(db)
        self.withdrawals = WithdrawalRepository(db)

    # ------------------------------------------------------------------ queries
    def get_authorized(self, user: User, wid: uuid.UUID) -> Withdrawal:
        w = self.withdrawals.get(wid)
        if not w:
            raise NotFoundError("Withdrawal not found.", code="WITHDRAWAL_NOT_FOUND")
        assert_wallet_access(user, self.s.wallets.get(w.wallet_id))
        return w

    def list(self, user: User, params: PageParams, status=None, wallet_id=None):
        return self.withdrawals.list(params, visible_wallet_ids(user, self.s.wallets), status, wallet_id)

    # ------------------------------------------------------------------ create (idempotent)
    def create(self, user: User, wallet_id: uuid.UUID, payload: WithdrawalCreate, key: str, ip) -> ApiResult:
        body = {**payload.model_dump(mode="json"), "wallet_id": str(wallet_id)}
        return run_idempotent(self.db, self.s.idem, user.id, "withdrawal.create", key, body,
                              lambda row: self._create(row, user, wallet_id, payload, ip))

    def _create(self, row, user: User, wallet_id: uuid.UUID, payload: WithdrawalCreate, ip) -> ApiResult:
        wallet = self.s.wallets.get(wallet_id)
        if not wallet:
            raise NotFoundError("Wallet not found.", code="WALLET_NOT_FOUND")
        assert_wallet_owner(user, wallet)
        wallet = self.s.wallets.lock_many([wallet_id])[wallet_id]
        assert_wallet_active(wallet)                       # frozen / suspended / closed wallets cannot withdraw

        now = utcnow()
        decision = self.s.policy.evaluate_outgoing(wallet, TransactionType.WITHDRAWAL, payload.amount, now)
        w = self.withdrawals.add(Withdrawal(
            id=uuid.uuid4(), wallet_id=wallet.id, amount=payload.amount, currency=wallet.currency,
            destination=payload.destination, reference_number=new_ref("WDR"), status=TxStatus.PENDING,
            created_by=user.id))
        tx = self.s.new_tx(type_=TransactionType.WITHDRAWAL, wallet_id=wallet.id, counterparty=None,
                           amount=w.amount, currency=w.currency, reference_number=w.reference_number,
                           entity="withdrawal", ref_id=w.id, user_id=user.id,
                           description=f"Withdrawal to {payload.destination}" if payload.destination else "Withdrawal",
                           risk=decision.risk_status)
        self.s.audit.log("WITHDRAWAL_CREATED", user_id=user.id, entity="withdrawal", entity_id=w.id, ip=ip,
                         meta={"amount": w.amount, "wallet_id": wallet.id})

        if self.s.ledger.spendable(wallet) < w.amount:
            return self._fail(row, w, tx, wallet, InsufficientBalanceError(), user, ip, decision=None)
        if decision.error:
            return self._fail(row, w, tx, wallet, decision.error, user, ip, decision=decision)

        if decision.risk_status == RiskStatus.REVIEW_REQUIRED:
            self.s.ledger.reserve(wallet, w.amount)           # funds held until a reviewer decides
            self.s.flag(tx, wallet, decision, user.id, ip)
            status = 202
        else:
            outcome = payload.simulate_outcome if settings.allow_simulation else "SUCCESS"
            self._process(w, tx, wallet, outcome, from_reserved=False, actor_id=user.id)
            status = 201
        body = WithdrawalOut.model_validate(w).model_dump(mode="json")
        self.s.idem.complete(row, status, body, w.id)
        self.db.commit()
        return ApiResult(status, body)

    def _fail(self, row, w, tx, wallet, err: AppError, user, ip, decision: RiskDecision | None):
        w.failure_reason = err.code
        self.s.set_status(w, tx, TxStatus.FAILED, "withdrawal")
        tx.risk_status = decision.risk_status if decision is not None else RiskStatus.NORMAL
        if decision is not None:
            self.s.flag(tx, wallet, decision, user.id, ip)
        self.s.audit.log("WITHDRAWAL_FAILED", user_id=user.id, entity="withdrawal", entity_id=w.id, ip=ip,
                         meta={"reason": err.code})
        self.s.notify.notify(wallet.user_id, NotificationType.WITHDRAWAL_FAILED, "Withdrawal failed",
                             f"Your withdrawal of {fmt(w.amount, w.currency)} failed: {err.message}",
                             {"withdrawal_id": w.id})
        self.s.idem.complete_with_error(row, err)
        self.db.commit()          # the FAILED record + audit trail persist even though the request errors
        raise err

    # ------------------------------------------------------------------ processing
    def _process(self, w: Withdrawal, tx, wallet, outcome: str, *, from_reserved: bool, actor_id) -> None:
        self.s.set_status(w, tx, TxStatus.PROCESSING, "withdrawal")
        if outcome == "SUCCESS":
            self.s.ledger.debit(wallet, w.amount, LedgerType.WITHDRAWAL, transaction=tx,
                                description=f"Withdrawal {w.reference_number}", created_by=actor_id,
                                from_reserved=from_reserved)
            w.failure_reason = None
            self.s.set_status(w, tx, TxStatus.COMPLETED, "withdrawal")
            self.s.notify.notify(wallet.user_id, NotificationType.WITHDRAWAL_COMPLETED, "Withdrawal completed",
                                 f"{fmt(w.amount, w.currency)} was withdrawn from '{wallet.name}'.",
                                 {"withdrawal_id": w.id})
            self.s.audit.log("WITHDRAWAL_COMPLETED", user_id=actor_id, entity="withdrawal", entity_id=w.id,
                             meta={"amount": w.amount})
        else:
            if from_reserved:
                self.s.ledger.release(wallet, w.amount)
            w.failure_reason = "GATEWAY_DECLINED" if outcome == "FAILURE" else "GATEWAY_TIMEOUT"
            self.s.set_status(w, tx, TxStatus.FAILED, "withdrawal")
            self.s.notify.notify(wallet.user_id, NotificationType.WITHDRAWAL_FAILED, "Withdrawal failed",
                                 f"Your withdrawal of {fmt(w.amount, w.currency)} failed ({w.failure_reason}).",
                                 {"withdrawal_id": w.id})
            self.s.audit.log("WITHDRAWAL_FAILED", user_id=actor_id, entity="withdrawal", entity_id=w.id,
                             meta={"reason": w.failure_reason})

    # ------------------------------------------------------------------ pending (review) lifecycle
    def complete_pending(self, wid: uuid.UUID, actor: User, ip, commit: bool = True) -> Withdrawal:
        w = self.withdrawals.get_for_update(wid)
        wallet = self.s.wallets.lock_many([w.wallet_id])[w.wallet_id]
        if w.status != TxStatus.PENDING:
            raise InvalidStateTransitionError(f"Withdrawal is {w.status.value}, not PENDING.")
        tx = self.s.txs.get_by_reference("withdrawal", w.id)
        if wallet.status.value != "ACTIVE":
            self.s.ledger.release(wallet, w.amount)
            w.failure_reason = "WALLET_NOT_ACTIVE"
            self.s.set_status(w, tx, TxStatus.FAILED, "withdrawal")
            self.s.notify.notify(wallet.user_id, NotificationType.WITHDRAWAL_FAILED, "Withdrawal failed",
                                 "The wallet is no longer active.", {"withdrawal_id": w.id})
        else:
            self._process(w, tx, wallet, "SUCCESS", from_reserved=True, actor_id=actor.id)
        if commit:
            self.db.commit()
        return w

    def reject_pending(self, wid: uuid.UUID, actor: User, ip, reason: str = "REJECTED_BY_REVIEWER") -> Withdrawal:
        w = self.withdrawals.get_for_update(wid)
        wallet = self.s.wallets.lock_many([w.wallet_id])[w.wallet_id]
        tx = self.s.txs.get_by_reference("withdrawal", w.id)
        self.s.ledger.release(wallet, w.amount)
        w.failure_reason = reason
        self.s.set_status(w, tx, TxStatus.FAILED, "withdrawal")
        self.s.notify.notify(wallet.user_id, NotificationType.WITHDRAWAL_FAILED, "Withdrawal rejected",
                             f"Your withdrawal of {fmt(w.amount, w.currency)} was rejected after review.",
                             {"withdrawal_id": w.id})
        self.s.audit.log("WITHDRAWAL_FAILED", user_id=actor.id, entity="withdrawal", entity_id=w.id, ip=ip,
                         meta={"reason": reason})
        return w

    def cancel(self, user: User | None, wid: uuid.UUID, ip, system: bool = False) -> Withdrawal:
        w = self.withdrawals.get(wid)
        if not w:
            raise NotFoundError("Withdrawal not found.", code="WITHDRAWAL_NOT_FOUND")
        wallet_ref = self.s.wallets.get(w.wallet_id)
        if not system and not (wallet_ref.user_id == user.id or is_admin(user)):
            raise ForbiddenError("Only the wallet owner or an administrator can cancel a withdrawal.")
        tx = self.s.txs.get_by_reference("withdrawal", w.id)
        rec = self.s.suspicious.by_transaction(tx.id)
        if rec:                                              # lock order: suspicious -> withdrawal -> wallet
            self.s.suspicious.get_for_update(rec.id)
        w = self.withdrawals.get_for_update(wid)
        wallet = self.s.wallets.lock_many([w.wallet_id])[w.wallet_id]
        if w.status != TxStatus.PENDING:
            raise InvalidStateTransitionError(f"Only PENDING withdrawals can be cancelled (current: {w.status.value}).")
        self.s.ledger.release(wallet, w.amount)
        self.s.set_status(w, tx, TxStatus.CANCELLED, "withdrawal")
        if rec:
            rec.review_status = ReviewStatus.CANCELLED
        self.s.audit.log("WITHDRAWAL_CANCELLED", user_id=None if system else user.id, entity="withdrawal",
                         entity_id=w.id, ip=ip, meta={"system": system})
        self.db.commit()
        return w

    def retry(self, wid: uuid.UUID) -> bool:
        """Failed-transaction retry job: bounded retries of GATEWAY_TIMEOUT failures only."""
        w = self.withdrawals.get(wid)
        wallet = self.s.wallets.lock_many([w.wallet_id])[w.wallet_id]
        w = self.withdrawals.get_for_update(wid)
        if w.status != TxStatus.FAILED or w.failure_reason != "GATEWAY_TIMEOUT":
            return False
        tx = self.s.txs.get_by_reference("withdrawal", w.id)
        w.retry_count += 1
        if wallet.status.value != "ACTIVE":
            w.failure_reason = "WALLET_NOT_ACTIVE"
            self.db.commit()
            return False
        if self.s.ledger.spendable(wallet) < w.amount:
            w.failure_reason = "INSUFFICIENT_BALANCE"
            self.db.commit()
            return False
        self._process(w, tx, wallet, "SUCCESS", from_reserved=False, actor_id=None)
        self.db.commit()
        return True

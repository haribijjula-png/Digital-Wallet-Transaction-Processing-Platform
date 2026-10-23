import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.security import utcnow
from app.models import (Deposit, LedgerType, NotificationType, TransactionType, TxStatus, User, Wallet)
from app.repositories.base import PageParams, new_ref
from app.repositories.transactions import DepositRepository
from app.schemas.wallets import DepositCreate, DepositOut
from app.services.access import assert_wallet_access, assert_wallet_owner, visible_wallet_ids
from app.services.common import ApiResult
from app.services.ledger import assert_wallet_active
from app.services.movement import MovementSupport, fmt, run_idempotent


class DepositService:
    def __init__(self, db: Session):
        self.db = db
        self.s = MovementSupport(db)
        self.deposits = DepositRepository(db)

    # ------------------------------------------------------------------ queries
    def get_authorized(self, user: User, deposit_id: uuid.UUID) -> Deposit:
        dep = self.deposits.get(deposit_id)
        if not dep:
            raise NotFoundError("Deposit not found.", code="DEPOSIT_NOT_FOUND")
        assert_wallet_access(user, self.s.wallets.get(dep.wallet_id))
        return dep

    def list(self, user: User, params: PageParams, status=None, wallet_id=None):
        return self.deposits.list(params, visible_wallet_ids(user, self.s.wallets), status, wallet_id)

    # ------------------------------------------------------------------ create (idempotent)
    def create(self, user: User, wallet_id: uuid.UUID, payload: DepositCreate, key: str, ip: str | None) -> ApiResult:
        body = {**payload.model_dump(mode="json"), "wallet_id": str(wallet_id)}
        return run_idempotent(self.db, self.s.idem, user.id, "deposit.create", key, body,
                              lambda row: self._create(row, user, wallet_id, payload, ip))

    def _create(self, row, user: User, wallet_id: uuid.UUID, payload: DepositCreate, ip) -> ApiResult:
        wallet = self.s.wallets.get(wallet_id)
        if not wallet:
            raise NotFoundError("Wallet not found.", code="WALLET_NOT_FOUND")
        assert_wallet_owner(user, wallet)
        wallet = self.s.wallets.lock_many([wallet_id])[wallet_id]       # serialise balance changes
        assert_wallet_active(wallet)
        if payload.currency and payload.currency.upper() != wallet.currency:
            raise BadRequestError("Deposit currency must match the wallet currency.", code="CURRENCY_MISMATCH")
        self.s.policy.check_max_amount(payload.amount)

        dep_id = uuid.uuid4()
        dep = self.deposits.add(Deposit(id=dep_id, wallet_id=wallet.id, amount=payload.amount,
                                        currency=wallet.currency, reference_number=new_ref("DEP"),
                                        status=TxStatus.PENDING, created_by=user.id))
        tx = self.s.new_tx(type_=TransactionType.DEPOSIT, wallet_id=wallet.id, counterparty=None,
                           amount=dep.amount, currency=dep.currency, reference_number=dep.reference_number,
                           entity="deposit", ref_id=dep.id, user_id=user.id, description="Wallet deposit")
        self.s.audit.log("DEPOSIT_CREATED", user_id=user.id, entity="deposit", entity_id=dep.id, ip=ip,
                         meta={"amount": dep.amount, "wallet_id": wallet.id})
        if settings.auto_process_payments:
            outcome = payload.simulate_outcome if settings.allow_simulation else "SUCCESS"
            self.process(dep, tx, wallet, outcome, user.id)
        body = DepositOut.model_validate(dep).model_dump(mode="json")
        self.s.idem.complete(row, 201, body, dep.id)
        self.db.commit()
        return ApiResult(201, body)

    # ------------------------------------------------------------------ processing (shared with retry job)
    def process(self, dep: Deposit, tx, wallet: Wallet, outcome: str, actor_id) -> None:
        """Simulated payment gateway. Wallet must be locked by the caller."""
        self.s.set_status(dep, tx, TxStatus.PROCESSING, "deposit")
        if outcome == "SUCCESS":
            self.s.ledger.credit(wallet, dep.amount, LedgerType.DEPOSIT, transaction=tx,
                                 description=f"Deposit {dep.reference_number}", created_by=actor_id)
            dep.failure_reason = None
            self.s.set_status(dep, tx, TxStatus.COMPLETED, "deposit")
            self.s.notify.notify(wallet.user_id, NotificationType.DEPOSIT_COMPLETED, "Deposit completed",
                                 f"{fmt(dep.amount, dep.currency)} was added to '{wallet.name}'.",
                                 {"deposit_id": dep.id})
            self.s.audit.log("DEPOSIT_COMPLETED", user_id=actor_id, entity="deposit", entity_id=dep.id,
                             meta={"amount": dep.amount})
        else:
            dep.failure_reason = "GATEWAY_DECLINED" if outcome == "FAILURE" else "GATEWAY_TIMEOUT"
            self.s.set_status(dep, tx, TxStatus.FAILED, "deposit")
            self.s.notify.notify(wallet.user_id, NotificationType.DEPOSIT_FAILED, "Deposit failed",
                                 f"Your deposit of {fmt(dep.amount, dep.currency)} failed ({dep.failure_reason}).",
                                 {"deposit_id": dep.id})
            self.s.audit.log("DEPOSIT_FAILED", user_id=actor_id, entity="deposit", entity_id=dep.id,
                             meta={"reason": dep.failure_reason})

    def retry(self, deposit_id: uuid.UUID) -> bool:
        """Used by the failed-transaction retry job. Returns True if the retry succeeded."""
        dep = self.deposits.get(deposit_id)
        wallet = self.s.wallets.lock_many([dep.wallet_id])[dep.wallet_id]
        dep = self.deposits.get_for_update(deposit_id)
        if dep.status != TxStatus.FAILED or dep.failure_reason != "GATEWAY_TIMEOUT":
            return False
        tx = self.s.txs.get_by_reference("deposit", dep.id)
        dep.retry_count += 1
        if wallet.status.value != "ACTIVE":
            dep.failure_reason = "WALLET_NOT_ACTIVE"      # non-retryable
            self.db.commit()
            return False
        self.process(dep, tx, wallet, "SUCCESS", None)
        self.db.commit()
        return True

    def cancel_stale(self, deposit_id: uuid.UUID) -> bool:
        dep = self.deposits.get_for_update(deposit_id)
        if dep.status != TxStatus.PENDING:
            return False
        tx = self.s.txs.get_by_reference("deposit", dep.id)
        dep.failure_reason = "EXPIRED"
        self.s.set_status(dep, tx, TxStatus.CANCELLED, "deposit")
        self.s.audit.log("DEPOSIT_CANCELLED", entity="deposit", entity_id=dep.id, meta={"reason": "stale pending"})
        self.db.commit()
        return True

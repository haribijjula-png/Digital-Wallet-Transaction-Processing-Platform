import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import (InsufficientBalanceError, InvalidAmountError, WalletClosedError,
                                 WalletFrozenError, WalletSuspendedError)
from app.models import Direction, LedgerEntry, LedgerType, Transaction, Wallet, WalletStatus
from app.repositories.transactions import LedgerRepository
from app.services.common import json_safe

ZERO = Decimal("0.00")


def assert_wallet_active(wallet: Wallet, role: str = "Wallet") -> None:
    """Maps each non-active status to its domain error."""
    if wallet.status == WalletStatus.ACTIVE:
        return
    err = {WalletStatus.FROZEN: WalletFrozenError, WalletStatus.CLOSED: WalletClosedError,
           WalletStatus.SUSPENDED: WalletSuspendedError}[wallet.status]
    raise err(f"{role} is {wallet.status.value.lower()}.")


class LedgerService:
    """The ONLY place where wallet balances change. Every change appends an immutable
    ledger row with balance_before / balance_after.

    Precondition for all mutating methods: the wallet row was locked with
    WalletRepository.lock_many() in the current DB transaction."""

    def __init__(self, db: Session):
        self.db = db
        self.ledger = LedgerRepository(db)

    @staticmethod
    def spendable(wallet: Wallet) -> Decimal:
        return wallet.balance.available_balance - wallet.balance.pending_balance

    def _entry(self, wallet, direction, entry_type, amount, before, after, *, transaction, description,
               created_by, meta, reference_entity=None, reference_id=None) -> LedgerEntry:
        return self.ledger.add(LedgerEntry(
            wallet_id=wallet.id, transaction_id=transaction.id if transaction else None,
            transaction_type=entry_type, direction=direction, amount=amount,
            balance_before=before, balance_after=after,
            reference_entity=reference_entity or (transaction.reference_entity if transaction else None),
            reference_id=reference_id or (transaction.reference_id if transaction else None),
            description=description, created_by=created_by, meta=json_safe(meta or {})))

    def credit(self, wallet: Wallet, amount: Decimal, entry_type: LedgerType, *, transaction: Transaction | None,
               description: str, created_by: uuid.UUID | None, meta: dict | None = None) -> LedgerEntry:
        if amount <= 0:
            raise InvalidAmountError()
        bal = wallet.balance
        before = bal.available_balance
        bal.available_balance = before + amount
        self.db.flush()
        return self._entry(wallet, Direction.CREDIT, entry_type, amount, before, bal.available_balance,
                           transaction=transaction, description=description, created_by=created_by, meta=meta)

    def debit(self, wallet: Wallet, amount: Decimal, entry_type: LedgerType, *, transaction: Transaction | None,
              description: str, created_by: uuid.UUID | None, meta: dict | None = None,
              from_reserved: bool = False) -> LedgerEntry:
        """Debit available funds. `from_reserved=True` settles a previously reserved
        (PENDING) amount; otherwise only spendable (available - reserved) funds may be used."""
        if amount <= 0:
            raise InvalidAmountError()
        bal = wallet.balance
        if from_reserved:
            if bal.pending_balance < amount:
                raise InsufficientBalanceError("Reserved funds are no longer available.")
            bal.pending_balance = bal.pending_balance - amount
        elif self.spendable(wallet) < amount:
            raise InsufficientBalanceError()
        before = bal.available_balance
        bal.available_balance = before - amount
        self.db.flush()    # CHECK constraints are the last line of defence against a negative balance
        return self._entry(wallet, Direction.DEBIT, entry_type, amount, before, bal.available_balance,
                           transaction=transaction, description=description, created_by=created_by, meta=meta)

    def reserve(self, wallet: Wallet, amount: Decimal) -> None:
        if self.spendable(wallet) < amount:
            raise InsufficientBalanceError()
        wallet.balance.pending_balance = wallet.balance.pending_balance + amount
        self.db.flush()

    def release(self, wallet: Wallet, amount: Decimal) -> None:
        wallet.balance.pending_balance = max(ZERO, wallet.balance.pending_balance - amount)
        self.db.flush()

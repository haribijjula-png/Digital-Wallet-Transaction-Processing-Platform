import uuid

from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models import User
from app.repositories.base import PageParams
from app.repositories.transactions import LedgerRepository, TransactionRepository
from app.repositories.wallets import WalletRepository
from app.services.access import visible_wallet_ids
from app.services.common import is_staff


class TransactionQueryService:
    def __init__(self, db: Session):
        self.txs = TransactionRepository(db)
        self.ledger = LedgerRepository(db)
        self.wallets = WalletRepository(db)

    def search(self, user: User, params: PageParams, **filters):
        scope = visible_wallet_ids(user, self.wallets)
        wallet_id = filters.pop("wallet_id", None)
        if wallet_id and scope is not None and wallet_id not in scope:
            raise ForbiddenError("You do not have access to this wallet.", code="WALLET_ACCESS_DENIED")
        return self.txs.search(params, wallet_ids=scope, wallet_id=wallet_id, **filters)

    def get(self, user: User, tx_id: uuid.UUID):
        tx = self.txs.get(tx_id)
        if not tx:
            raise NotFoundError("Transaction not found.", code="TRANSACTION_NOT_FOUND")
        if not is_staff(user):
            mine = set(self.wallets.ids_for_user(user.id))
            if tx.wallet_id not in mine and tx.counterparty_wallet_id not in mine:
                raise ForbiddenError("You do not have access to this transaction.", code="TRANSACTION_ACCESS_DENIED")
        return tx

    def ledger_entries(self, user: User, tx_id: uuid.UUID):
        self.get(user, tx_id)
        return self.ledger.for_transaction(tx_id)

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Wallet, WalletBalance, WalletStatus
from app.repositories.base import PageParams, paginate


class WalletRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, wallet_id: uuid.UUID) -> Wallet | None:
        return self.db.scalar(select(Wallet).where(Wallet.id == wallet_id).options(selectinload(Wallet.balance)))

    def add(self, wallet: Wallet) -> Wallet:
        wallet.balance = WalletBalance()
        self.db.add(wallet)
        self.db.flush()
        return wallet

    def count_for_user(self, user_id: uuid.UUID) -> int:
        return self.db.scalar(select(func.count()).select_from(Wallet).where(Wallet.user_id == user_id)) or 0

    def ids_for_user(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        return list(self.db.scalars(select(Wallet.id).where(Wallet.user_id == user_id)))

    def lock_many(self, wallet_ids: list[uuid.UUID]) -> dict[uuid.UUID, Wallet]:
        """Pessimistic row locks (SELECT ... FOR UPDATE) taken in a deterministic order
        (ascending id). Consistent ordering prevents deadlocks between opposite transfers
        (A->B vs B->A). populate_existing forces a refresh so we never act on stale state."""
        ids = sorted(set(wallet_ids))
        wallets = self.db.scalars(
            select(Wallet).where(Wallet.id.in_(ids)).order_by(Wallet.id).with_for_update()
            .execution_options(populate_existing=True)).all()
        self.db.scalars(
            select(WalletBalance).where(WalletBalance.wallet_id.in_(ids)).order_by(WalletBalance.wallet_id)
            .with_for_update().execution_options(populate_existing=True)).all()
        return {w.id: w for w in wallets}

    def list(self, params: PageParams, user_id: uuid.UUID | None = None, status: WalletStatus | None = None,
             currency: str | None = None):
        stmt = select(Wallet).options(selectinload(Wallet.balance))
        if user_id:
            stmt = stmt.where(Wallet.user_id == user_id)
        if status:
            stmt = stmt.where(Wallet.status == status)
        if currency:
            stmt = stmt.where(Wallet.currency == currency.upper())
        return paginate(self.db, stmt, params, {"created_at": Wallet.created_at, "currency": Wallet.currency,
                                                "status": Wallet.status, "name": Wallet.name},
                        "created_at", Wallet.id)

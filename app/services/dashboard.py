from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (RefundStatus, ReviewStatus, SuspiciousTransaction, Transaction, TransactionType, TxStatus,
                        User, Wallet, WalletBalance, WalletStatus)
from app.repositories.base import utc_day_start
from app.repositories.misc import RefundRepository, SuspiciousRepository
from app.repositories.users import UserRepository
from app.repositories.wallets import WalletRepository
from app.schemas.wallets import TransactionOut


def _by_currency(rows) -> dict[str, str]:
    return {cur: f"{total:.2f}" for cur, total in rows}


class DashboardService:
    def __init__(self, db: Session):
        self.db = db

    def admin(self) -> dict:
        db, day = self.db, utc_day_start()
        today = Transaction.created_at >= day
        return {
            "total_users": UserRepository(db).count(),
            "active_wallets": db.scalar(select(func.count()).select_from(Wallet)
                                        .where(Wallet.status == WalletStatus.ACTIVE)) or 0,
            "total_wallet_balance": _by_currency(db.execute(
                select(Wallet.currency, func.coalesce(func.sum(WalletBalance.available_balance), 0))
                .join(WalletBalance, WalletBalance.wallet_id == Wallet.id).group_by(Wallet.currency))),
            "todays_transactions": db.scalar(select(func.count()).select_from(Transaction).where(today)) or 0,
            "todays_transaction_volume": _by_currency(db.execute(
                select(Transaction.currency, func.coalesce(func.sum(Transaction.amount), 0))
                .where(today, Transaction.status == TxStatus.COMPLETED).group_by(Transaction.currency))),
            "failed_transactions": db.scalar(select(func.count()).select_from(Transaction)
                                             .where(today, Transaction.status == TxStatus.FAILED)) or 0,
            "suspicious_transactions": SuspiciousRepository(db).count(),
        }

    def support(self) -> dict:
        db, day = self.db, utc_day_start()
        sus = SuspiciousRepository(db)
        return {
            "pending_reviews": sus.count(ReviewStatus.PENDING),
            "suspicious_transactions": sus.count(),
            "failed_transactions": db.scalar(select(func.count()).select_from(Transaction).where(
                Transaction.created_at >= day, Transaction.status == TxStatus.FAILED)) or 0,
            "refund_requests": RefundRepository(db).count_open(),
            "frozen_wallets": db.scalar(select(func.count()).select_from(Wallet)
                                        .where(Wallet.status == WalletStatus.FROZEN)) or 0,
        }

    def customer(self, user: User) -> dict:
        db = self.db
        wallets = db.scalars(select(Wallet).where(Wallet.user_id == user.id).order_by(Wallet.created_at)).all()
        ids = [w.id for w in wallets]
        totals: dict[str, float] = {}
        for w in wallets:
            totals[w.currency] = totals.get(w.currency, 0) + w.balance.available_balance
        recent = db.scalars(select(Transaction).where(
            Transaction.wallet_id.in_(ids) | Transaction.counterparty_wallet_id.in_(ids))
            .order_by(Transaction.created_at.desc(), Transaction.id).limit(10)).all() if ids else []

        def completed_total(tx_type):
            if not ids:
                return {}
            return _by_currency(db.execute(
                select(Transaction.currency, func.coalesce(func.sum(Transaction.amount), 0))
                .where(Transaction.wallet_id.in_(ids), Transaction.type == tx_type,
                       Transaction.status == TxStatus.COMPLETED).group_by(Transaction.currency)))

        pending = db.scalar(select(func.count()).select_from(Transaction).where(
            Transaction.wallet_id.in_(ids), Transaction.status.in_((TxStatus.PENDING, TxStatus.PROCESSING)))) if ids else 0
        return {
            "wallet_balances": [{"wallet_id": str(w.id), "name": w.name, "currency": w.currency,
                                 "status": w.status.value, "available_balance": f"{w.balance.available_balance:.2f}",
                                 "pending_balance": f"{w.balance.pending_balance:.2f}"} for w in wallets],
            "total_balance": {c: f"{t:.2f}" for c, t in totals.items()},
            "recent_transactions": [TransactionOut.model_validate(t) for t in recent],
            "total_deposits": completed_total(TransactionType.DEPOSIT),
            "total_transfers": completed_total(TransactionType.TRANSFER),
            "total_withdrawals": completed_total(TransactionType.WITHDRAWAL),
            "pending_transactions": pending or 0,
        }

"""Transaction limits + rule-based suspicious-transaction monitoring."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, SuspiciousTransactionError, TransactionLimitExceededError
from app.models import RiskStatus, TransactionType, Wallet
from app.repositories.base import utc_day_start
from app.repositories.misc import LimitRepository
from app.repositories.transactions import TransactionRepository


@dataclass
class RiskDecision:
    risk_status: RiskStatus = RiskStatus.NORMAL
    reasons: list[str] = field(default_factory=list)
    error: AppError | None = None     # set when the transaction must be rejected (BLOCKED)


class PolicyService:
    def __init__(self, db: Session):
        self.limits = LimitRepository(db)
        self.txs = TransactionRepository(db)

    def check_max_amount(self, amount: Decimal) -> None:
        limits = self.limits.get()
        if amount > limits.max_transaction_amount:
            raise TransactionLimitExceededError(
                f"Amount exceeds the maximum transaction amount of {limits.max_transaction_amount}.")

    def evaluate_outgoing(self, wallet: Wallet, kind: TransactionType, amount: Decimal,
                          now: datetime) -> RiskDecision:
        """Called while the sender wallet is row-locked, so usage counters cannot race."""
        L = self.limits.get()
        day_start = utc_day_start(now)
        d = RiskDecision()

        # ---- hard limits -> BLOCKED (rejected with a domain error)
        if amount > L.max_transaction_amount:
            d.error = TransactionLimitExceededError(
                f"Amount exceeds the maximum transaction amount of {L.max_transaction_amount}.")
            d.reasons.append("MAX_TRANSACTION_AMOUNT_EXCEEDED")
        else:
            used = self.txs.outgoing_total(wallet.id, kind, day_start)
            daily_limit = L.daily_transfer_limit if kind == TransactionType.TRANSFER else L.daily_withdrawal_limit
            if used + amount > daily_limit:
                d.error = TransactionLimitExceededError(
                    f"Daily {kind.value.lower()} limit of {daily_limit} would be exceeded "
                    f"(already used {used}).")
                d.reasons.append("DAILY_LIMIT_EXCEEDED")
            elif self.txs.outgoing_count(wallet.id, day_start) + 1 > L.max_transactions_per_day:
                d.error = TransactionLimitExceededError(
                    f"Maximum of {L.max_transactions_per_day} transactions per day reached.")
                d.reasons.append("DAILY_TRANSACTION_COUNT_EXCEEDED")
        if d.error:
            d.risk_status = RiskStatus.BLOCKED
            return d

        # ---- velocity: many transactions inside a short window
        recent = self.txs.outgoing_count(wallet.id, now - timedelta(minutes=L.velocity_window_minutes)) + 1
        if recent >= L.velocity_block_count:
            d.risk_status = RiskStatus.BLOCKED
            d.reasons.append("VELOCITY_BLOCK")
            d.error = SuspiciousTransactionError(
                "Too many transactions in a short period; this transaction was blocked.")
            return d
        if recent >= L.velocity_review_count:
            d.reasons.append("MULTIPLE_TRANSACTIONS_SHORT_PERIOD")

        # ---- other review rules
        if amount >= L.suspicious_amount_threshold:
            d.reasons.append("AMOUNT_ABOVE_THRESHOLD")
        if self.txs.outgoing_count(wallet.id, day_start) + 1 >= L.frequency_review_threshold:
            d.reasons.append("UNUSUAL_TRANSACTION_FREQUENCY")
        if wallet.last_frozen_at and now - wallet.last_frozen_at <= timedelta(hours=L.recently_frozen_hours):
            d.reasons.append("WALLET_RECENTLY_FROZEN")

        if d.reasons:
            d.risk_status = RiskStatus.REVIEW_REQUIRED
        return d

"""Scheduled processing. Each job is a plain function (Session -> dict) so it can be driven by
Celery Beat, cron, the admin "run job" endpoint, or tests.

Daily limits are computed from a UTC-day window over `transactions`, so they reset implicitly at
00:00 UTC; `daily_limit_reset` records the roll-over for audit/monitoring."""
import logging
from datetime import timedelta

from sqlalchemy import delete, distinct, exists, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import utcnow
from app.models import (AuditLog, Direction, IdempotencyKey, LedgerEntry, Notification, NotificationType, RefreshToken,
                        ReviewStatus, RiskStatus, SuspiciousTransaction, Transaction, TransactionType, TxStatus,
                        Wallet)
from app.repositories.base import utc_day_start
from app.repositories.misc import LimitRepository
from app.repositories.transactions import ACTIVE_STATUSES, DepositRepository, WithdrawalRepository
from app.services.audit import AuditService
from app.services.deposits import DepositService
from app.services.transfers import TransferService
from app.services.withdrawals import WithdrawalService

logger = logging.getLogger("wallet.jobs")


def daily_limit_reset(db: Session) -> dict:
    day = utc_day_start()
    yesterday = day - timedelta(days=1)
    wallets = db.scalar(select(func.count(distinct(Transaction.wallet_id))).where(
        Transaction.type.in_((TransactionType.TRANSFER, TransactionType.WITHDRAWAL)),
        Transaction.created_at >= yesterday, Transaction.created_at < day)) or 0
    AuditService(db).log("DAILY_LIMITS_RESET", entity="system", meta={"window_start": day, "wallets_rolled_over": wallets})
    db.commit()
    return {"window_start": day.isoformat(), "wallets_rolled_over": wallets}


def pending_cleanup(db: Session) -> dict:
    now = utcnow()
    result = {"deposits_cancelled": 0, "held_transfers_cancelled": 0, "held_withdrawals_cancelled": 0,
              "idempotency_keys_deleted": 0, "refresh_tokens_deleted": 0}
    # stale PENDING deposits
    cutoff = now - timedelta(minutes=settings.pending_deposit_timeout_minutes)
    for dep in DepositRepository(db).stale_pending(cutoff):
        if DepositService(db).cancel_stale(dep.id):
            result["deposits_cancelled"] += 1
    # transfers / withdrawals held for review for too long -> cancel + release reserved funds
    review_cutoff = now - timedelta(hours=settings.pending_review_timeout_hours)
    held = db.execute(select(Transaction.reference_entity, Transaction.reference_id).where(
        Transaction.status == TxStatus.PENDING, Transaction.type.in_((TransactionType.TRANSFER,
                                                                     TransactionType.WITHDRAWAL)),
        Transaction.created_at < review_cutoff)).all()
    for entity, ref_id in held:
        if entity == "transfer":
            TransferService(db).cancel(None, ref_id, None, system=True)
            result["held_transfers_cancelled"] += 1
        else:
            WithdrawalService(db).cancel(None, ref_id, None, system=True)
            result["held_withdrawals_cancelled"] += 1
    # housekeeping
    res = db.execute(delete(IdempotencyKey).where(
        IdempotencyKey.created_at < now - timedelta(hours=settings.idempotency_ttl_hours)))
    result["idempotency_keys_deleted"] = res.rowcount
    res = db.execute(delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=7)))
    result["refresh_tokens_deleted"] = res.rowcount
    db.commit()
    return result


def suspicious_scan(db: Session) -> dict:
    """Post-hoc scan for patterns that slipped past request-time checks (e.g. activity spread over
    many requests). Flags the most recent unflagged transaction of an offending wallet."""
    L = LimitRepository(db).get()
    now = utcnow()
    flagged = 0
    for window, threshold, reason in (
            (timedelta(minutes=L.velocity_window_minutes), L.velocity_review_count, "SCAN_VELOCITY"),
            (now - utc_day_start(now), L.frequency_review_threshold, "SCAN_DAILY_FREQUENCY")):
        since = now - window
        offenders = db.scalars(select(Transaction.wallet_id).where(
            Transaction.type.in_((TransactionType.TRANSFER, TransactionType.WITHDRAWAL)),
            Transaction.status.in_(ACTIVE_STATUSES), Transaction.created_at >= since)
            .group_by(Transaction.wallet_id).having(func.count() >= threshold)).all()
        for wallet_id in offenders:
            already = db.scalar(select(exists().where(SuspiciousTransaction.wallet_id == wallet_id,
                                                      SuspiciousTransaction.created_at >= since)))
            if already:
                continue
            tx = db.scalar(select(Transaction).where(
                Transaction.wallet_id == wallet_id, Transaction.created_at >= since,
                Transaction.type.in_((TransactionType.TRANSFER, TransactionType.WITHDRAWAL)),
                ~exists().where(SuspiciousTransaction.transaction_id == Transaction.id))
                .order_by(Transaction.created_at.desc()).limit(1))
            if not tx:
                continue
            db.add(SuspiciousTransaction(transaction_id=tx.id, wallet_id=wallet_id, risk_status=RiskStatus.REVIEW_REQUIRED,
                                         reasons=[reason], review_status=ReviewStatus.PENDING))
            if tx.risk_status == RiskStatus.NORMAL:
                tx.risk_status = RiskStatus.REVIEW_REQUIRED
            AuditService(db).log("TRANSACTION_FLAGGED", entity="transaction", entity_id=tx.id,
                                 meta={"reasons": [reason], "source": "scheduled_scan"})
            flagged += 1
    db.commit()
    return {"flagged": flagged}


def daily_wallet_summary(db: Session) -> dict:
    day = utc_day_start()
    start = day - timedelta(days=1)
    rows = db.execute(
        select(LedgerEntry.wallet_id,
               func.coalesce(func.sum(LedgerEntry.amount).filter(LedgerEntry.direction == Direction.CREDIT), 0),
               func.coalesce(func.sum(LedgerEntry.amount).filter(LedgerEntry.direction == Direction.DEBIT), 0),
               func.count())
        .where(LedgerEntry.created_at >= start, LedgerEntry.created_at < day).group_by(LedgerEntry.wallet_id)).all()
    sent = 0
    for wallet_id, credits, debits, count in rows:
        wallet = db.get(Wallet, wallet_id)
        marker = {"wallet_id": str(wallet_id), "summary_date": start.date().isoformat()}
        if db.scalar(select(exists().where(Notification.type == NotificationType.DAILY_SUMMARY,
                                           Notification.meta.contains(marker)))):
            continue      # idempotent: one summary per wallet per day
        db.add(Notification(
            user_id=wallet.user_id, type=NotificationType.DAILY_SUMMARY, title="Daily wallet summary",
            message=(f"{start.date()}: {count} entries, credits {wallet.currency} {credits:.2f}, "
                     f"debits {wallet.currency} {debits:.2f}, closing balance {wallet.currency} "
                     f"{wallet.balance.available_balance:.2f}."),
            meta={**marker, "credits": str(credits), "debits": str(debits), "entries": count}))
        sent += 1
    db.commit()
    return {"summaries_created": sent}


def failed_transaction_retry(db: Session) -> dict:
    out = {"deposits_retried": 0, "deposits_succeeded": 0, "withdrawals_retried": 0, "withdrawals_succeeded": 0}
    for dep in DepositRepository(db).retryable(settings.max_retry_attempts):
        out["deposits_retried"] += 1
        out["deposits_succeeded"] += int(DepositService(db).retry(dep.id))
    for w in WithdrawalRepository(db).retryable(settings.max_retry_attempts):
        out["withdrawals_retried"] += 1
        out["withdrawals_succeeded"] += int(WithdrawalService(db).retry(w.id))
    return out


def daily_transaction_report(db: Session) -> dict:
    day = utc_day_start()
    start = day - timedelta(days=1)
    rows = db.execute(select(Transaction.type, Transaction.status, Transaction.currency, func.count(),
                             func.coalesce(func.sum(Transaction.amount), 0))
                      .where(Transaction.created_at >= start, Transaction.created_at < day)
                      .group_by(Transaction.type, Transaction.status, Transaction.currency)).all()
    report = {"date": start.date().isoformat(), "total": 0, "failed": 0, "volume": {}, "by_type": {}}
    for ttype, status, currency, count, total in rows:
        report["total"] += count
        report["by_type"][ttype.value] = report["by_type"].get(ttype.value, 0) + count
        if status == TxStatus.FAILED:
            report["failed"] += count
        if status == TxStatus.COMPLETED:
            report["volume"][currency] = f"{float(report['volume'].get(currency, 0)) + float(total):.2f}"
    AuditService(db).log("DAILY_TRANSACTION_REPORT", entity="system", meta=report)
    db.commit()
    return report


JOBS = {
    "daily_limit_reset": daily_limit_reset,
    "pending_cleanup": pending_cleanup,
    "suspicious_scan": suspicious_scan,
    "daily_wallet_summary": daily_wallet_summary,
    "failed_transaction_retry": failed_transaction_retry,
    "daily_transaction_report": daily_transaction_report,
}


def run_job(name: str) -> dict:
    with SessionLocal() as db:
        result = JOBS[name](db)
    logger.info("job %s -> %s", name, result)
    return result

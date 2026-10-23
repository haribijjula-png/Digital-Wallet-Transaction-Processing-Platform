"""Optional: Celery + Celery Beat scheduling (requires Redis).

    celery -A app.celery_app worker -l info
    celery -A app.celery_app beat   -l info
"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.jobs.tasks import JOBS, run_job
from app.services.notifications import deliver_pending_notifications

celery = Celery("wallet", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.timezone = "UTC"


def _register(name: str):
    @celery.task(name=f"wallet.{name}")
    def _task():
        return run_job(name)
    return _task


for _name in JOBS:
    _register(_name)


@celery.task(name="wallet.deliver_notifications")
def deliver_notifications():
    return deliver_pending_notifications()


celery.conf.beat_schedule = {
    "daily-limit-reset":       {"task": "wallet.daily_limit_reset",       "schedule": crontab(minute=0, hour=0)},
    "daily-wallet-summary":    {"task": "wallet.daily_wallet_summary",    "schedule": crontab(minute=10, hour=0)},
    "daily-transaction-report": {"task": "wallet.daily_transaction_report", "schedule": crontab(minute=20, hour=0)},
    "pending-cleanup":         {"task": "wallet.pending_cleanup",         "schedule": crontab(minute="*/15")},
    "suspicious-scan":         {"task": "wallet.suspicious_scan",         "schedule": crontab(minute="*/5")},
    "failed-transaction-retry": {"task": "wallet.failed_transaction_retry", "schedule": crontab(minute="*/10")},
    "deliver-notifications":   {"task": "wallet.deliver_notifications",   "schedule": 30.0},
}

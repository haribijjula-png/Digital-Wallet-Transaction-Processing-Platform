import logging

from fastapi import FastAPI

from app.api.routes import admin, auth, dashboard, money, notifications, wallets
from app.core.config import settings
from app.core.exceptions import register_exception_handlers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

DESCRIPTION = """
Secure REST API for a digital wallet platform: wallets, deposits, withdrawals, transfers,
immutable ledger, refunds, suspicious-transaction monitoring, notifications, audit logs.

**Money** is serialised as decimal strings (e.g. `"800.00"`). **Idempotency-Key** header is
required for creating deposits, withdrawals, transfers and refunds.

Errors always use `{"success": false, "error": {"code": "...", "message": "..."}}`.
"""

_WEAK_SECRETS = {"dev-only-secret-change-me", "change-me-to-a-long-random-string", "change-me", ""}
if settings.secret_key in _WEAK_SECRETS or len(settings.secret_key) < 16:
    logging.getLogger("wallet.security").critical(
        "SECRET_KEY is a default/weak value - JWTs can be forged. Set a long random SECRET_KEY before deploying.")

app = FastAPI(title=settings.app_name, version="1.0.0", description=DESCRIPTION)
register_exception_handlers(app)

ROUTERS = (auth.router, wallets.router, money.deposits, money.withdrawals, money.transfers, money.refunds,
           money.transactions, notifications.router, dashboard.router, admin.router)
for router in ROUTERS:
    app.include_router(router)


@app.get("/health", tags=["Health"], summary="Liveness probe")
def health():
    return {"status": "ok"}

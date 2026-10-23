"""Captures REAL request/response pairs from a full API flow into docs/api_samples.json
(used by scripts.generate_api_docs). Uses the TEST database (wallet_test) and resets it.

    python -m scripts.capture_api_samples
"""
import os, json, uuid
from pathlib import Path
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg2://wallet:wallet@localhost:5432/wallet_test")
os.environ["BCRYPT_ROUNDS"] = "4"
from sqlalchemy import text
from alembic import command
from alembic.config import Config
from app.core.database import engine
with engine.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
ROOT = Path(__file__).resolve().parent.parent
command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
from fastapi.testclient import TestClient
from app.main import app
from tests.helpers import make_staff

S = {}
def rec(key, r, req=None, note=None):
    try: body = r.json()
    except Exception: body = None
    S[key] = {"status": r.status_code, "request": req, "response": body, "headers": {k: v for k, v in r.headers.items() if k.lower() in ("idempotent-replayed",)}}
    return r

with TestClient(app) as c:
    pw = "StrongPass123"
    rec("register", c.post("/api/auth/register", json={"email": "alice@example.com", "full_name": "Alice Sharma", "password": pw}), {"email": "alice@example.com", "full_name": "Alice Sharma", "password": pw})
    c.post("/api/auth/register", json={"email": "bob@example.com", "full_name": "Bob Verma", "password": pw})
    tok = c.post("/api/auth/login", json={"email": "alice@example.com", "password": pw}).json()
    rec("login", c.post("/api/auth/login", json={"email": "alice@example.com", "password": pw}), {"email": "alice@example.com", "password": pw})
    btok = c.post("/api/auth/login", json={"email": "bob@example.com", "password": pw}).json()
    A = {"Authorization": f"Bearer {tok['access_token']}"}; B = {"Authorization": f"Bearer {btok['access_token']}"}
    rec("refresh", c.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]}), {"refresh_token": "<refresh_token>"})
    rec("invalid_login", c.post("/api/auth/login", json={"email": "alice@example.com", "password": "Nope12345"}))
    w = rec("create_wallet", c.post("/api/wallets", json={"currency": "INR", "name": "Savings"}, headers=A), {"currency": "INR", "name": "Savings"}).json()
    wb = c.post("/api/wallets", json={"currency": "INR", "name": "Main"}, headers=B).json()
    rec("deposit", c.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "1000.00"}, headers={**A, "Idempotency-Key": "abc123"}), {"amount": "1000.00"})
    rec("deposit_replay", c.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "1000.00"}, headers={**A, "Idempotency-Key": "abc123"}), {"amount": "1000.00"})
    rec("idem_conflict", c.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "5.00"}, headers={**A, "Idempotency-Key": "abc123"}), {"amount": "5.00"})
    rec("missing_key", c.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "5.00"}, headers=A))
    treq = {"sender_wallet_id": w["id"], "receiver_wallet_id": wb["id"], "amount": "300.00", "currency": "INR", "reference": "Dinner"}
    t = rec("transfer", c.post("/api/transfers", json=treq, headers={**A, "Idempotency-Key": "tr-001"}), treq).json()
    rec("insufficient", c.post("/api/transfers", json={**treq, "amount": "9000.00"}, headers={**A, "Idempotency-Key": "tr-002"}), {**treq, "amount": "9000.00"})
    rec("self_transfer", c.post("/api/transfers", json={**treq, "receiver_wallet_id": w["id"]}, headers={**A, "Idempotency-Key": "tr-003"}))
    rec("wallet_get", c.get(f"/api/wallets/{w['id']}", headers=A))
    rec("withdrawal", c.post(f"/api/wallets/{w['id']}/withdrawals", json={"amount": "100.00", "destination": "HDFC ****1234"}, headers={**A, "Idempotency-Key": "wd-001"}), {"amount": "100.00", "destination": "HDFC ****1234"})
    rec("transactions", c.get("/api/transactions?type=transfer&status=completed&min_amount=100&page=1&page_size=20", headers=A))
    txid = c.get("/api/transactions?type=transfer&status=completed", headers=A).json()["items"][0]["id"]
    rec("tx_ledger", c.get(f"/api/transactions/{txid}/ledger", headers=A))
    rec("notifications", c.get("/api/notifications?page_size=2", headers=B))
    ref = rec("refund_request", c.post("/api/refunds", json={"transaction_id": txid, "amount": "300.00", "reason": "Wrong recipient"}, headers={**A, "Idempotency-Key": "rf-001"}), {"transaction_id": txid, "amount": "300.00", "reason": "Wrong recipient"}).json()
    sup = make_staff(c, "SUPPORT_AGENT", "support@example.com")
    adm = make_staff(c, "ADMIN", "admin@example.com")
    rec("refund_approve", c.post(f"/api/refunds/{ref['id']}/approve", json={"notes": "Verified"}, headers=sup.h), {"notes": "Verified"})
    rec("freeze", c.post(f"/api/wallets/{wb['id']}/freeze", headers=sup.h))
    rec("transfer_frozen", c.post("/api/transfers", json={**treq, "amount": "10.00"}, headers={**A, "Idempotency-Key": "tr-004"}))
    c.post(f"/api/wallets/{wb['id']}/unfreeze", headers=sup.h)
    rec("limits_get", c.get("/api/admin/transaction-limits", headers=adm.h))
    rec("limits_patch", c.patch("/api/admin/transaction-limits", json={"daily_transfer_limit": "150000.00", "suspicious_amount_threshold": "500.00"}, headers=adm.h), {"daily_transfer_limit": "150000.00", "suspicious_amount_threshold": "500.00"})
    held = rec("transfer_held", c.post("/api/transfers", json={**treq, "amount": "600.00"}, headers={**A, "Idempotency-Key": "tr-005"}), {**treq, "amount": "600.00"}).json()
    rec("wallet_pending", c.get(f"/api/wallets/{w['id']}", headers=A))
    rec("suspicious_list", c.get("/api/admin/suspicious-transactions?review_status=pending", headers=sup.h))
    sid = c.get("/api/admin/suspicious-transactions", headers=sup.h).json()["items"][0]["id"]
    rec("suspicious_review", c.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "APPROVE", "notes": "Customer verified"}, headers=sup.h), {"decision": "APPROVE", "notes": "Customer verified"})
    rec("dash_customer", c.get("/api/dashboard/customer", headers=A))
    rec("dash_support", c.get("/api/dashboard/support", headers=sup.h))
    rec("dash_admin", c.get("/api/dashboard/admin", headers=adm.h))
    rec("audit", c.get("/api/admin/audit-logs?action=TRANSACTION_LIMIT_CHANGED&page_size=1", headers=adm.h))
    rec("validation", c.post("/api/wallets", json={"currency": "INR", "name": ""}, headers=A), {"currency": "INR", "name": ""})
    rec("forbidden", c.get("/api/admin/transaction-limits", headers=A))
    rec("unauth", c.get("/api/wallets"))
    rec("job", c.post("/api/admin/jobs/suspicious_scan/run", headers=adm.h))

def shorten(o):
    if isinstance(o, dict): return {k: shorten(v) for k, v in o.items()}
    if isinstance(o, list): return [shorten(v) for v in o]
    if isinstance(o, str) and o.startswith("eyJ"): return o[:24] + "…"
    return o
json.dump(shorten(S), open(ROOT / "docs" / "api_samples.json", "w"), indent=2)
print({k: v["status"] for k, v in S.items()})

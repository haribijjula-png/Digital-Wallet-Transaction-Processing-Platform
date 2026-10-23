"""Held-withdrawal lifecycle, retry edge cases and remaining admin branches."""
import uuid
from decimal import Decimal

from sqlalchemy import func, select, text

from app.models import LedgerEntry
from tests.helpers import (PASSWORD, bal, create_wallet, deposit, err_code, make_customer, set_limits, transfer,
                           wallet, withdraw)


def held_withdrawal(client, customer, admin, funds="10000.00", amount="1500.00"):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], funds)
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    r = withdraw(client, customer, w["id"], amount)
    assert r.status_code == 202, r.text
    return w, r.json()


def pending_review_id(client, staff):
    return client.get("/api/admin/suspicious-transactions?review_status=pending", headers=staff.h).json()["items"][0]["id"]


# ---------------------------------------------------------------- held withdrawal: approve / reject
def test_approving_held_withdrawal_debits_reserved_funds(client, customer, admin, support, db):
    w, wd = held_withdrawal(client, customer, admin)
    sid = pending_review_id(client, support)
    r = client.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "APPROVE"}, headers=support.h)
    assert r.status_code == 200
    assert client.get(f"/api/withdrawals/{wd['id']}", headers=customer.h).json()["status"] == "COMPLETED"
    snap = wallet(client, customer, w["id"])
    assert snap["available_balance"] == "8500.00" and snap["pending_balance"] == "0.00"
    e = db.scalars(select(LedgerEntry).where(LedgerEntry.transaction_type == "WITHDRAWAL")).one()
    assert (e.balance_before, e.balance_after) == (Decimal("10000.00"), Decimal("8500.00"))
    types = [n["type"] for n in client.get("/api/notifications", headers=customer.h).json()["items"]]
    assert "WITHDRAWAL_COMPLETED" in types and "TRANSACTION_UNDER_REVIEW" in types


def test_rejecting_held_withdrawal_releases_funds(client, customer, admin, support):
    w, wd = held_withdrawal(client, customer, admin)
    sid = pending_review_id(client, support)
    r = client.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "REJECT", "notes": "fraud"},
                    headers=support.h)
    assert r.status_code == 200 and r.json()["review_status"] == "REJECTED" and r.json()["review_notes"] == "fraud"
    got = client.get(f"/api/withdrawals/{wd['id']}", headers=customer.h).json()
    assert got["status"] == "FAILED" and got["failure_reason"] == "REJECTED_BY_REVIEWER"
    snap = wallet(client, customer, w["id"])
    assert snap["available_balance"] == "10000.00" and snap["pending_balance"] == "0.00"
    assert err_code(withdraw(client, customer, w["id"], "999999.00")) == "INSUFFICIENT_BALANCE"


def test_approving_held_withdrawal_fails_cleanly_if_wallet_frozen(client, customer, admin, support):
    w, wd = held_withdrawal(client, customer, admin)
    client.post(f"/api/wallets/{w['id']}/freeze", headers=support.h)
    sid = pending_review_id(client, support)
    client.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "APPROVE"}, headers=support.h)
    got = client.get(f"/api/withdrawals/{wd['id']}", headers=customer.h).json()
    assert got["status"] == "FAILED" and got["failure_reason"] == "WALLET_NOT_ACTIVE"
    snap = wallet(client, customer, w["id"])
    assert snap["available_balance"] == "10000.00" and snap["pending_balance"] == "0.00"


def test_cleanup_cancels_stale_held_withdrawals(client, customer, admin, db):
    w, wd = held_withdrawal(client, customer, admin)
    db.execute(text("UPDATE transactions SET created_at = now() - interval '5 days' WHERE status = 'PENDING'"))
    db.commit()
    out = client.post("/api/admin/jobs/pending_cleanup/run", headers=admin.h).json()["result"]
    assert out["held_withdrawals_cancelled"] == 1
    assert client.get(f"/api/withdrawals/{wd['id']}", headers=customer.h).json()["status"] == "CANCELLED"
    assert wallet(client, customer, w["id"])["pending_balance"] == "0.00"


def test_admin_can_cancel_a_customers_held_withdrawal(client, customer, admin):
    w, wd = held_withdrawal(client, customer, admin)
    assert client.post(f"/api/withdrawals/{wd['id']}/cancel", headers=admin.h).json()["status"] == "CANCELLED"


def test_suspicious_get_and_not_found(client, customer, admin, support):
    held_withdrawal(client, customer, admin)
    sid = pending_review_id(client, support)
    assert client.get(f"/api/admin/suspicious-transactions/{sid}", headers=support.h).json()["id"] == sid
    assert client.get(f"/api/admin/suspicious-transactions/{uuid.uuid4()}", headers=support.h).status_code == 404
    r = client.post(f"/api/admin/suspicious-transactions/{uuid.uuid4()}/review", json={"decision": "APPROVE"},
                    headers=support.h)
    assert r.status_code == 404
    assert client.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "MAYBE"},
                       headers=support.h).status_code == 422


def test_cancelled_review_cannot_be_reviewed_again(client, customer, admin, support):
    w, wd = held_withdrawal(client, customer, admin)
    sid = pending_review_id(client, support)
    client.post(f"/api/withdrawals/{wd['id']}/cancel", headers=customer.h)
    r = client.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "APPROVE"}, headers=support.h)
    assert r.status_code == 409
    assert wallet(client, customer, w["id"])["available_balance"] == "10000.00"        # nothing was debited


# ---------------------------------------------------------------- retry edge cases
def test_withdrawal_retry_gives_up_if_funds_are_gone(client, customer, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "500.00")
    withdraw(client, customer, w["id"], "400.00", simulate_outcome="TIMEOUT")          # FAILED, retryable
    assert withdraw(client, customer, w["id"], "450.00").status_code == 201            # funds spent elsewhere
    out = client.post("/api/admin/jobs/failed_transaction_retry/run", headers=admin.h).json()["result"]
    assert out["withdrawals_retried"] == 1 and out["withdrawals_succeeded"] == 0
    assert bal(client, customer, w["id"]) == Decimal("50.00")                          # never overdrawn
    wd = [x for x in client.get("/api/withdrawals", headers=customer.h).json()["items"] if x["status"] == "FAILED"]
    assert wd[0]["failure_reason"] == "INSUFFICIENT_BALANCE"
    assert client.post("/api/admin/jobs/failed_transaction_retry/run", headers=admin.h).json()["result"][
        "withdrawals_retried"] == 0


def test_retry_gives_up_if_wallet_no_longer_active(client, customer, admin, support):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    d = deposit(client, customer, w["id"], "50.00", simulate_outcome="TIMEOUT")
    withdraw(client, customer, w["id"], "10.00", simulate_outcome="TIMEOUT")
    client.post(f"/api/wallets/{w['id']}/freeze", headers=support.h)
    out = client.post("/api/admin/jobs/failed_transaction_retry/run", headers=admin.h).json()["result"]
    assert out["deposits_succeeded"] == 0 and out["withdrawals_succeeded"] == 0
    assert bal(client, customer, w["id"]) == Decimal("100.00")
    assert client.get(f"/api/deposits/{d['id']}", headers=customer.h).json()["status"] == "FAILED"


# ---------------------------------------------------------------- admin branches
def test_admin_updates_user_name_and_validates(client, admin, customer):
    r = client.patch(f"/api/admin/users/{customer.id}", json={"full_name": "Alice Renamed"}, headers=admin.h)
    assert r.status_code == 200 and r.json()["full_name"] == "Alice Renamed"
    assert err_code(client.patch(f"/api/admin/users/{customer.id}", json={}, headers=admin.h)) == "NO_CHANGES"


def test_super_admin_can_promote_and_demote(client, super_admin):
    u = make_customer(client, "promote@example.com")
    r = client.patch(f"/api/admin/users/{u.id}", json={"role": "SUPPORT_AGENT"}, headers=super_admin.h)
    assert r.status_code == 200 and r.json()["role"] == "SUPPORT_AGENT"
    assert client.patch(f"/api/admin/users/{super_admin.id}", json={"role": "ADMIN"},
                        headers=super_admin.h).status_code == 403                       # not their own role


def test_create_staff_duplicate_email(client, super_admin, customer):
    r = client.post("/api/admin/users", headers=super_admin.h, json={
        "email": customer.email, "full_name": "Dup Dup", "password": PASSWORD, "role": "ADMIN"})
    assert r.status_code == 409 and err_code(r) == "EMAIL_ALREADY_REGISTERED"


def test_adjustment_on_closed_wallet_and_missing_wallet(client, super_admin, customer):
    w = create_wallet(client, customer)
    client.patch(f"/api/wallets/{w['id']}", json={"status": "CLOSED"}, headers=customer.h)
    body = {"amount": "5.00", "direction": "CREDIT", "reason": "late fix"}
    assert err_code(client.post(f"/api/admin/wallets/{w['id']}/adjustments", json=body,
                                headers=super_admin.h)) == "WALLET_CLOSED"
    assert client.post(f"/api/admin/wallets/{uuid.uuid4()}/adjustments", json=body,
                       headers=super_admin.h).status_code == 404


def test_wallet_cap_per_user(client, customer, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "max_wallets_per_user", 2)
    create_wallet(client, customer); create_wallet(client, customer)
    r = client.post("/api/wallets", json={"currency": "INR"}, headers=customer.h)
    assert r.status_code == 409 and err_code(r) == "WALLET_LIMIT_REACHED"


def test_simulation_hook_can_be_disabled(client, customer, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "allow_simulation", False)
    w = create_wallet(client, customer)
    d = deposit(client, customer, w["id"], "10.00", simulate_outcome="FAILURE")
    assert d["status"] == "COMPLETED"            # simulation ignored in "production" mode


def test_manual_processing_mode_leaves_deposits_pending(client, customer, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "auto_process_payments", False)
    w = create_wallet(client, customer)
    d = deposit(client, customer, w["id"], "10.00")
    assert d["status"] == "PENDING" and bal(client, customer, w["id"]) == 0

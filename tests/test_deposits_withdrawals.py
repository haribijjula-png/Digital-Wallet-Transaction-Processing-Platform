import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import LedgerEntry
from tests.helpers import (bal, create_wallet, deposit, err_code, idem, set_limits, wallet, withdraw)


# ================================================================== deposits
def test_deposit_updates_balance_and_writes_ledger(client, customer, db):
    w = create_wallet(client, customer)
    d = deposit(client, customer, w["id"], "1500.50")
    assert d["status"] == "COMPLETED" and d["completed_at"] and d["reference_number"].startswith("DEP-")
    assert bal(client, customer, w["id"]) == Decimal("1500.50")
    entry = db.scalar(select(LedgerEntry))
    assert entry.transaction_type.value == "DEPOSIT" and entry.direction.value == "CREDIT"
    assert entry.balance_before == Decimal("0.00") and entry.balance_after == Decimal("1500.50")
    assert str(entry.created_by) == customer.id


def test_deposit_ledger_tracks_balance_before_after(client, customer, db):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    deposit(client, customer, w["id"], "50.00")
    rows = db.scalars(select(LedgerEntry).order_by(LedgerEntry.created_at, LedgerEntry.balance_after)).all()
    assert [(r.balance_before, r.balance_after) for r in rows] == [
        (Decimal("0.00"), Decimal("100.00")), (Decimal("100.00"), Decimal("150.00"))]


@pytest.mark.parametrize("amount", ["0", "0.00", "-5.00", "10.123", "abc", None])
def test_deposit_invalid_amount(client, customer, amount):
    w = create_wallet(client, customer)
    r = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": amount}, headers={**customer.h, **idem()})
    assert r.status_code == 422 and err_code(r) == "VALIDATION_ERROR"
    assert bal(client, customer, w["id"]) == 0


def test_deposit_requires_idempotency_key(client, customer):
    w = create_wallet(client, customer)
    r = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "10.00"}, headers=customer.h)
    assert r.status_code == 400 and err_code(r) == "IDEMPOTENCY_KEY_REQUIRED"


def test_deposit_to_other_users_wallet_forbidden(client, customer, customer2):
    w = create_wallet(client, customer)
    r = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "10.00"}, headers={**customer2.h, **idem()})
    assert r.status_code == 403 and err_code(r) == "WALLET_ACCESS_DENIED"


def test_deposit_to_missing_wallet(client, customer):
    r = client.post(f"/api/wallets/{uuid.uuid4()}/deposits", json={"amount": "10.00"},
                    headers={**customer.h, **idem()})
    assert r.status_code == 404


def test_deposit_into_frozen_wallet_rejected(client, customer):
    w = create_wallet(client, customer)
    client.post(f"/api/wallets/{w['id']}/freeze", headers=customer.h)
    r = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "10.00"}, headers={**customer.h, **idem()})
    assert r.status_code == 409 and err_code(r) == "WALLET_FROZEN"


def test_deposit_into_closed_wallet_rejected(client, customer):
    w = create_wallet(client, customer)
    client.patch(f"/api/wallets/{w['id']}", json={"status": "CLOSED"}, headers=customer.h)
    r = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "10.00"}, headers={**customer.h, **idem()})
    assert r.status_code == 409 and err_code(r) == "WALLET_CLOSED"


def test_deposit_currency_mismatch(client, customer):
    w = create_wallet(client, customer, "INR")
    r = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "10.00", "currency": "USD"},
                    headers={**customer.h, **idem()})
    assert r.status_code == 400 and err_code(r) == "CURRENCY_MISMATCH"


def test_deposit_over_max_transaction_amount(client, customer, admin):
    w = create_wallet(client, customer)
    set_limits(client, admin, max_transaction_amount="5000.00")
    r = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "5000.01"}, headers={**customer.h, **idem()})
    assert r.status_code == 400 and err_code(r) == "TRANSACTION_LIMIT_EXCEEDED"
    assert bal(client, customer, w["id"]) == 0


def test_failed_deposit_leaves_balance_untouched_and_notifies(client, customer):
    w = create_wallet(client, customer)
    d = deposit(client, customer, w["id"], "100.00", simulate_outcome="FAILURE")
    assert d["status"] == "FAILED" and d["failure_reason"] == "GATEWAY_DECLINED"
    assert bal(client, customer, w["id"]) == 0
    types = [n["type"] for n in client.get("/api/notifications", headers=customer.h).json()["items"]]
    assert "DEPOSIT_FAILED" in types


def test_completed_deposit_notifies(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    types = [n["type"] for n in client.get("/api/notifications", headers=customer.h).json()["items"]]
    assert "DEPOSIT_COMPLETED" in types


def test_list_and_get_deposits_are_scoped(client, customer, customer2, support):
    w = create_wallet(client, customer)
    d = deposit(client, customer, w["id"], "10.00")
    assert client.get("/api/deposits", headers=customer.h).json()["total"] == 1
    assert client.get("/api/deposits", headers=customer2.h).json()["total"] == 0
    assert client.get("/api/deposits", headers=support.h).json()["total"] == 1
    assert client.get(f"/api/deposits/{d['id']}", headers=customer.h).status_code == 200
    assert client.get(f"/api/deposits/{d['id']}", headers=customer2.h).status_code == 403
    assert client.get(f"/api/deposits/{uuid.uuid4()}", headers=customer.h).status_code == 404


def test_deposit_status_filter_case_insensitive(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10.00")
    deposit(client, customer, w["id"], "10.00", simulate_outcome="FAILURE")
    assert client.get("/api/deposits?status=completed", headers=customer.h).json()["total"] == 1
    assert client.get("/api/deposits?status=FAILED", headers=customer.h).json()["total"] == 1
    assert client.get("/api/deposits?status=bogus", headers=customer.h).status_code == 422


# ---------------------------------------------------------------- deposit idempotency
def test_duplicate_deposit_same_key_is_not_applied_twice(client, customer, db):
    w = create_wallet(client, customer)
    key = {"Idempotency-Key": "abc123"}
    r1 = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "100.00"}, headers={**customer.h, **key})
    r2 = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "100.00"}, headers={**customer.h, **key})
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()                                   # exact replay of the first response
    assert r2.headers["idempotent-replayed"] == "true" and "idempotent-replayed" not in r1.headers
    assert bal(client, customer, w["id"]) == Decimal("100.00")
    assert len(db.scalars(select(LedgerEntry)).all()) == 1
    assert client.get("/api/deposits", headers=customer.h).json()["total"] == 1


def test_deposit_same_key_different_payload_conflicts(client, customer):
    w = create_wallet(client, customer)
    key = {"Idempotency-Key": "abc123"}
    client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "100.00"}, headers={**customer.h, **key})
    r = client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "999.00"}, headers={**customer.h, **key})
    assert r.status_code == 409 and err_code(r) == "DUPLICATE_IDEMPOTENCY_KEY"
    assert bal(client, customer, w["id"]) == Decimal("100.00")


def test_idempotency_keys_are_per_user(client, customer, customer2):
    w1, w2 = create_wallet(client, customer), create_wallet(client, customer2)
    key = {"Idempotency-Key": "shared-key"}
    assert client.post(f"/api/wallets/{w1['id']}/deposits", json={"amount": "10.00"},
                       headers={**customer.h, **key}).status_code == 201
    r = client.post(f"/api/wallets/{w2['id']}/deposits", json={"amount": "10.00"}, headers={**customer2.h, **key})
    assert r.status_code == 201 and "idempotent-replayed" not in r.headers
    assert bal(client, customer2, w2["id"]) == Decimal("10.00")


def test_rejected_request_does_not_burn_the_key(client, customer):
    """A request that failed validation of a business rule rolls back, so the same key can be retried."""
    w = create_wallet(client, customer)
    client.post(f"/api/wallets/{w['id']}/freeze", headers=customer.h)
    key = {"Idempotency-Key": "retry-me"}
    assert client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "10.00"},
                       headers={**customer.h, **key}).status_code == 409
    # staff unfreeze, then the client retries with the SAME key and it works
    from tests.helpers import make_staff
    sup = make_staff(client, "SUPPORT_AGENT", "s@example.com")
    client.post(f"/api/wallets/{w['id']}/unfreeze", headers=sup.h)
    assert client.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "10.00"},
                       headers={**customer.h, **key}).status_code == 201


# ================================================================== withdrawals
def test_withdrawal_success(client, customer, db):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "1000.00")
    r = withdraw(client, customer, w["id"], "400.00", destination="HDFC ****1234")
    assert r.status_code == 201 and r.json()["status"] == "COMPLETED"
    assert r.json()["reference_number"].startswith("WDR-")
    assert bal(client, customer, w["id"]) == Decimal("600.00")
    e = db.scalars(select(LedgerEntry).where(LedgerEntry.transaction_type == "WITHDRAWAL")).one()
    assert (e.balance_before, e.balance_after, e.direction.value) == (Decimal("1000.00"), Decimal("600.00"), "DEBIT")


def test_withdrawal_exact_balance_allowed(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    assert withdraw(client, customer, w["id"], "100.00").status_code == 201
    assert bal(client, customer, w["id"]) == 0


def test_withdrawal_insufficient_balance(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    r = withdraw(client, customer, w["id"], "100.01")
    assert r.status_code == 400 and err_code(r) == "INSUFFICIENT_BALANCE"
    assert r.json()["error"]["message"] == "Insufficient wallet balance."
    assert bal(client, customer, w["id"]) == Decimal("100.00")


def test_failed_withdrawal_is_recorded_and_notified(client, customer):
    w = create_wallet(client, customer)
    withdraw(client, customer, w["id"], "5.00")
    items = client.get("/api/withdrawals", headers=customer.h).json()["items"]
    assert len(items) == 1 and items[0]["status"] == "FAILED" and items[0]["failure_reason"] == "INSUFFICIENT_BALANCE"
    types = [n["type"] for n in client.get("/api/notifications", headers=customer.h).json()["items"]]
    assert "WITHDRAWAL_FAILED" in types


@pytest.mark.parametrize("amount", ["0", "-1", "1.005"])
def test_withdrawal_invalid_amount(client, customer, amount):
    w = create_wallet(client, customer)
    assert withdraw(client, customer, w["id"], amount).status_code == 422


def test_frozen_wallet_cannot_withdraw(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    client.post(f"/api/wallets/{w['id']}/freeze", headers=customer.h)
    r = withdraw(client, customer, w["id"], "10.00")
    assert r.status_code == 409 and err_code(r) == "WALLET_FROZEN"
    assert bal(client, customer, w["id"]) == Decimal("100.00")


def test_closed_wallet_cannot_withdraw(client, customer):
    w = create_wallet(client, customer)
    client.patch(f"/api/wallets/{w['id']}", json={"status": "CLOSED"}, headers=customer.h)
    r = withdraw(client, customer, w["id"], "10.00")
    assert r.status_code == 409 and err_code(r) == "WALLET_CLOSED"


def test_cannot_withdraw_from_someone_elses_wallet(client, customer, customer2):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    r = withdraw(client, customer2, w["id"], "10.00")
    assert r.status_code == 403
    assert bal(client, customer, w["id"]) == Decimal("100.00")


def test_daily_withdrawal_limit_enforced(client, customer, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10000.00")
    set_limits(client, admin, daily_withdrawal_limit="1000.00")
    assert withdraw(client, customer, w["id"], "600.00").status_code == 201
    r = withdraw(client, customer, w["id"], "500.00")                   # 600 + 500 > 1000
    assert r.status_code == 400 and err_code(r) == "TRANSACTION_LIMIT_EXCEEDED"
    assert "Daily withdrawal limit" in r.json()["error"]["message"]
    assert withdraw(client, customer, w["id"], "400.00").status_code == 201   # exactly at the limit
    assert bal(client, customer, w["id"]) == Decimal("9000.00")


def test_failed_withdrawals_do_not_consume_daily_limit(client, customer, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "300.00")
    set_limits(client, admin, daily_withdrawal_limit="500.00")
    assert withdraw(client, customer, w["id"], "400.00").status_code == 400     # insufficient -> FAILED
    assert withdraw(client, customer, w["id"], "300.00").status_code == 201


def test_withdrawal_over_max_transaction_amount(client, customer, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10000.00")
    set_limits(client, admin, max_transaction_amount="2000.00")
    r = withdraw(client, customer, w["id"], "2000.01")
    assert r.status_code == 400 and err_code(r) == "TRANSACTION_LIMIT_EXCEEDED"
    assert bal(client, customer, w["id"]) == Decimal("10000.00")


def test_max_transactions_per_day_enforced(client, customer, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "1000.00")
    set_limits(client, admin, max_transactions_per_day=2, frequency_review_threshold=50)
    assert withdraw(client, customer, w["id"], "1.00").status_code == 201
    assert withdraw(client, customer, w["id"], "1.00").status_code == 201
    r = withdraw(client, customer, w["id"], "1.00")
    assert r.status_code == 400 and err_code(r) == "TRANSACTION_LIMIT_EXCEEDED"


def test_blocked_withdrawal_is_flagged_and_audited(client, customer, admin, support):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10000.00")
    set_limits(client, admin, max_transaction_amount="2000.00")
    withdraw(client, customer, w["id"], "3000.00")
    flagged = client.get("/api/admin/suspicious-transactions?risk_status=blocked", headers=support.h).json()
    assert flagged["total"] == 1 and "MAX_TRANSACTION_AMOUNT_EXCEEDED" in flagged["items"][0]["reasons"]
    logs = client.get("/api/admin/audit-logs?action=TRANSACTION_BLOCKED", headers=admin.h).json()
    assert logs["total"] == 1


def test_withdrawal_idempotent_replay(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "1000.00")
    h = {**customer.h, "Idempotency-Key": "wd-1"}
    r1 = client.post(f"/api/wallets/{w['id']}/withdrawals", json={"amount": "300.00"}, headers=h)
    r2 = client.post(f"/api/wallets/{w['id']}/withdrawals", json={"amount": "300.00"}, headers=h)
    assert r1.json() == r2.json() and r2.headers["idempotent-replayed"] == "true"
    assert bal(client, customer, w["id"]) == Decimal("700.00")                       # debited once
    assert client.get("/api/withdrawals", headers=customer.h).json()["total"] == 1


def test_withdrawal_idempotency_key_reuse_with_other_amount(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "1000.00")
    h = {**customer.h, "Idempotency-Key": "wd-1"}
    client.post(f"/api/wallets/{w['id']}/withdrawals", json={"amount": "300.00"}, headers=h)
    r = client.post(f"/api/wallets/{w['id']}/withdrawals", json={"amount": "301.00"}, headers=h)
    assert r.status_code == 409 and err_code(r) == "DUPLICATE_IDEMPOTENCY_KEY"


def test_failed_withdrawal_replay_returns_same_error_without_new_record(client, customer):
    w = create_wallet(client, customer)
    h = {**customer.h, "Idempotency-Key": "wd-fail"}
    r1 = client.post(f"/api/wallets/{w['id']}/withdrawals", json={"amount": "50.00"}, headers=h)
    r2 = client.post(f"/api/wallets/{w['id']}/withdrawals", json={"amount": "50.00"}, headers=h)
    assert r1.status_code == r2.status_code == 400 and r1.json() == r2.json()
    assert client.get("/api/withdrawals", headers=customer.h).json()["total"] == 1


def test_withdrawal_requires_idempotency_key(client, customer):
    w = create_wallet(client, customer)
    r = client.post(f"/api/wallets/{w['id']}/withdrawals", json={"amount": "1.00"}, headers=customer.h)
    assert r.status_code == 400 and err_code(r) == "IDEMPOTENCY_KEY_REQUIRED"


def test_withdrawal_gateway_decline_keeps_balance(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    r = withdraw(client, customer, w["id"], "50.00", simulate_outcome="FAILURE")
    assert r.status_code == 201 and r.json()["status"] == "FAILED"
    assert bal(client, customer, w["id"]) == Decimal("100.00")


def test_get_and_list_withdrawals_scoped(client, customer, customer2):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    wd = withdraw(client, customer, w["id"], "10.00").json()
    assert client.get(f"/api/withdrawals/{wd['id']}", headers=customer.h).status_code == 200
    assert client.get(f"/api/withdrawals/{wd['id']}", headers=customer2.h).status_code == 403
    assert client.get("/api/withdrawals", headers=customer2.h).json()["total"] == 0


def test_cancel_completed_withdrawal_is_invalid(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    wd = withdraw(client, customer, w["id"], "10.00").json()
    r = client.post(f"/api/withdrawals/{wd['id']}/cancel", headers=customer.h)
    assert r.status_code == 409 and err_code(r) == "INVALID_STATE_TRANSITION"
    assert bal(client, customer, w["id"]) == Decimal("90.00")


def test_held_withdrawal_reserves_funds_and_can_be_cancelled(client, customer, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10000.00")
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    r = withdraw(client, customer, w["id"], "1500.00")
    assert r.status_code == 202 and r.json()["status"] == "PENDING"
    snap = wallet(client, customer, w["id"])
    assert snap["available_balance"] == "10000.00" and snap["pending_balance"] == "1500.00"
    # reserved funds cannot be spent twice
    assert withdraw(client, customer, w["id"], "8600.00").status_code == 400
    c = client.post(f"/api/withdrawals/{r.json()['id']}/cancel", headers=customer.h)
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    snap = wallet(client, customer, w["id"])
    assert snap["available_balance"] == "10000.00" and snap["pending_balance"] == "0.00"


def test_other_user_cannot_cancel_withdrawal(client, customer, customer2, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10000.00")
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    wd = withdraw(client, customer, w["id"], "1500.00").json()
    assert client.post(f"/api/withdrawals/{wd['id']}/cancel", headers=customer2.h).status_code == 403

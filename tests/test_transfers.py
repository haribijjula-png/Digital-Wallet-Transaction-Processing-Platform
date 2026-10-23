import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import LedgerEntry
from tests.helpers import (bal, create_wallet, deposit, err_code, idem, make_customer, set_limits, transfer, wallet)


def test_transfer_success_moves_money_and_writes_two_ledger_entries(client, customer, customer2, funded, db):
    a, b = funded
    r = transfer(client, customer, a["id"], b["id"], "300.00", reference="Rent")
    assert r.status_code == 201
    t = r.json()
    assert t["status"] == "COMPLETED" and t["reference"] == "Rent" and t["reference_number"].startswith("TRF-")
    assert t["completed_at"] and t["currency"] == "INR"
    assert bal(client, customer, a["id"]) == Decimal("700.00")
    assert bal(client, customer2, b["id"]) == Decimal("300.00")

    entries = db.scalars(select(LedgerEntry).where(LedgerEntry.transaction_type.in_(
        ["TRANSFER_DEBIT", "TRANSFER_CREDIT"]))).all()
    by_type = {e.transaction_type.value: e for e in entries}
    assert by_type["TRANSFER_DEBIT"].balance_before == Decimal("1000.00")
    assert by_type["TRANSFER_DEBIT"].balance_after == Decimal("700.00")
    assert by_type["TRANSFER_CREDIT"].balance_before == Decimal("0.00")
    assert by_type["TRANSFER_CREDIT"].balance_after == Decimal("300.00")
    assert by_type["TRANSFER_DEBIT"].transaction_id == by_type["TRANSFER_CREDIT"].transaction_id


def test_transfer_notifies_both_parties(client, customer, customer2, funded):
    a, b = funded
    transfer(client, customer, a["id"], b["id"], "10.00")
    sender = [n["type"] for n in client.get("/api/notifications", headers=customer.h).json()["items"]]
    receiver = [n["type"] for n in client.get("/api/notifications", headers=customer2.h).json()["items"]]
    assert "TRANSFER_COMPLETED" in sender and "TRANSFER_RECEIVED" in receiver


def test_transfer_entire_balance(client, customer, customer2, funded):
    a, b = funded
    assert transfer(client, customer, a["id"], b["id"], "1000.00").status_code == 201
    assert bal(client, customer, a["id"]) == 0 and bal(client, customer2, b["id"]) == Decimal("1000.00")


# ---------------------------------------------------------------- failures never touch balances
def test_insufficient_balance_fails_without_debit(client, customer, customer2, funded):
    a, b = funded
    r = transfer(client, customer, a["id"], b["id"], "1000.01")
    assert r.status_code == 400 and err_code(r) == "INSUFFICIENT_BALANCE"
    assert bal(client, customer, a["id"]) == Decimal("1000.00")
    assert bal(client, customer2, b["id"]) == 0
    # the failed attempt is recorded for history/audit
    listed = client.get("/api/transfers", headers=customer.h).json()["items"]
    assert len(listed) == 1 and listed[0]["status"] == "FAILED" and listed[0]["failure_reason"] == "INSUFFICIENT_BALANCE"
    types = [n["type"] for n in client.get("/api/notifications", headers=customer.h).json()["items"]]
    assert "TRANSFER_FAILED" in types


@pytest.mark.parametrize("amount", ["0", "0.00", "-10", "1.234", "x"])
def test_invalid_amount(client, customer, funded, amount):
    a, b = funded
    r = transfer(client, customer, a["id"], b["id"], amount)
    assert r.status_code == 422
    assert bal(client, customer, a["id"]) == Decimal("1000.00")


def test_cannot_transfer_to_own_wallet(client, customer, funded):
    a, _ = funded
    r = transfer(client, customer, a["id"], a["id"], "10.00")
    assert r.status_code == 400 and err_code(r) == "INVALID_TRANSFER"


def test_cannot_transfer_between_own_wallets(client, customer, funded):
    a, _ = funded
    other = create_wallet(client, customer, "INR", "Second")
    r = transfer(client, customer, a["id"], other["id"], "10.00")
    assert r.status_code == 400 and err_code(r) == "INVALID_TRANSFER" and "yourself" in r.json()["error"]["message"]


def test_currency_mismatch(client, customer, customer2):
    a = create_wallet(client, customer, "INR")
    deposit(client, customer, a["id"], "100.00")
    usd = create_wallet(client, customer2, "USD")
    r = transfer(client, customer, a["id"], usd["id"], "10.00")
    assert r.status_code == 400 and err_code(r) == "INVALID_TRANSFER" and "currenc" in r.json()["error"]["message"]
    assert bal(client, customer, a["id"]) == Decimal("100.00")


def test_explicit_currency_must_match(client, customer, funded):
    a, b = funded
    r = transfer(client, customer, a["id"], b["id"], "10.00", currency="USD")
    assert r.status_code == 400 and err_code(r) == "INVALID_TRANSFER"


def test_frozen_sender_cannot_initiate(client, customer, funded):
    a, b = funded
    client.post(f"/api/wallets/{a['id']}/freeze", headers=customer.h)
    r = transfer(client, customer, a["id"], b["id"], "10.00")
    assert r.status_code == 409 and err_code(r) == "WALLET_FROZEN"
    assert bal(client, customer, a["id"]) == Decimal("1000.00")


def test_frozen_receiver_rejected(client, customer, customer2, funded):
    a, b = funded
    client.post(f"/api/wallets/{b['id']}/freeze", headers=customer2.h)
    r = transfer(client, customer, a["id"], b["id"], "10.00")
    assert r.status_code == 409 and err_code(r) == "WALLET_FROZEN"
    assert bal(client, customer, a["id"]) == Decimal("1000.00")


def test_closed_receiver_rejected(client, customer, customer2, funded):
    a, b = funded
    client.patch(f"/api/wallets/{b['id']}", json={"status": "CLOSED"}, headers=customer2.h)
    r = transfer(client, customer, a["id"], b["id"], "10.00")
    assert r.status_code == 409 and err_code(r) == "WALLET_CLOSED"


def test_suspended_receiver_rejected(client, customer, funded, admin):
    a, b = funded
    client.patch(f"/api/wallets/{b['id']}", json={"status": "SUSPENDED"}, headers=admin.h)
    r = transfer(client, customer, a["id"], b["id"], "10.00")
    assert r.status_code == 409 and err_code(r) == "WALLET_SUSPENDED"


def test_unknown_receiver_404(client, customer, funded):
    a, _ = funded
    r = transfer(client, customer, a["id"], str(uuid.uuid4()), "10.00")
    assert r.status_code == 404 and err_code(r) == "WALLET_NOT_FOUND"


def test_unknown_sender_404(client, customer, funded):
    _, b = funded
    assert transfer(client, customer, str(uuid.uuid4()), b["id"], "10.00").status_code == 404


def test_cannot_send_from_someone_elses_wallet(client, customer, customer2, funded):
    a, b = funded
    r = transfer(client, customer2, a["id"], b["id"], "10.00")
    assert r.status_code == 403 and err_code(r) == "WALLET_ACCESS_DENIED"
    assert bal(client, customer, a["id"]) == Decimal("1000.00")


def test_staff_cannot_create_transfers(client, support, funded):
    a, b = funded
    assert transfer(client, support, a["id"], b["id"], "10.00").status_code == 403


def test_transfer_requires_idempotency_key(client, customer, funded):
    a, b = funded
    r = client.post("/api/transfers", headers=customer.h,
                    json={"sender_wallet_id": a["id"], "receiver_wallet_id": b["id"], "amount": "10.00"})
    assert r.status_code == 400 and err_code(r) == "IDEMPOTENCY_KEY_REQUIRED"


# ---------------------------------------------------------------- idempotency
def test_duplicate_transfer_same_key_executes_once(client, customer, customer2, funded, db):
    a, b = funded
    key = "transfer-abc123"
    r1 = transfer(client, customer, a["id"], b["id"], "400.00", key=key)
    r2 = transfer(client, customer, a["id"], b["id"], "400.00", key=key)
    r3 = transfer(client, customer, a["id"], b["id"], "400.00", key=key)
    assert r1.status_code == r2.status_code == r3.status_code == 201
    assert r1.json() == r2.json() == r3.json()
    assert r2.headers["idempotent-replayed"] == "true"
    assert bal(client, customer, a["id"]) == Decimal("600.00")
    assert bal(client, customer2, b["id"]) == Decimal("400.00")
    assert client.get("/api/transfers", headers=customer.h).json()["total"] == 1
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.transaction_type == "TRANSFER_DEBIT")) == 1


def test_same_key_different_payload_is_conflict(client, customer, funded):
    a, b = funded
    transfer(client, customer, a["id"], b["id"], "100.00", key="k1")
    r = transfer(client, customer, a["id"], b["id"], "200.00", key="k1")
    assert r.status_code == 409 and err_code(r) == "DUPLICATE_IDEMPOTENCY_KEY"
    assert bal(client, customer, a["id"]) == Decimal("900.00")


def test_failed_transfer_replay_returns_same_failure(client, customer, funded):
    a, b = funded
    r1 = transfer(client, customer, a["id"], b["id"], "5000.00", key="fail-1")
    r2 = transfer(client, customer, a["id"], b["id"], "5000.00", key="fail-1")
    assert r1.status_code == r2.status_code == 400 and r1.json() == r2.json()
    assert client.get("/api/transfers", headers=customer.h).json()["total"] == 1     # one FAILED record only


# ---------------------------------------------------------------- limits
def test_max_transaction_amount(client, customer, funded, admin):
    a, b = funded
    set_limits(client, admin, max_transaction_amount="500.00")
    r = transfer(client, customer, a["id"], b["id"], "500.01")
    assert r.status_code == 400 and err_code(r) == "TRANSACTION_LIMIT_EXCEEDED"
    assert bal(client, customer, a["id"]) == Decimal("1000.00")
    assert transfer(client, customer, a["id"], b["id"], "500.00").status_code == 201


def test_daily_transfer_limit(client, customer, customer2, funded, admin):
    a, b = funded
    set_limits(client, admin, daily_transfer_limit="600.00")
    assert transfer(client, customer, a["id"], b["id"], "400.00").status_code == 201
    r = transfer(client, customer, a["id"], b["id"], "201.00")
    assert r.status_code == 400 and err_code(r) == "TRANSACTION_LIMIT_EXCEEDED"
    assert transfer(client, customer, a["id"], b["id"], "200.00").status_code == 201
    assert bal(client, customer2, b["id"]) == Decimal("600.00")


def test_blocked_transfer_creates_failed_record_suspicious_entry_and_staff_notification(
        client, customer, funded, admin, support):
    a, b = funded
    set_limits(client, admin, max_transaction_amount="500.00")
    transfer(client, customer, a["id"], b["id"], "800.00")
    t = client.get("/api/transfers", headers=customer.h).json()["items"][0]
    assert t["status"] == "FAILED"
    sus = client.get("/api/admin/suspicious-transactions", headers=support.h).json()["items"]
    assert len(sus) == 1 and sus[0]["risk_status"] == "BLOCKED" and sus[0]["review_status"] == "NOT_REQUIRED"
    staff_types = [n["type"] for n in client.get("/api/notifications", headers=support.h).json()["items"]]
    assert "SUSPICIOUS_TRANSACTION" in staff_types


# ---------------------------------------------------------------- suspicious rules / held transfers
def _hold(client, customer, a, b, admin, amount="3000.00"):
    set_limits(client, admin, suspicious_amount_threshold="2000.00")
    r = transfer(client, customer, a["id"], b["id"], amount)
    assert r.status_code == 202, r.text
    return r.json()


def test_large_amount_is_held_for_review_with_reserved_funds(client, customer, customer2, admin, support):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    t = _hold(client, customer, a, b, admin)
    assert t["status"] == "PENDING"
    snap = wallet(client, customer, a["id"])
    assert snap["available_balance"] == "10000.00" and snap["pending_balance"] == "3000.00"
    assert bal(client, customer2, b["id"]) == 0                                   # nothing credited yet
    txs = client.get("/api/transactions?status=pending", headers=support.h).json()["items"]
    assert txs[0]["risk_status"] == "REVIEW_REQUIRED"
    sus = client.get("/api/admin/suspicious-transactions?review_status=pending", headers=support.h).json()
    assert sus["total"] == 1 and "AMOUNT_ABOVE_THRESHOLD" in sus["items"][0]["reasons"]


def test_reserved_funds_cannot_be_double_spent(client, customer, customer2, admin):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "4000.00")
    _hold(client, customer, a, b, admin, "3000.00")
    r = transfer(client, customer, a["id"], b["id"], "1500.00")                   # only 1000 spendable
    assert r.status_code == 400 and err_code(r) == "INSUFFICIENT_BALANCE"
    assert transfer(client, customer, a["id"], b["id"], "1000.00").status_code == 201


def test_staff_approval_settles_held_transfer(client, customer, customer2, admin, support, db):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    t = _hold(client, customer, a, b, admin)
    sid = client.get("/api/admin/suspicious-transactions", headers=support.h).json()["items"][0]["id"]
    r = client.post(f"/api/admin/suspicious-transactions/{sid}/review",
                    json={"decision": "APPROVE", "notes": "verified with customer"}, headers=support.h)
    assert r.status_code == 200 and r.json()["review_status"] == "APPROVED"
    assert client.get(f"/api/transfers/{t['id']}", headers=customer.h).json()["status"] == "COMPLETED"
    snap = wallet(client, customer, a["id"])
    assert snap["available_balance"] == "7000.00" and snap["pending_balance"] == "0.00"
    assert bal(client, customer2, b["id"]) == Decimal("3000.00")
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.transaction_type == "TRANSFER_DEBIT")) == 1
    # a second review of the same record is invalid
    r2 = client.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "APPROVE"}, headers=support.h)
    assert r2.status_code == 409 and err_code(r2) == "INVALID_STATE_TRANSITION"
    assert bal(client, customer2, b["id"]) == Decimal("3000.00")


def test_staff_rejection_releases_hold(client, customer, customer2, admin, support):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    t = _hold(client, customer, a, b, admin)
    sid = client.get("/api/admin/suspicious-transactions", headers=support.h).json()["items"][0]["id"]
    r = client.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "REJECT"}, headers=support.h)
    assert r.status_code == 200 and r.json()["review_status"] == "REJECTED"
    assert client.get(f"/api/transfers/{t['id']}", headers=customer.h).json()["status"] == "FAILED"
    snap = wallet(client, customer, a["id"])
    assert snap["available_balance"] == "10000.00" and snap["pending_balance"] == "0.00"
    assert bal(client, customer2, b["id"]) == 0


def test_customer_cannot_review_suspicious(client, customer, customer2, admin):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    _hold(client, customer, a, b, admin)
    assert client.get("/api/admin/suspicious-transactions", headers=customer.h).status_code == 403
    assert client.post(f"/api/admin/suspicious-transactions/{uuid.uuid4()}/review",
                       json={"decision": "APPROVE"}, headers=customer.h).status_code == 403


def test_sender_can_cancel_held_transfer(client, customer, customer2, admin):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    t = _hold(client, customer, a, b, admin)
    r = client.post(f"/api/transfers/{t['id']}/cancel", headers=customer.h)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert wallet(client, customer, a["id"])["pending_balance"] == "0.00"
    # the pending review record is closed too
    sus = client.get("/api/admin/suspicious-transactions", headers=admin.h).json()["items"][0]
    assert sus["review_status"] == "CANCELLED"
    # cancelling twice is invalid
    assert client.post(f"/api/transfers/{t['id']}/cancel", headers=customer.h).status_code == 409


def test_receiver_and_strangers_cannot_cancel(client, customer, customer2, admin):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    t = _hold(client, customer, a, b, admin)
    assert client.post(f"/api/transfers/{t['id']}/cancel", headers=customer2.h).status_code == 403
    stranger = make_customer(client, "eve@example.com")
    assert client.post(f"/api/transfers/{t['id']}/cancel", headers=stranger.h).status_code == 403


def test_cannot_cancel_completed_transfer(client, customer, funded):
    a, b = funded
    t = transfer(client, customer, a["id"], b["id"], "10.00").json()
    r = client.post(f"/api/transfers/{t['id']}/cancel", headers=customer.h)
    assert r.status_code == 409 and err_code(r) == "INVALID_STATE_TRANSITION"
    assert bal(client, customer, a["id"]) == Decimal("990.00")


def test_approval_fails_cleanly_if_wallet_frozen_meanwhile(client, customer, customer2, admin, support):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    t = _hold(client, customer, a, b, admin)
    client.post(f"/api/wallets/{b['id']}/freeze", headers=support.h)
    sid = client.get("/api/admin/suspicious-transactions", headers=support.h).json()["items"][0]["id"]
    client.post(f"/api/admin/suspicious-transactions/{sid}/review", json={"decision": "APPROVE"}, headers=support.h)
    got = client.get(f"/api/transfers/{t['id']}", headers=customer.h).json()
    assert got["status"] == "FAILED" and got["failure_reason"] == "WALLET_NOT_ACTIVE"
    snap = wallet(client, customer, a["id"])
    assert snap["available_balance"] == "10000.00" and snap["pending_balance"] == "0.00"


def test_velocity_rule_flags_then_blocks(client, customer, customer2, admin, support):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "1000.00")
    set_limits(client, admin, velocity_review_count=3, velocity_block_count=5, velocity_window_minutes=10)
    codes = [transfer(client, customer, a["id"], b["id"], "10.00").status_code for _ in range(6)]
    # 1,2 normal | 3,4 held for review | 5th+ blocked
    assert codes[:2] == [201, 201] and codes[2:4] == [202, 202] and codes[4:] == [403, 403]
    r = transfer(client, customer, a["id"], b["id"], "10.00")
    assert err_code(r) == "SUSPICIOUS_TRANSACTION"
    reasons = {x for s in client.get("/api/admin/suspicious-transactions", headers=support.h).json()["items"]
               for x in s["reasons"]}
    assert {"MULTIPLE_TRANSACTIONS_SHORT_PERIOD", "VELOCITY_BLOCK"} <= reasons


def test_recently_frozen_wallet_triggers_review(client, customer, customer2, admin, support):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "1000.00")
    client.post(f"/api/wallets/{a['id']}/freeze", headers=support.h)
    client.post(f"/api/wallets/{a['id']}/unfreeze", headers=support.h)
    r = transfer(client, customer, a["id"], b["id"], "10.00")
    assert r.status_code == 202
    sus = client.get("/api/admin/suspicious-transactions", headers=support.h).json()["items"][0]
    assert "WALLET_RECENTLY_FROZEN" in sus["reasons"]


def test_unusual_frequency_rule(client, customer, customer2, admin, support):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "1000.00")
    set_limits(client, admin, frequency_review_threshold=3, velocity_review_count=100, velocity_block_count=200)
    codes = [transfer(client, customer, a["id"], b["id"], "1.00").status_code for _ in range(3)]
    assert codes == [201, 201, 202]
    assert "UNUSUAL_TRANSACTION_FREQUENCY" in client.get(
        "/api/admin/suspicious-transactions", headers=support.h).json()["items"][0]["reasons"]


# ---------------------------------------------------------------- read access
def test_transfer_visibility(client, customer, customer2, funded, support):
    a, b = funded
    t = transfer(client, customer, a["id"], b["id"], "10.00").json()
    stranger = make_customer(client, "eve@example.com")
    for who, expected in ((customer, 200), (customer2, 200), (support, 200), (stranger, 403)):
        assert client.get(f"/api/transfers/{t['id']}", headers=who.h).status_code == expected
    assert client.get("/api/transfers", headers=stranger.h).json()["total"] == 0
    assert client.get("/api/transfers", headers=customer2.h).json()["total"] == 1
    assert client.get(f"/api/transfers/{uuid.uuid4()}", headers=customer.h).status_code == 404

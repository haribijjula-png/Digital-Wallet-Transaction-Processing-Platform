"""Concurrency tests run against REAL PostgreSQL with truly parallel requests (threads)
released simultaneously by a barrier. They verify the guarantees promised in the README:
no negative balances, no double spending, no lost updates, no duplicate transactions."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.core.database import SessionLocal
from app.main import app
from app.models import LedgerEntry, Transfer
from tests.helpers import (bal, create_wallet, deposit, make_customer, set_limits, transfer, wallet, withdraw)


@pytest.fixture
def relaxed(client, admin):
    """Disable velocity / frequency rules so only the money invariants are under test."""
    set_limits(client, admin, velocity_review_count=1000, velocity_block_count=2000,
               frequency_review_threshold=1000, max_transactions_per_day=1000,
               daily_transfer_limit="10000000.00", daily_withdrawal_limit="10000000.00")


def run_parallel(n, fn):
    """Run fn(i) in n threads, all starting at the same instant."""
    barrier = threading.Barrier(n)

    def worker(i):
        with TestClient(app) as c:        # own client per thread
            barrier.wait(timeout=10)
            return fn(c, i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(worker, range(n)))


def ledger_balance(wallet_id) -> Decimal:
    with SessionLocal() as db:
        credits = db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.wallet_id == wallet_id, LedgerEntry.direction == "CREDIT"))
        debits = db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.wallet_id == wallet_id, LedgerEntry.direction == "DEBIT"))
        return credits - debits


# ---------------------------------------------------------------- the spec's example
def test_two_competing_transfers_only_one_succeeds(client, customer, customer2, funded, relaxed):
    """Balance 1000. Request A -> 800 and Request B -> 700 must NOT both succeed."""
    a, b = funded
    amounts = ["800.00", "700.00"]
    results = run_parallel(2, lambda c, i: transfer(c, customer, a["id"], b["id"], amounts[i]))
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 400], [r.text for r in results]
    failed = next(r for r in results if r.status_code == 400)
    assert failed.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    winner = Decimal(next(r for r in results if r.status_code == 201).json()["amount"])
    assert bal(client, customer, a["id"]) == Decimal("1000.00") - winner       # 200 or 300, never negative
    assert bal(client, customer2, b["id"]) == winner
    assert ledger_balance(a["id"]) == bal(client, customer, a["id"])


def test_many_concurrent_transfers_never_overspend(client, customer, customer2, funded, relaxed):
    a, b = funded
    n, amount = 20, Decimal("150.00")                                             # 20 x 150 = 3000 vs 1000 funds
    results = run_parallel(n, lambda c, i: transfer(c, customer, a["id"], b["id"], str(amount)))
    ok = [r for r in results if r.status_code == 201]
    bad = [r for r in results if r.status_code != 201]
    assert len(ok) == 6                                                           # floor(1000 / 150)
    assert all(r.json()["error"]["code"] == "INSUFFICIENT_BALANCE" for r in bad)
    assert bal(client, customer, a["id"]) == Decimal("100.00")
    assert bal(client, customer2, b["id"]) == Decimal("900.00")
    assert ledger_balance(a["id"]) == Decimal("100.00") and ledger_balance(b["id"]) == Decimal("900.00")


def test_concurrent_withdrawals_never_overdraw(client, customer, relaxed):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "1000.00")
    results = run_parallel(15, lambda c, i: withdraw(c, customer, w["id"], "200.00"))
    assert sum(r.status_code == 201 for r in results) == 5
    assert all(r.status_code in (201, 400) for r in results)
    assert bal(client, customer, w["id"]) == Decimal("0.00")
    assert ledger_balance(w["id"]) == Decimal("0.00")


def test_concurrent_mixed_withdrawals_and_transfers_share_one_balance(client, customer, customer2, funded, relaxed):
    a, b = funded

    def op(c, i):
        if i % 2:
            return withdraw(c, customer, a["id"], "300.00")
        return transfer(c, customer, a["id"], b["id"], "300.00")
    results = run_parallel(10, op)
    assert sum(r.status_code == 201 for r in results) == 3                         # 1000 // 300
    assert bal(client, customer, a["id"]) == Decimal("100.00")
    assert ledger_balance(a["id"]) == Decimal("100.00")


# ---------------------------------------------------------------- lost updates on the receiver
def test_concurrent_deposits_no_lost_updates(client, customer):
    w = create_wallet(client, customer)
    results = run_parallel(20, lambda c, i: deposit(c, customer, w["id"], "10.00"))
    assert len(results) == 20
    assert bal(client, customer, w["id"]) == Decimal("200.00")
    assert ledger_balance(w["id"]) == Decimal("200.00")


def test_concurrent_credits_to_same_receiver_no_lost_updates(client, customer2, relaxed):
    receiver = create_wallet(client, customer2)
    senders = []
    for i in range(10):
        u = make_customer(client, f"s{i}@example.com")
        w = create_wallet(client, u)
        deposit(client, u, w["id"], "100.00")
        senders.append((u, w))
    results = run_parallel(10, lambda c, i: transfer(c, senders[i][0], senders[i][1]["id"], receiver["id"], "40.00"))
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    assert bal(client, customer2, receiver["id"]) == Decimal("400.00")
    assert ledger_balance(receiver["id"]) == Decimal("400.00")


# ---------------------------------------------------------------- deadlocks
def test_opposite_direction_transfers_do_not_deadlock(client, customer, customer2, relaxed):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "5000.00")
    deposit(client, customer2, b["id"], "5000.00")

    def op(c, i):
        if i % 2:
            return transfer(c, customer, a["id"], b["id"], "10.00")
        return transfer(c, customer2, b["id"], a["id"], "10.00")
    results = run_parallel(20, op)
    assert all(r.status_code == 201 for r in results), [r.text for r in results if r.status_code != 201]
    # 10 each way, money is conserved
    assert bal(client, customer, a["id"]) + bal(client, customer2, b["id"]) == Decimal("10000.00")
    assert bal(client, customer, a["id"]) == Decimal("5000.00")


# ---------------------------------------------------------------- idempotency under concurrency
def test_same_idempotency_key_in_parallel_creates_one_transfer(client, customer, customer2, funded, relaxed):
    a, b = funded
    key = "parallel-key-1"
    results = run_parallel(8, lambda c, i: transfer(c, customer, a["id"], b["id"], "250.00", key=key))
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    assert len({r.json()["id"] for r in results}) == 1                              # every caller got the same transfer
    assert sum(1 for r in results if "idempotent-replayed" not in r.headers) == 1   # exactly one real execution
    assert bal(client, customer, a["id"]) == Decimal("750.00")
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Transfer)) == 1


def test_same_idempotency_key_in_parallel_deposit(client, customer):
    w = create_wallet(client, customer)
    key = "parallel-dep"
    results = run_parallel(8, lambda c, i: c.post(f"/api/wallets/{w['id']}/deposits", json={"amount": "75.00"},
                                                   headers={**customer.h, "Idempotency-Key": key}))
    assert all(r.status_code == 201 for r in results)
    assert bal(client, customer, w["id"]) == Decimal("75.00")


def test_concurrent_refund_requests_create_a_single_refund(client, customer, customer2, funded, relaxed):
    a, b = funded
    assert transfer(client, customer, a["id"], b["id"], "500.00").status_code == 201
    tx = client.get("/api/transactions?type=transfer", headers=customer.h).json()["items"][0]["id"]
    results = run_parallel(6, lambda c, i: c.post(
        "/api/refunds", json={"transaction_id": tx, "reason": "duplicate attempt"},
        headers={**customer.h, "Idempotency-Key": uuid.uuid4().hex}))                # distinct keys
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * 5, [r.text for r in results]
    assert client.get("/api/refunds", headers=customer.h).json()["total"] == 1


def test_concurrent_refund_approvals_pay_out_once(client, customer, customer2, funded, relaxed, admin, support):
    a, b = funded
    transfer(client, customer, a["id"], b["id"], "500.00")
    tx = client.get("/api/transactions?type=transfer", headers=customer.h).json()["items"][0]["id"]
    rid = client.post("/api/refunds", json={"transaction_id": tx, "reason": "double approve"},
                      headers={**customer.h, "Idempotency-Key": uuid.uuid4().hex}).json()["id"]
    results = run_parallel(2, lambda c, i: c.post(f"/api/refunds/{rid}/approve",
                                                   headers=(admin if i == 0 else support).h))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert bal(client, customer, a["id"]) == Decimal("1000.00")
    assert bal(client, customer2, b["id"]) == Decimal("0.00")


def test_concurrent_review_approvals_settle_once(client, customer, customer2, admin, support, relaxed):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    assert transfer(client, customer, a["id"], b["id"], "2000.00").status_code == 202
    sid = client.get("/api/admin/suspicious-transactions", headers=support.h).json()["items"][0]["id"]
    results = run_parallel(4, lambda c, i: c.post(f"/api/admin/suspicious-transactions/{sid}/review",
                                                   json={"decision": "APPROVE"}, headers=support.h))
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409]
    assert bal(client, customer2, b["id"]) == Decimal("2000.00")
    assert bal(client, customer, a["id"]) == Decimal("8000.00")


def test_db_constraints_block_negative_balance_even_if_app_logic_is_bypassed(client, customer):
    """Last line of defence: CHECK constraints on wallet_balances."""
    w = create_wallet(client, customer)
    with SessionLocal() as db:
        with pytest.raises(Exception) as exc:
            db.execute(text("UPDATE wallet_balances SET available_balance = -1 WHERE wallet_id = :w"), {"w": w["id"]})
            db.commit()
        assert "available_non_negative" in str(exc.value)
    with SessionLocal() as db:
        with pytest.raises(Exception) as exc:
            db.execute(text("UPDATE wallet_balances SET pending_balance = 5 WHERE wallet_id = :w"), {"w": w["id"]})
            db.commit()
        assert "pending_within_available" in str(exc.value)

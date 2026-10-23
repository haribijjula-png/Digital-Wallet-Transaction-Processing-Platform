import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models import LedgerEntry
from tests.helpers import (bal, create_wallet, deposit, err_code, idem, make_customer, transfer, wallet, withdraw)


def completed_transfer(client, customer, a, b, amount="500.00"):
    r = transfer(client, customer, a["id"], b["id"], amount)
    assert r.status_code == 201, r.text
    txs = client.get("/api/transactions?type=transfer&status=completed", headers=customer.h).json()["items"]
    return txs[0]["id"]


def request_refund(client, actor, tx_id, amount=None, key=None, reason="Item not delivered"):
    body = {"transaction_id": tx_id, "reason": reason}
    if amount:
        body["amount"] = amount
    return client.post("/api/refunds", json=body, headers={**actor.h, "Idempotency-Key": key or uuid.uuid4().hex})


def test_refund_full_lifecycle_creates_ledger_entries(client, customer, customer2, funded, support, db):
    a, b = funded
    tx = completed_transfer(client, customer, a, b, "500.00")
    r = request_refund(client, customer, tx)
    assert r.status_code == 201 and r.json()["status"] == "REQUESTED" and r.json()["amount"] == "500.00"
    rid = r.json()["id"]

    assert client.post(f"/api/refunds/{rid}/start-review", headers=support.h).json()["status"] == "UNDER_REVIEW"
    ap = client.post(f"/api/refunds/{rid}/approve", json={"notes": "ok"}, headers=support.h)
    assert ap.status_code == 200 and ap.json()["status"] == "COMPLETED" and ap.json()["reviewed_by"] == support.id

    assert bal(client, customer, a["id"]) == Decimal("1000.00")      # got the 500 back
    assert bal(client, customer2, b["id"]) == Decimal("0.00")
    entries = db.scalars(select(LedgerEntry).where(LedgerEntry.transaction_type == "REFUND")).all()
    assert len(entries) == 2
    debit = next(e for e in entries if e.direction.value == "DEBIT")
    credit = next(e for e in entries if e.direction.value == "CREDIT")
    assert (str(debit.wallet_id), debit.balance_before, debit.balance_after) == (b["id"], Decimal("500.00"), Decimal("0.00"))
    assert (str(credit.wallet_id), credit.balance_before, credit.balance_after) == (a["id"], Decimal("500.00"), Decimal("1000.00"))
    types = [n["type"] for n in client.get("/api/notifications", headers=customer.h).json()["items"]]
    assert "REFUND_APPROVED" in types
    # the refund is itself a visible REFUND transaction
    assert client.get("/api/transactions?type=refund", headers=customer.h).json()["total"] == 1


def test_partial_refund(client, customer, customer2, funded, admin):
    a, b = funded
    tx = completed_transfer(client, customer, a, b, "500.00")
    rid = request_refund(client, customer, tx, amount="120.50").json()["id"]
    client.post(f"/api/refunds/{rid}/approve", headers=admin.h)
    assert bal(client, customer, a["id"]) == Decimal("620.50")
    assert bal(client, customer2, b["id"]) == Decimal("379.50")


def test_refund_exceeding_original_amount_rejected(client, customer, funded):
    a, b = funded
    tx = completed_transfer(client, customer, a, b, "500.00")
    r = request_refund(client, customer, tx, amount="500.01")
    assert r.status_code == 400 and err_code(r) == "INVALID_REFUND"


def test_duplicate_refund_rejected(client, customer, funded):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    assert request_refund(client, customer, tx).status_code == 201
    r = request_refund(client, customer, tx)                       # different idempotency key
    assert r.status_code == 409 and err_code(r) == "DUPLICATE_TRANSACTION"


def test_cannot_refund_twice_after_completion(client, customer, funded, admin):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    rid = request_refund(client, customer, tx).json()["id"]
    client.post(f"/api/refunds/{rid}/approve", headers=admin.h)
    r = request_refund(client, customer, tx)
    assert r.status_code == 409 and err_code(r) == "DUPLICATE_TRANSACTION"
    assert bal(client, customer, a["id"]) == Decimal("1000.00")


def test_rejected_refund_allows_new_request(client, customer, funded, admin):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    rid = request_refund(client, customer, tx).json()["id"]
    assert client.post(f"/api/refunds/{rid}/reject", json={"notes": "no"}, headers=admin.h).json()["status"] == "REJECTED"
    assert request_refund(client, customer, tx).status_code == 201


def test_refund_idempotent_replay(client, customer, funded, db):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    r1 = request_refund(client, customer, tx, key="refund-1")
    r2 = request_refund(client, customer, tx, key="refund-1")
    assert r1.status_code == r2.status_code == 201 and r1.json() == r2.json()
    assert r2.headers["idempotent-replayed"] == "true"
    assert client.get("/api/refunds", headers=customer.h).json()["total"] == 1


def test_refund_key_reuse_with_different_payload(client, customer, funded):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    request_refund(client, customer, tx, amount="10.00", key="rk")
    r = request_refund(client, customer, tx, amount="20.00", key="rk")
    assert r.status_code == 409 and err_code(r) == "DUPLICATE_IDEMPOTENCY_KEY"


def test_refund_requires_idempotency_key(client, customer, funded):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    r = client.post("/api/refunds", json={"transaction_id": tx, "reason": "abc"}, headers=customer.h)
    assert r.status_code == 400 and err_code(r) == "IDEMPOTENCY_KEY_REQUIRED"


# ---------------------------------------------------------------- invalid original transactions
def test_refund_unknown_transaction(client, customer):
    r = request_refund(client, customer, str(uuid.uuid4()))
    assert r.status_code == 404 and err_code(r) == "TRANSACTION_NOT_FOUND"


def test_refund_against_deposit_is_invalid(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    tx = client.get("/api/transactions?type=deposit", headers=customer.h).json()["items"][0]["id"]
    r = request_refund(client, customer, tx)
    assert r.status_code == 400 and err_code(r) == "INVALID_REFUND"


def test_refund_against_withdrawal_is_invalid(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "100.00")
    withdraw(client, customer, w["id"], "10.00")
    tx = client.get("/api/transactions?type=withdrawal", headers=customer.h).json()["items"][0]["id"]
    assert err_code(request_refund(client, customer, tx)) == "INVALID_REFUND"


def test_refund_against_failed_transfer_is_invalid(client, customer, funded):
    a, b = funded
    transfer(client, customer, a["id"], b["id"], "99999.00")                  # insufficient -> FAILED
    tx = client.get("/api/transactions?type=transfer&status=failed", headers=customer.h).json()["items"][0]["id"]
    r = request_refund(client, customer, tx)
    assert r.status_code == 400 and err_code(r) == "INVALID_REFUND"


def test_refund_against_pending_transfer_is_invalid(client, customer, customer2, admin):
    from tests.helpers import set_limits
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    transfer(client, customer, a["id"], b["id"], "2000.00")
    tx = client.get("/api/transactions?type=transfer&status=pending", headers=customer.h).json()["items"][0]["id"]
    assert err_code(request_refund(client, customer, tx)) == "INVALID_REFUND"


def test_only_original_sender_can_request_refund(client, customer, customer2, funded):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    r = request_refund(client, customer2, tx)                                  # the receiver
    assert r.status_code == 403
    stranger = make_customer(client, "eve@example.com")
    assert request_refund(client, stranger, tx).status_code == 403


def test_staff_cannot_request_refund(client, support, customer, funded):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    assert request_refund(client, support, tx).status_code == 403


# ---------------------------------------------------------------- closed wallets / balance
def test_refund_from_closed_wallet_rejected_at_request(client, customer, customer2, funded):
    a, b = funded
    tx = completed_transfer(client, customer, a, b, "500.00")
    withdraw(client, customer2, b["id"], "500.00")                              # empty the receiver so it can close
    assert client.patch(f"/api/wallets/{b['id']}", json={"status": "CLOSED"}, headers=customer2.h).status_code == 200
    r = request_refund(client, customer, tx)
    assert r.status_code == 409 and err_code(r) == "WALLET_CLOSED"


def test_refund_fails_if_wallet_closed_before_approval(client, customer, customer2, funded, admin):
    a, b = funded
    tx = completed_transfer(client, customer, a, b, "500.00")
    rid = request_refund(client, customer, tx).json()["id"]
    withdraw(client, customer2, b["id"], "500.00")
    client.patch(f"/api/wallets/{b['id']}", json={"status": "CLOSED"}, headers=customer2.h)
    r = client.post(f"/api/refunds/{rid}/approve", headers=admin.h)
    assert r.status_code == 200 and r.json()["status"] == "FAILED" and r.json()["failure_reason"] == "WALLET_CLOSED"
    assert bal(client, customer, a["id"]) == Decimal("500.00")


def test_refund_fails_when_receiver_has_spent_the_money(client, customer, customer2, funded, admin):
    a, b = funded
    tx = completed_transfer(client, customer, a, b, "500.00")
    rid = request_refund(client, customer, tx).json()["id"]
    withdraw(client, customer2, b["id"], "450.00")                              # receiver only has 50 left
    r = client.post(f"/api/refunds/{rid}/approve", headers=admin.h)
    assert r.json()["status"] == "FAILED" and r.json()["failure_reason"] == "INSUFFICIENT_BALANCE"
    assert bal(client, customer, a["id"]) == Decimal("500.00")                  # nothing moved
    assert bal(client, customer2, b["id"]) == Decimal("50.00")
    # a FAILED refund no longer blocks a fresh request
    assert request_refund(client, customer, tx, amount="50.00").status_code == 201


# ---------------------------------------------------------------- authorization / state machine
def test_customer_cannot_approve_or_reject(client, customer, funded):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    rid = request_refund(client, customer, tx).json()["id"]
    assert client.post(f"/api/refunds/{rid}/approve", headers=customer.h).status_code == 403
    assert client.post(f"/api/refunds/{rid}/reject", headers=customer.h).status_code == 403
    assert client.post(f"/api/refunds/{rid}/start-review", headers=customer.h).status_code == 403


def test_cannot_approve_twice_or_after_rejection(client, customer, customer2, funded, admin):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    rid = request_refund(client, customer, tx).json()["id"]
    client.post(f"/api/refunds/{rid}/approve", headers=admin.h)
    r = client.post(f"/api/refunds/{rid}/approve", headers=admin.h)
    assert r.status_code == 409 and err_code(r) == "INVALID_STATE_TRANSITION"
    assert bal(client, customer, a["id"]) == Decimal("1000.00")                 # not refunded twice
    assert client.post(f"/api/refunds/{rid}/reject", headers=admin.h).status_code == 409


def test_cannot_approve_rejected_refund(client, customer, funded, admin):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    rid = request_refund(client, customer, tx).json()["id"]
    client.post(f"/api/refunds/{rid}/reject", headers=admin.h)
    assert client.post(f"/api/refunds/{rid}/approve", headers=admin.h).status_code == 409
    assert bal(client, customer, a["id"]) == Decimal("500.00")


def test_refund_visibility(client, customer, customer2, funded, support):
    a, b = funded
    tx = completed_transfer(client, customer, a, b)
    rid = request_refund(client, customer, tx).json()["id"]
    assert client.get(f"/api/refunds/{rid}", headers=customer.h).status_code == 200
    assert client.get(f"/api/refunds/{rid}", headers=support.h).status_code == 200
    assert client.get(f"/api/refunds/{rid}", headers=customer2.h).status_code == 403
    assert client.get("/api/refunds", headers=customer2.h).json()["total"] == 0
    assert client.get("/api/refunds?status=requested", headers=support.h).json()["total"] == 1
    assert client.get(f"/api/refunds/{uuid.uuid4()}", headers=customer.h).status_code == 404

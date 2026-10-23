import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update

from app.core.security import utcnow
from app.models import AuditLog, Deposit, LedgerEntry, Notification, Transaction, Wallet
from tests.helpers import (PASSWORD, bal, create_wallet, deposit, err_code, idem, make_customer, make_staff,
                           set_limits, transfer, wallet, withdraw)


# ================================================================== transaction limits admin API
def test_get_limits_defaults(client, admin):
    r = client.get("/api/admin/transaction-limits", headers=admin.h)
    assert r.status_code == 200
    body = r.json()
    assert body["max_transaction_amount"] == "100000.00" and body["max_transactions_per_day"] == 50


def test_update_limits_is_audited_with_before_and_after(client, admin):
    r = client.patch("/api/admin/transaction-limits", headers=admin.h,
                     json={"daily_transfer_limit": "150000.00", "max_transactions_per_day": 40})
    assert r.status_code == 200 and r.json()["daily_transfer_limit"] == "150000.00"
    assert r.json()["updated_by"] == admin.id
    logs = client.get("/api/admin/audit-logs?action=TRANSACTION_LIMIT_CHANGED", headers=admin.h).json()
    assert logs["total"] == 1
    meta = logs["items"][0]["metadata"]
    assert meta["before"]["daily_transfer_limit"] == "200000.00" and meta["after"]["daily_transfer_limit"] == "150000.00"
    assert client.get("/api/admin/transaction-limits", headers=admin.h).json()["max_transactions_per_day"] == 40


def test_limits_rbac(client, customer, support, admin, super_admin):
    assert client.get("/api/admin/transaction-limits", headers=customer.h).status_code == 403
    assert client.get("/api/admin/transaction-limits", headers=support.h).status_code == 403
    assert client.patch("/api/admin/transaction-limits", json={"max_transactions_per_day": 5},
                        headers=support.h).status_code == 403
    assert client.get("/api/admin/transaction-limits").status_code == 401
    assert client.patch("/api/admin/transaction-limits", json={"max_transactions_per_day": 5},
                        headers=super_admin.h).status_code == 200


@pytest.mark.parametrize("payload", [{"max_transaction_amount": "0"}, {"daily_transfer_limit": "-1"},
                                     {"max_transactions_per_day": 0}, {"velocity_window_minutes": -3}])
def test_limits_validation(client, admin, payload):
    assert client.patch("/api/admin/transaction-limits", json=payload, headers=admin.h).status_code == 422


def test_limits_empty_patch_and_inconsistent_velocity(client, admin):
    assert err_code(client.patch("/api/admin/transaction-limits", json={}, headers=admin.h)) == "NO_CHANGES"
    r = client.patch("/api/admin/transaction-limits", headers=admin.h,
                     json={"velocity_review_count": 9, "velocity_block_count": 3})
    assert r.status_code == 400 and err_code(r) == "INVALID_LIMITS"
    assert client.get("/api/admin/transaction-limits", headers=admin.h).json()["velocity_review_count"] == 5


# ================================================================== user management
def test_admin_lists_and_filters_users(client, admin, customer, customer2):
    r = client.get("/api/admin/users?role=customer", headers=admin.h).json()
    assert r["total"] == 2
    assert client.get("/api/admin/users?search=alice", headers=admin.h).json()["total"] == 1
    assert client.get("/api/admin/users", headers=customer.h).status_code == 403


def test_super_admin_creates_staff_and_they_can_log_in(client, super_admin):
    r = client.post("/api/admin/users", headers=super_admin.h, json={
        "email": "agent@example.com", "full_name": "Agent Smith", "password": PASSWORD, "role": "SUPPORT_AGENT"})
    assert r.status_code == 201 and r.json()["role"] == "SUPPORT_AGENT"
    assert client.post("/api/auth/login", json={"email": "agent@example.com", "password": PASSWORD}).status_code == 200


def test_plain_admin_cannot_create_staff_or_change_roles(client, admin, customer):
    assert client.post("/api/admin/users", headers=admin.h, json={
        "email": "x@example.com", "full_name": "X Y", "password": PASSWORD, "role": "ADMIN"}).status_code == 403
    assert client.patch(f"/api/admin/users/{customer.id}", json={"role": "ADMIN"}, headers=admin.h).status_code == 403


def test_admin_cannot_touch_other_admins_or_deactivate_self(client, admin, super_admin):
    assert client.patch(f"/api/admin/users/{super_admin.id}", json={"is_active": False}, headers=admin.h).status_code == 403
    assert client.patch(f"/api/admin/users/{admin.id}", json={"is_active": False}, headers=admin.h).status_code == 403


def test_reactivate_user(client, admin, customer):
    client.patch(f"/api/admin/users/{customer.id}", json={"is_active": False}, headers=admin.h)
    client.patch(f"/api/admin/users/{customer.id}", json={"is_active": True}, headers=admin.h)
    assert client.post("/api/auth/login", json={"email": customer.email, "password": PASSWORD}).status_code == 200


def test_user_with_wallets_cannot_become_staff(client, super_admin, customer):
    create_wallet(client, customer)
    r = client.patch(f"/api/admin/users/{customer.id}", json={"role": "SUPPORT_AGENT"}, headers=super_admin.h)
    assert r.status_code == 409 and err_code(r) == "USER_HAS_WALLETS"


def test_get_unknown_user_404(client, admin):
    assert client.get(f"/api/admin/users/{uuid.uuid4()}", headers=admin.h).status_code == 404


# ================================================================== manual adjustments
def test_super_admin_adjustment_creates_ledger_entry(client, super_admin, customer, db):
    w = create_wallet(client, customer)
    r = client.post(f"/api/admin/wallets/{w['id']}/adjustments", headers=super_admin.h,
                    json={"amount": "250.00", "direction": "CREDIT", "reason": "Goodwill credit"})
    assert r.status_code == 201 and r.json()["transaction_type"] == "ADJUSTMENT"
    assert bal(client, customer, w["id"]) == Decimal("250.00")
    r = client.post(f"/api/admin/wallets/{w['id']}/adjustments", headers=super_admin.h,
                    json={"amount": "50.00", "direction": "DEBIT", "reason": "Correction"})
    assert r.status_code == 201 and bal(client, customer, w["id"]) == Decimal("200.00")
    over = client.post(f"/api/admin/wallets/{w['id']}/adjustments", headers=super_admin.h,
                       json={"amount": "500.00", "direction": "DEBIT", "reason": "Too much"})
    assert over.status_code == 400 and err_code(over) == "INSUFFICIENT_BALANCE"
    assert bal(client, customer, w["id"]) == Decimal("200.00")


def test_adjustment_rbac(client, admin, customer):
    w = create_wallet(client, customer)
    assert client.post(f"/api/admin/wallets/{w['id']}/adjustments", headers=admin.h,
                       json={"amount": "1.00", "direction": "CREDIT", "reason": "nope"}).status_code == 403
    assert client.post(f"/api/admin/wallets/{w['id']}/adjustments", headers=customer.h,
                       json={"amount": "1.00", "direction": "CREDIT", "reason": "nope"}).status_code == 403


# ================================================================== immutability
def test_ledger_cannot_be_updated_or_deleted_via_sql(client, customer, db):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10.00")
    for stmt in ("UPDATE transaction_ledger SET amount = 999", "DELETE FROM transaction_ledger"):
        with pytest.raises(Exception) as exc:
            db.execute(text(stmt))
            db.commit()
        assert "append-only" in str(exc.value)
        db.rollback()
    assert db.scalar(select(LedgerEntry.amount)) == Decimal("10.00")


def test_audit_log_cannot_be_updated_or_deleted_via_sql(client, customer, db):
    for stmt in ("UPDATE audit_logs SET action = 'HACKED'", "DELETE FROM audit_logs"):
        with pytest.raises(Exception) as exc:
            db.execute(text(stmt))
            db.commit()
        assert "append-only" in str(exc.value)
        db.rollback()


def test_orm_blocks_ledger_and_audit_mutation(client, customer, db):
    from app.core.exceptions import ImmutableRecordError
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10.00")
    entry = db.scalar(select(LedgerEntry))
    entry.amount = Decimal("1.00")
    with pytest.raises(ImmutableRecordError):
        db.flush()
    db.rollback()
    log = db.scalar(select(AuditLog))
    db.delete(log)
    with pytest.raises(ImmutableRecordError):
        db.flush()
    db.rollback()


def test_no_api_endpoint_can_mutate_ledger_or_audit(client, admin):
    for method in ("put", "patch", "delete"):
        for path in ("/api/admin/audit-logs", f"/api/admin/audit-logs/{uuid.uuid4()}", "/api/ledger"):
            assert getattr(client, method)(path, headers=admin.h).status_code in (404, 405)


def test_ledger_invariant_constraints(client, customer, db):
    w = create_wallet(client, customer)
    with pytest.raises(Exception) as exc:      # balance arithmetic must be consistent
        db.execute(text("""INSERT INTO transaction_ledger (id, wallet_id, transaction_type, direction, amount,
                           balance_before, balance_after) VALUES (gen_random_uuid(), :w, 'DEPOSIT', 'CREDIT', 10, 0, 99)"""),
                   {"w": w["id"]})
        db.commit()
    assert "balance_arithmetic" in str(exc.value)


# ================================================================== audit log coverage
def test_audit_trail_covers_required_events(client, customer, customer2, admin, support):
    client.post("/api/auth/login", json={"email": customer.email, "password": "Wrong12345"})        # failed login
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "5000.00")
    transfer(client, customer, a["id"], b["id"], "100.00")                                           # created + completed
    transfer(client, customer, a["id"], b["id"], "9999999.00")                                       # failed
    withdraw(client, customer, a["id"], "50.00")
    client.post(f"/api/wallets/{a['id']}/freeze", headers=support.h)
    client.post(f"/api/wallets/{a['id']}/unfreeze", headers=support.h)
    set_limits(client, admin, max_transactions_per_day=40)
    set_limits(client, admin, max_transaction_amount="500.00")
    transfer(client, customer, a["id"], b["id"], "800.00")                                           # blocked
    actions = {l["action"] for l in client.get("/api/admin/audit-logs?page_size=100", headers=admin.h).json()["items"]}
    required = {"LOGIN", "LOGIN_FAILED", "WALLET_CREATED", "WALLET_FROZEN", "WALLET_UNFROZEN", "DEPOSIT_CREATED",
                "TRANSFER_CREATED", "TRANSFER_COMPLETED", "TRANSFER_FAILED", "WITHDRAWAL_CREATED",
                "TRANSACTION_BLOCKED", "TRANSACTION_LIMIT_CHANGED"}
    assert required <= actions, required - actions


def test_audit_entries_store_user_entity_ip_metadata(client, customer):
    create_wallet(client, customer)
    from app.core.database import SessionLocal
    with SessionLocal() as db:
        log = db.scalar(select(AuditLog).where(AuditLog.action == "WALLET_CREATED"))
        assert str(log.user_id) == customer.id and log.entity == "wallet" and log.entity_id
        assert log.ip_address and log.created_at and log.meta["currency"] == "INR"


def test_audit_log_access_restricted_to_admins(client, customer, support, admin):
    assert client.get("/api/admin/audit-logs", headers=customer.h).status_code == 403
    assert client.get("/api/admin/audit-logs", headers=support.h).status_code == 403
    assert client.get("/api/admin/audit-logs", headers=admin.h).status_code == 200


def test_audit_log_filters(client, customer, admin):
    create_wallet(client, customer)
    r = client.get(f"/api/admin/audit-logs?user_id={customer.id}&action=WALLET_CREATED&entity=wallet", headers=admin.h)
    assert r.json()["total"] == 1


# ================================================================== notifications
def test_notifications_list_and_mark_read(client, customer, customer2, funded):
    a, b = funded
    transfer(client, customer, a["id"], b["id"], "10.00")
    items = client.get("/api/notifications", headers=customer.h).json()
    assert items["total"] >= 2 and all(n["is_read"] is False for n in items["items"])
    nid = items["items"][0]["id"]
    r = client.patch(f"/api/notifications/{nid}/read", headers=customer.h)
    assert r.status_code == 200 and r.json()["is_read"] is True and r.json()["read_at"]
    unread = client.get("/api/notifications?unread_only=true", headers=customer.h).json()
    assert unread["total"] == items["total"] - 1


def test_read_all(client, customer, customer2, funded):
    a, b = funded
    transfer(client, customer, a["id"], b["id"], "10.00")
    r = client.patch("/api/notifications/read-all", headers=customer.h)
    assert r.status_code == 200 and "marked as read" in r.json()["message"]
    assert client.get("/api/notifications?unread_only=true", headers=customer.h).json()["total"] == 0
    # the other user's notifications are untouched
    assert client.get("/api/notifications?unread_only=true", headers=customer2.h).json()["total"] >= 1


def test_cannot_read_someone_elses_notification(client, customer, customer2, funded):
    a, b = funded
    transfer(client, customer, a["id"], b["id"], "10.00")
    nid = client.get("/api/notifications", headers=customer2.h).json()["items"][0]["id"]
    assert client.patch(f"/api/notifications/{nid}/read", headers=customer.h).status_code == 403
    assert client.patch(f"/api/notifications/{uuid.uuid4()}/read", headers=customer.h).status_code == 404


def test_notifications_are_delivered_by_background_task(client, customer, db):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10.00")
    pending = db.scalars(select(Notification).where(Notification.delivered_at.is_(None))).all()
    assert pending == []        # BackgroundTask ran after the response


def test_all_required_notification_types_are_generated(client, customer, customer2, admin, support):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "50000.00")
    deposit(client, customer, a["id"], "10.00", simulate_outcome="FAILURE")
    transfer(client, customer, a["id"], b["id"], "10.00")
    transfer(client, customer, a["id"], b["id"], "999999.00")
    withdraw(client, customer, a["id"], "10.00")
    withdraw(client, customer, a["id"], "999999.00")
    client.post(f"/api/wallets/{a['id']}/freeze", headers=support.h)
    client.post(f"/api/wallets/{a['id']}/unfreeze", headers=support.h)
    tx = client.get("/api/transactions?type=transfer&status=completed", headers=customer.h).json()["items"][0]["id"]
    rid = client.post("/api/refunds", json={"transaction_id": tx, "reason": "test"},
                      headers={**customer.h, **idem()}).json()["id"]
    client.post(f"/api/refunds/{rid}/approve", headers=admin.h)
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    transfer(client, customer, a["id"], b["id"], "2000.00")
    types = set()
    for u in (customer, customer2, support):
        types |= {n["type"] for n in client.get("/api/notifications?page_size=100", headers=u.h).json()["items"]}
    required = {"DEPOSIT_COMPLETED", "DEPOSIT_FAILED", "TRANSFER_RECEIVED", "TRANSFER_COMPLETED", "TRANSFER_FAILED",
                "WITHDRAWAL_COMPLETED", "WITHDRAWAL_FAILED", "WALLET_FROZEN", "WALLET_UNFROZEN", "REFUND_APPROVED",
                "SUSPICIOUS_TRANSACTION"}
    assert required <= types, required - types


# ================================================================== transaction history
@pytest.fixture
def history(client, customer, customer2, funded, admin):
    a, b = funded                                    # 1000 deposit already made
    deposit(client, customer, a["id"], "250.00")
    transfer(client, customer, a["id"], b["id"], "100.00", reference="coffee money")
    transfer(client, customer, a["id"], b["id"], "300.00", reference="rent")
    withdraw(client, customer, a["id"], "50.00")
    transfer(client, customer, a["id"], b["id"], "99999.00")           # FAILED
    return a, b


def test_history_pagination(client, customer, history):
    p1 = client.get("/api/transactions?page=1&page_size=2", headers=customer.h).json()
    assert p1["total"] == 6 and p1["pages"] == 3 and len(p1["items"]) == 2 and p1["page"] == 1
    p3 = client.get("/api/transactions?page=3&page_size=2", headers=customer.h).json()
    assert len(p3["items"]) == 2
    p4 = client.get("/api/transactions?page=4&page_size=2", headers=customer.h).json()
    assert p4["items"] == []
    ids = {i["id"] for p in (1, 2, 3) for i in client.get(f"/api/transactions?page={p}&page_size=2",
                                                           headers=customer.h).json()["items"]}
    assert len(ids) == 6                                                  # stable, no duplicates across pages


def test_history_page_size_bounds(client, customer):
    assert client.get("/api/transactions?page_size=101", headers=customer.h).status_code == 422
    assert client.get("/api/transactions?page=0", headers=customer.h).status_code == 422


def test_history_filter_type_and_status_case_insensitive(client, customer, history):
    assert client.get("/api/transactions?type=transfer", headers=customer.h).json()["total"] == 3
    assert client.get("/api/transactions?type=TRANSFER", headers=customer.h).json()["total"] == 3
    assert client.get("/api/transactions?type=deposit", headers=customer.h).json()["total"] == 2
    assert client.get("/api/transactions?type=withdrawal", headers=customer.h).json()["total"] == 1
    assert client.get("/api/transactions?status=completed", headers=customer.h).json()["total"] == 5
    assert client.get("/api/transactions?status=failed", headers=customer.h).json()["total"] == 1
    assert client.get("/api/transactions?type=transfer&status=completed", headers=customer.h).json()["total"] == 2
    assert client.get("/api/transactions?type=nonsense", headers=customer.h).status_code == 422


def test_history_amount_range(client, customer, history):
    amounts = lambda q: sorted(Decimal(i["amount"]) for i in client.get(f"/api/transactions?{q}", headers=customer.h).json()["items"])
    assert amounts("min_amount=300") == [Decimal("300.00"), Decimal("1000.00"), Decimal("99999.00")]
    assert amounts("max_amount=100") == [Decimal("50.00"), Decimal("100.00")]
    assert amounts("min_amount=100&max_amount=300") == [Decimal("100.00"), Decimal("250.00"), Decimal("300.00")]


def test_history_date_range(client, customer, history):
    today = utcnow().date()
    tomorrow, yesterday = today + timedelta(days=1), today - timedelta(days=1)
    assert client.get(f"/api/transactions?from_date={today}&to_date={today}", headers=customer.h).json()["total"] == 6
    assert client.get(f"/api/transactions?from_date={tomorrow}", headers=customer.h).json()["total"] == 0
    assert client.get(f"/api/transactions?to_date={yesterday}", headers=customer.h).json()["total"] == 0
    assert client.get("/api/transactions?from_date=not-a-date", headers=customer.h).status_code == 422


def test_history_sorting(client, customer, history):
    asc = [Decimal(i["amount"]) for i in client.get("/api/transactions?sort_by=amount&order=asc", headers=customer.h).json()["items"]]
    desc = [Decimal(i["amount"]) for i in client.get("/api/transactions?sort_by=amount&order=desc", headers=customer.h).json()["items"]]
    assert asc == sorted(asc) and desc == sorted(desc, reverse=True) and asc[0] == Decimal("50.00")
    assert client.get("/api/transactions?sort_by=password", headers=customer.h).status_code == 422
    assert client.get("/api/transactions?order=sideways", headers=customer.h).status_code == 422


def test_history_search_by_reference_number(client, customer, history):
    ref = client.get("/api/transactions?type=withdrawal", headers=customer.h).json()["items"][0]["reference_number"]
    found = client.get(f"/api/transactions?search={ref[:8]}", headers=customer.h).json()
    assert found["total"] == 1 and found["items"][0]["reference_number"] == ref
    assert client.get("/api/transactions?search=rent", headers=customer.h).json()["total"] == 1
    assert client.get("/api/transactions?search=zzzz", headers=customer.h).json()["total"] == 0


def test_history_is_scoped_to_the_customer(client, customer, customer2, history, support):
    assert client.get("/api/transactions", headers=customer2.h).json()["total"] == 3    # only the 3 transfers received
    stranger = make_customer(client, "eve@example.com")
    assert client.get("/api/transactions", headers=stranger.h).json()["total"] == 0
    assert client.get("/api/transactions", headers=support.h).json()["total"] == 6 + 0
    a, b = history
    assert client.get(f"/api/transactions?wallet_id={a['id']}", headers=stranger.h).status_code == 403


def test_transaction_detail_and_ledger_access(client, customer, customer2, history):
    tx = client.get("/api/transactions?type=transfer&status=completed", headers=customer.h).json()["items"][0]
    assert client.get(f"/api/transactions/{tx['id']}", headers=customer.h).status_code == 200
    assert client.get(f"/api/transactions/{tx['id']}", headers=customer2.h).status_code == 200     # receiver
    stranger = make_customer(client, "eve@example.com")
    assert client.get(f"/api/transactions/{tx['id']}", headers=stranger.h).status_code == 403
    ledger = client.get(f"/api/transactions/{tx['id']}/ledger", headers=customer.h).json()
    assert {e["transaction_type"] for e in ledger} == {"TRANSFER_DEBIT", "TRANSFER_CREDIT"}
    assert client.get(f"/api/transactions/{uuid.uuid4()}", headers=customer.h).status_code == 404


# ================================================================== dashboards
def test_customer_dashboard(client, customer, customer2, history):
    d = client.get("/api/dashboard/customer", headers=customer.h).json()
    assert d["total_balance"] == {"INR": "800.00"}                       # 1250 - 100 - 300 - 50
    assert d["wallet_balances"][0]["available_balance"] == "800.00"
    assert d["total_deposits"] == {"INR": "1250.00"}
    assert d["total_transfers"] == {"INR": "400.00"}
    assert d["total_withdrawals"] == {"INR": "50.00"}
    assert d["pending_transactions"] == 0 and len(d["recent_transactions"]) == 6


def test_customer_dashboard_counts_pending(client, customer, customer2, admin):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    transfer(client, customer, a["id"], b["id"], "2000.00")
    d = client.get("/api/dashboard/customer", headers=customer.h).json()
    assert d["pending_transactions"] == 1
    assert d["wallet_balances"][0]["pending_balance"] == "2000.00"


def test_admin_dashboard(client, admin, customer, customer2, history):
    d = client.get("/api/dashboard/admin", headers=admin.h).json()
    assert d["total_users"] == 3 and d["active_wallets"] == 2
    # 1250 deposited - 50 withdrawn = 1200 in the system (the 400 transferred stays inside: 800 + 400)
    assert d["total_wallet_balance"] == {"INR": "1200.00"}
    assert d["todays_transaction_volume"] == {"INR": "1700.00"}          # 1250 + 400 + 50 (completed only)
    assert d["todays_transactions"] == 6
    assert d["failed_transactions"] == 1


def test_support_dashboard(client, support, customer, customer2, admin):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    transfer(client, customer, a["id"], b["id"], "2000.00")
    client.post(f"/api/wallets/{b['id']}/freeze", headers=support.h)
    d = client.get("/api/dashboard/support", headers=support.h).json()
    assert d["pending_reviews"] == 1 and d["suspicious_transactions"] == 1
    assert d["frozen_wallets"] == 1 and d["refund_requests"] == 0 and d["failed_transactions"] == 0


def test_dashboard_rbac(client, customer, support, admin):
    assert client.get("/api/dashboard/admin", headers=customer.h).status_code == 403
    assert client.get("/api/dashboard/admin", headers=support.h).status_code == 403
    assert client.get("/api/dashboard/support", headers=customer.h).status_code == 403
    assert client.get("/api/dashboard/support", headers=admin.h).status_code == 200
    assert client.get("/api/dashboard/customer", headers=admin.h).status_code == 403
    assert client.get("/api/dashboard/customer").status_code == 401


# ================================================================== error handling
def test_error_envelope_shape_for_every_status(client, customer):
    cases = {
        400: client.post("/api/wallets/%s/deposits" % uuid.uuid4(), json={"amount": "1.00"}, headers=customer.h),
        401: client.get("/api/auth/me"),
        403: client.get("/api/admin/transaction-limits", headers=customer.h),
        404: client.get(f"/api/wallets/{uuid.uuid4()}", headers=customer.h),
        422: client.post("/api/wallets", json={}, headers=customer.h),
    }
    for status, resp in cases.items():
        assert resp.status_code == status, (status, resp.text)
        body = resp.json()
        assert body["success"] is False and set(body["error"]) >= {"code", "message"}


def test_409_envelope(client, customer):
    w = create_wallet(client, customer)
    client.post(f"/api/wallets/{w['id']}/freeze", headers=customer.h)
    r = client.post(f"/api/wallets/{w['id']}/freeze", headers=customer.h)
    assert r.status_code == 409 and r.json() == {"success": False, "error": {
        "code": "INVALID_STATE_TRANSITION", "message": "Invalid wallet status transition: FROZEN -> FROZEN."}}


def test_validation_error_lists_fields(client, customer):
    r = client.post("/api/wallets", json={"currency": "INR", "name": ""}, headers=customer.h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert any(d["field"].endswith("name") for d in r.json()["error"]["details"])


def test_unknown_route_and_method_use_envelope(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = client.delete("/api/wallets")
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_unhandled_exception_returns_500_envelope(client, customer, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.wallets import WalletService
    monkeypatch.setattr(WalletService, "list", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/wallets", headers=customer.h)
    assert r.status_code == 500
    assert r.json() == {"success": False, "error": {"code": "INTERNAL_SERVER_ERROR",
                                                    "message": "An unexpected error occurred."}}      # no stack-trace leak


def test_health_and_openapi(client):
    assert client.get("/health").json() == {"status": "ok"}
    spec = client.get("/openapi.json").json()
    assert "/api/transfers" in spec["paths"] and "/api/auth/refresh" in spec["paths"]
    assert client.get("/docs").status_code == 200


# ================================================================== scheduled jobs
def run(client, admin, name):
    r = client.post(f"/api/admin/jobs/{name}/run", headers=admin.h)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_job_endpoint_rbac(client, customer, support, admin):
    assert client.post("/api/admin/jobs/pending_cleanup/run", headers=customer.h).status_code == 403
    assert client.post("/api/admin/jobs/pending_cleanup/run", headers=support.h).status_code == 403
    assert client.post("/api/admin/jobs/nope/run", headers=admin.h).status_code == 404


def test_pending_cleanup_cancels_stale_held_transfers_and_releases_funds(client, customer, customer2, admin, db):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "10000.00")
    set_limits(client, admin, suspicious_amount_threshold="1000.00")
    t = transfer(client, customer, a["id"], b["id"], "2000.00").json()
    assert run(client, admin, "pending_cleanup")["held_transfers_cancelled"] == 0          # too fresh
    db.execute(text("UPDATE transactions SET created_at = now() - interval '5 days' WHERE status = 'PENDING'"))
    db.commit()
    assert run(client, admin, "pending_cleanup")["held_transfers_cancelled"] == 1
    assert client.get(f"/api/transfers/{t['id']}", headers=customer.h).json()["status"] == "CANCELLED"
    snap = wallet(client, customer, a["id"])
    assert snap["pending_balance"] == "0.00" and snap["available_balance"] == "10000.00"


def test_pending_cleanup_cancels_stale_pending_deposits(client, customer, db, admin):
    w = create_wallet(client, customer)
    d = deposit(client, customer, w["id"], "100.00")
    # fabricate a deposit stuck in PENDING (e.g. gateway never called back)
    db.execute(text("UPDATE deposits SET status='PENDING', completed_at=NULL, created_at = now() - interval '2 hours'"))
    db.execute(text("UPDATE transactions SET status='PENDING', completed_at=NULL WHERE type='DEPOSIT'"))
    db.commit()
    assert run(client, admin, "pending_cleanup")["deposits_cancelled"] == 1
    assert client.get(f"/api/deposits/{d['id']}", headers=customer.h).json()["status"] == "CANCELLED"


def test_pending_cleanup_purges_old_idempotency_keys(client, customer, db, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10.00")
    db.execute(text("UPDATE idempotency_keys SET created_at = now() - interval '3 days'"))
    db.commit()
    assert run(client, admin, "pending_cleanup")["idempotency_keys_deleted"] == 1


def test_failed_transaction_retry_recovers_gateway_timeouts(client, customer, admin):
    w = create_wallet(client, customer)
    d = deposit(client, customer, w["id"], "300.00", simulate_outcome="TIMEOUT")
    assert d["status"] == "FAILED" and d["failure_reason"] == "GATEWAY_TIMEOUT" and bal(client, customer, w["id"]) == 0
    out = run(client, admin, "failed_transaction_retry")
    assert out["deposits_retried"] == 1 and out["deposits_succeeded"] == 1
    assert client.get(f"/api/deposits/{d['id']}", headers=customer.h).json()["status"] == "COMPLETED"
    assert bal(client, customer, w["id"]) == Decimal("300.00")
    assert run(client, admin, "failed_transaction_retry")["deposits_retried"] == 0          # not retried again


def test_retry_skips_permanent_failures(client, customer, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "300.00", simulate_outcome="FAILURE")
    assert run(client, admin, "failed_transaction_retry")["deposits_retried"] == 0
    assert bal(client, customer, w["id"]) == 0


def test_retry_withdrawals(client, customer, admin):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "500.00")
    r = withdraw(client, customer, w["id"], "200.00", simulate_outcome="TIMEOUT")
    assert r.json()["status"] == "FAILED" and bal(client, customer, w["id"]) == Decimal("500.00")
    assert run(client, admin, "failed_transaction_retry")["withdrawals_succeeded"] == 1
    assert bal(client, customer, w["id"]) == Decimal("300.00")


def test_retry_is_bounded(client, customer, admin, db):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10.00", simulate_outcome="TIMEOUT")
    db.execute(text("UPDATE deposits SET retry_count = 3"))
    db.commit()
    assert run(client, admin, "failed_transaction_retry")["deposits_retried"] == 0


def test_suspicious_scan_flags_burst_activity(client, customer, customer2, admin, support):
    a = create_wallet(client, customer); b = create_wallet(client, customer2)
    deposit(client, customer, a["id"], "1000.00")
    set_limits(client, admin, velocity_review_count=1000, velocity_block_count=2000, frequency_review_threshold=1000)
    for _ in range(4):
        assert transfer(client, customer, a["id"], b["id"], "10.00").status_code == 201
    # tighten the rule AFTER the fact; the scheduled scan catches the burst retroactively
    set_limits(client, admin, velocity_review_count=3, velocity_block_count=100)
    assert run(client, admin, "suspicious_scan")["flagged"] >= 1
    sus = client.get("/api/admin/suspicious-transactions", headers=support.h).json()
    assert sus["total"] >= 1 and sus["items"][0]["reasons"] == ["SCAN_VELOCITY"]
    assert run(client, admin, "suspicious_scan")["flagged"] == 0                              # idempotent
    # reviewing a post-hoc flag just acknowledges it; nothing is moved
    before = bal(client, customer, a["id"])
    r = client.post(f"/api/admin/suspicious-transactions/{sus['items'][0]['id']}/review",
                    json={"decision": "APPROVE"}, headers=support.h)
    assert r.status_code == 200 and bal(client, customer, a["id"]) == before


def test_daily_wallet_summary_and_report(client, customer, customer2, admin, funded, db):
    a, b = funded
    transfer(client, customer, a["id"], b["id"], "100.00")
    # move today's activity to "yesterday" (bypassing the immutability trigger only inside this test)
    db.execute(text("ALTER TABLE transaction_ledger DISABLE TRIGGER trg_transaction_ledger_immutable"))
    db.execute(text("UPDATE transaction_ledger SET created_at = created_at - interval '1 day'"))
    db.execute(text("ALTER TABLE transaction_ledger ENABLE TRIGGER trg_transaction_ledger_immutable"))
    db.execute(text("UPDATE transactions SET created_at = created_at - interval '1 day'"))
    db.commit()
    out = run(client, admin, "daily_wallet_summary")
    assert out["summaries_created"] == 2
    summary = [n for n in client.get("/api/notifications", headers=customer.h).json()["items"] if n["type"] == "DAILY_SUMMARY"]
    assert len(summary) == 1 and "closing balance INR 900.00" in summary[0]["message"]
    assert run(client, admin, "daily_wallet_summary")["summaries_created"] == 0               # idempotent per day
    rep = run(client, admin, "daily_transaction_report")
    assert rep["total"] == 2 and rep["volume"]["INR"] == "1100.00" and rep["by_type"] == {"DEPOSIT": 1, "TRANSFER": 1}
    assert client.get("/api/admin/audit-logs?action=DAILY_TRANSACTION_REPORT", headers=admin.h).json()["total"] == 1


def test_daily_limit_reset_window_rolls_over_naturally(client, customer, customer2, funded, admin, db):
    a, b = funded
    set_limits(client, admin, daily_transfer_limit="500.00")
    assert transfer(client, customer, a["id"], b["id"], "500.00").status_code == 201
    assert err_code(transfer(client, customer, a["id"], b["id"], "1.00")) == "TRANSACTION_LIMIT_EXCEEDED"
    # "tomorrow": yesterday's usage no longer counts
    db.execute(text("UPDATE transactions SET created_at = created_at - interval '1 day'"))
    db.commit()
    assert transfer(client, customer, a["id"], b["id"], "500.00").status_code == 201
    assert "wallets_rolled_over" in run(client, admin, "daily_limit_reset")

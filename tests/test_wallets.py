import pytest

from tests.helpers import bal, create_wallet, deposit, err_code, wallet


def test_create_wallet_defaults(client, customer):
    w = create_wallet(client, customer, "INR", "Savings")
    assert w["status"] == "ACTIVE" and w["currency"] == "INR" and w["name"] == "Savings"
    assert w["available_balance"] == "0.00" and w["pending_balance"] == "0.00"
    assert w["user_id"] == customer.id


def test_customer_can_have_multiple_wallets(client, customer):
    create_wallet(client, customer, "INR")
    create_wallet(client, customer, "USD")
    r = client.get("/api/wallets", headers=customer.h)
    assert r.json()["total"] == 2


def test_unsupported_currency(client, customer):
    r = client.post("/api/wallets", json={"currency": "XYZ"}, headers=customer.h)
    assert r.status_code == 400 and err_code(r) == "UNSUPPORTED_CURRENCY"


def test_invalid_currency_format(client, customer):
    assert client.post("/api/wallets", json={"currency": "RUPEE"}, headers=customer.h).status_code == 422


def test_staff_cannot_create_wallets(client, admin):
    r = client.post("/api/wallets", json={"currency": "INR"}, headers=admin.h)
    assert r.status_code == 403 and err_code(r) == "ROLE_NOT_ALLOWED"


def test_unauthenticated_cannot_create_wallet(client):
    assert client.post("/api/wallets", json={"currency": "INR"}).status_code == 401


# ---------------------------------------------------------------- ownership authorization
def test_list_only_returns_own_wallets(client, customer, customer2):
    create_wallet(client, customer)
    create_wallet(client, customer2)
    r = client.get("/api/wallets", headers=customer.h).json()
    assert r["total"] == 1 and r["items"][0]["user_id"] == customer.id


def test_cannot_read_other_users_wallet(client, customer, customer2):
    w = create_wallet(client, customer)
    r = client.get(f"/api/wallets/{w['id']}", headers=customer2.h)
    assert r.status_code == 403 and err_code(r) == "WALLET_ACCESS_DENIED"


def test_cannot_modify_other_users_wallet(client, customer, customer2):
    w = create_wallet(client, customer)
    assert client.patch(f"/api/wallets/{w['id']}", json={"name": "x"}, headers=customer2.h).status_code == 403
    assert client.post(f"/api/wallets/{w['id']}/freeze", headers=customer2.h).status_code == 403


@pytest.mark.parametrize("who", ["support", "admin", "super_admin"])
def test_staff_can_view_any_wallet(client, customer, request, who):
    staff = request.getfixturevalue(who)
    w = create_wallet(client, customer)
    assert client.get(f"/api/wallets/{w['id']}", headers=staff.h).status_code == 200
    assert client.get("/api/wallets", headers=staff.h).json()["total"] == 1


def test_missing_wallet_404(client, customer):
    r = client.get("/api/wallets/00000000-0000-0000-0000-000000000000", headers=customer.h)
    assert r.status_code == 404 and err_code(r) == "WALLET_NOT_FOUND"


def test_invalid_uuid_422(client, customer):
    assert client.get("/api/wallets/not-a-uuid", headers=customer.h).status_code == 422


def test_rename_wallet(client, customer):
    w = create_wallet(client, customer)
    r = client.patch(f"/api/wallets/{w['id']}", json={"name": "Holiday fund"}, headers=customer.h)
    assert r.status_code == 200 and r.json()["name"] == "Holiday fund"


def test_support_cannot_rename(client, customer, support):
    w = create_wallet(client, customer)
    assert client.patch(f"/api/wallets/{w['id']}", json={"name": "x"}, headers=support.h).status_code == 403


# ---------------------------------------------------------------- freeze / unfreeze state machine
def test_owner_can_freeze_but_not_unfreeze(client, customer):
    w = create_wallet(client, customer)
    r = client.post(f"/api/wallets/{w['id']}/freeze", headers=customer.h)
    assert r.status_code == 200 and r.json()["status"] == "FROZEN"
    r = client.post(f"/api/wallets/{w['id']}/unfreeze", headers=customer.h)
    assert r.status_code == 403


def test_support_can_freeze_and_unfreeze(client, customer, support):
    w = create_wallet(client, customer)
    assert client.post(f"/api/wallets/{w['id']}/freeze", headers=support.h).json()["status"] == "FROZEN"
    assert client.post(f"/api/wallets/{w['id']}/unfreeze", headers=support.h).json()["status"] == "ACTIVE"


def test_freeze_already_frozen_wallet_is_invalid(client, customer):
    w = create_wallet(client, customer)
    client.post(f"/api/wallets/{w['id']}/freeze", headers=customer.h)
    r = client.post(f"/api/wallets/{w['id']}/freeze", headers=customer.h)
    assert r.status_code == 409 and err_code(r) == "INVALID_STATE_TRANSITION"


def test_unfreeze_active_wallet_is_invalid(client, customer, admin):
    w = create_wallet(client, customer)
    r = client.post(f"/api/wallets/{w['id']}/unfreeze", headers=admin.h)
    assert r.status_code == 409 and err_code(r) == "INVALID_STATE_TRANSITION"


def test_closed_wallet_is_terminal(client, customer, admin):
    w = create_wallet(client, customer)
    assert client.patch(f"/api/wallets/{w['id']}", json={"status": "CLOSED"}, headers=customer.h).status_code == 200
    for status in ("ACTIVE", "FROZEN", "SUSPENDED"):
        r = client.patch(f"/api/wallets/{w['id']}", json={"status": status}, headers=admin.h)
        assert r.status_code == 409 and err_code(r) == "INVALID_STATE_TRANSITION"
    assert client.post(f"/api/wallets/{w['id']}/freeze", headers=admin.h).status_code == 409


def test_cannot_close_wallet_with_balance(client, customer):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "10.00")
    r = client.patch(f"/api/wallets/{w['id']}", json={"status": "CLOSED"}, headers=customer.h)
    assert r.status_code == 409 and err_code(r) == "WALLET_NOT_EMPTY"


def test_only_admin_can_suspend(client, customer, support, admin):
    w = create_wallet(client, customer)
    assert client.patch(f"/api/wallets/{w['id']}", json={"status": "SUSPENDED"}, headers=customer.h).status_code == 403
    assert client.patch(f"/api/wallets/{w['id']}", json={"status": "SUSPENDED"}, headers=support.h).status_code == 403
    r = client.patch(f"/api/wallets/{w['id']}", json={"status": "SUSPENDED"}, headers=admin.h)
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED"
    # support can neither unfreeze a SUSPENDED wallet; admin can reactivate it
    assert client.post(f"/api/wallets/{w['id']}/unfreeze", headers=support.h).status_code == 409
    assert client.patch(f"/api/wallets/{w['id']}", json={"status": "ACTIVE"}, headers=admin.h).json()["status"] == "ACTIVE"


def test_same_status_patch_is_invalid(client, customer):
    w = create_wallet(client, customer)
    r = client.patch(f"/api/wallets/{w['id']}", json={"status": "ACTIVE"}, headers=customer.h)
    assert r.status_code == 409


def test_invalid_status_value(client, customer):
    w = create_wallet(client, customer)
    assert client.patch(f"/api/wallets/{w['id']}", json={"status": "BROKEN"}, headers=customer.h).status_code == 422


def test_freeze_unfreeze_create_notifications_and_audit(client, customer, admin):
    w = create_wallet(client, customer)
    client.post(f"/api/wallets/{w['id']}/freeze", headers=admin.h)
    client.post(f"/api/wallets/{w['id']}/unfreeze", headers=admin.h)
    types = [n["type"] for n in client.get("/api/notifications", headers=customer.h).json()["items"]]
    assert "WALLET_FROZEN" in types and "WALLET_UNFROZEN" in types
    actions = {l["action"] for l in client.get("/api/admin/audit-logs", headers=admin.h).json()["items"]}
    assert {"WALLET_CREATED", "WALLET_FROZEN", "WALLET_UNFROZEN"} <= actions


def test_ledger_endpoint_is_scoped(client, customer, customer2):
    w = create_wallet(client, customer)
    deposit(client, customer, w["id"], "50.00")
    r = client.get(f"/api/wallets/{w['id']}/ledger", headers=customer.h)
    assert r.status_code == 200 and r.json()["total"] == 1
    assert client.get(f"/api/wallets/{w['id']}/ledger", headers=customer2.h).status_code == 403

from datetime import timedelta
import uuid

from sqlalchemy import select, text

from app.core.security import create_access_token, create_refresh_token
from app.models import AuditLog, RefreshToken, User
from tests.helpers import PASSWORD, err_code, make_customer


def reg(client, email="new@example.com", password=PASSWORD, name="New User"):
    return client.post("/api/auth/register", json={"email": email, "full_name": name, "password": password})


# ---------------------------------------------------------------- registration
def test_register_success_creates_customer(client):
    r = reg(client)
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "CUSTOMER" and body["is_active"] is True
    assert "password" not in str(body).lower() or "hashed" not in str(body).lower()


def test_register_duplicate_email_case_insensitive(client):
    assert reg(client, "dup@example.com").status_code == 201
    r = reg(client, "DUP@example.com")
    assert r.status_code == 409 and err_code(r) == "EMAIL_ALREADY_REGISTERED"


def test_register_weak_password_rejected(client):
    for pw in ("short1A", "alllowercase1", "NoDigitsHere"):
        r = reg(client, f"{uuid.uuid4().hex[:6]}@example.com", pw)
        assert r.status_code == 422 and err_code(r) == "VALIDATION_ERROR"


def test_register_invalid_email_rejected(client):
    assert reg(client, "not-an-email").status_code == 422


def test_password_is_hashed_in_db(client, db):
    reg(client, "hash@example.com")
    user = db.scalar(select(User).where(User.email == "hash@example.com"))
    assert user.hashed_password != PASSWORD and user.hashed_password.startswith("$2")


# ---------------------------------------------------------------- login
def test_login_success_returns_tokens(client):
    reg(client, "l@example.com")
    r = client.post("/api/auth/login", json={"email": "l@example.com", "password": PASSWORD})
    assert r.status_code == 200
    assert {"access_token", "refresh_token", "token_type", "expires_in"} <= set(r.json())


def test_login_invalid_credentials(client):
    reg(client, "l@example.com")
    r = client.post("/api/auth/login", json={"email": "l@example.com", "password": "WrongPass123"})
    assert r.status_code == 401 and err_code(r) == "INVALID_CREDENTIALS"
    r = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": PASSWORD})
    assert r.status_code == 401 and err_code(r) == "INVALID_CREDENTIALS"


def test_failed_login_is_audited(client, db):
    reg(client, "l@example.com")
    client.post("/api/auth/login", json={"email": "l@example.com", "password": "WrongPass123"})
    assert db.scalar(select(AuditLog).where(AuditLog.action == "LOGIN_FAILED")) is not None


def test_successful_login_is_audited(client, db):
    make_customer(client, "l@example.com")
    assert db.scalar(select(AuditLog).where(AuditLog.action == "LOGIN")) is not None


def test_inactive_user_cannot_login(client, admin):
    u = make_customer(client, "gone@example.com")
    r = client.patch(f"/api/admin/users/{u.id}", json={"is_active": False}, headers=admin.h)
    assert r.status_code == 200
    r = client.post("/api/auth/login", json={"email": "gone@example.com", "password": PASSWORD})
    assert r.status_code == 401 and err_code(r) == "ACCOUNT_INACTIVE"


def test_deactivation_kills_existing_sessions(client, admin):
    u = make_customer(client, "gone@example.com")
    assert client.get("/api/auth/me", headers=u.h).status_code == 200
    client.patch(f"/api/admin/users/{u.id}", json={"is_active": False}, headers=admin.h)
    assert client.get("/api/auth/me", headers=u.h).status_code == 401
    r = client.post("/api/auth/refresh", json={"refresh_token": u.refresh})
    assert r.status_code == 401


# ---------------------------------------------------------------- protected endpoints / tokens
def test_protected_endpoint_requires_token(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401 and err_code(r) == "NOT_AUTHENTICATED"
    assert r.headers["www-authenticate"] == "Bearer"


def test_garbage_token_rejected(client):
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401 and err_code(r) == "INVALID_TOKEN"


def test_refresh_token_cannot_be_used_as_access_token(client, customer):
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {customer.refresh}"})
    assert r.status_code == 401 and err_code(r) == "INVALID_TOKEN"


def test_access_token_cannot_be_used_to_refresh(client, customer):
    r = client.post("/api/auth/refresh", json={"refresh_token": customer.access})
    assert r.status_code == 401


def test_expired_access_token(client, customer, db):
    sid = db.scalar(select(RefreshToken.family_id))
    token = create_access_token(uuid.UUID(customer.id), "CUSTOMER", sid, expires_delta=timedelta(seconds=-5))
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and err_code(r) == "TOKEN_EXPIRED"


def test_expired_refresh_token(client, customer, db):
    row = db.scalar(select(RefreshToken))
    token = create_refresh_token(uuid.UUID(customer.id), row.family_id, row.jti, expires_delta=timedelta(seconds=-5))
    r = client.post("/api/auth/refresh", json={"refresh_token": token})
    assert r.status_code == 401 and err_code(r) == "TOKEN_EXPIRED"


def test_expired_refresh_token_in_db_rejected(client, customer, db):
    row = db.scalar(select(RefreshToken))
    db.execute(text("UPDATE refresh_tokens SET expires_at = now() - interval '1 hour'"))
    db.commit()
    r = client.post("/api/auth/refresh", json={"refresh_token": customer.refresh})
    assert r.status_code == 401 and err_code(r) == "TOKEN_EXPIRED"


def test_tampered_token_rejected(client, customer):
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {customer.access[:-4]}abcd"})
    assert r.status_code == 401


# ---------------------------------------------------------------- refresh rotation / logout
def test_refresh_rotates_token(client, customer):
    r = client.post("/api/auth/refresh", json={"refresh_token": customer.refresh})
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != customer.refresh
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"}).status_code == 200


def test_reused_refresh_token_revokes_whole_session(client, customer):
    new = client.post("/api/auth/refresh", json={"refresh_token": customer.refresh}).json()
    # replaying the OLD (already rotated) token = theft signal
    r = client.post("/api/auth/refresh", json={"refresh_token": customer.refresh})
    assert r.status_code == 401 and err_code(r) == "TOKEN_REVOKED"
    # ... which also kills the legitimately rotated token and its access token
    r = client.post("/api/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert r.status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"}).status_code == 401


def test_logout_revokes_session(client, customer):
    r = client.post("/api/auth/logout", json={"refresh_token": customer.refresh}, headers=customer.h)
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=customer.h).status_code == 401           # access revoked
    assert client.post("/api/auth/refresh", json={"refresh_token": customer.refresh}).status_code == 401


def test_logout_without_body_revokes_current_session(client, customer):
    assert client.post("/api/auth/logout", headers=customer.h).status_code == 200
    assert client.get("/api/auth/me", headers=customer.h).status_code == 401


def test_logout_one_session_keeps_other_sessions(client, customer):
    other = client.post("/api/auth/login", json={"email": customer.email, "password": PASSWORD}).json()
    client.post("/api/auth/logout", headers=customer.h)
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {other['access_token']}"}).status_code == 200


def test_cannot_logout_with_someone_elses_refresh_token(client, customer, customer2):
    r = client.post("/api/auth/logout", json={"refresh_token": customer2.refresh}, headers=customer.h)
    assert r.status_code == 401


# ---------------------------------------------------------------- me / change password
def test_me_returns_profile(client, customer):
    r = client.get("/api/auth/me", headers=customer.h)
    assert r.status_code == 200 and r.json()["email"] == customer.email


def test_change_password_flow(client, customer):
    r = client.post("/api/auth/change-password", headers=customer.h,
                    json={"current_password": PASSWORD, "new_password": "BrandNewPass456"})
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=customer.h).status_code == 401      # all sessions revoked
    assert client.post("/api/auth/login", json={"email": customer.email, "password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/login",
                       json={"email": customer.email, "password": "BrandNewPass456"}).status_code == 200


def test_change_password_wrong_current(client, customer):
    r = client.post("/api/auth/change-password", headers=customer.h,
                    json={"current_password": "Nope12345", "new_password": "BrandNewPass456"})
    assert r.status_code == 401 and err_code(r) == "INVALID_CREDENTIALS"


def test_change_password_weak_new(client, customer):
    r = client.post("/api/auth/change-password", headers=customer.h,
                    json={"current_password": PASSWORD, "new_password": "weakpass"})
    assert r.status_code == 422

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import RoleName, User
from app.repositories.users import UserRepository

PASSWORD = "StrongPass123"


@dataclass
class Actor:
    id: str
    email: str
    access: str
    refresh: str
    role: str

    @property
    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.access}"}


def idem() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


def _login(client, email, password=PASSWORD):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def make_customer(client, email: str) -> Actor:
    r = client.post("/api/auth/register", json={"email": email, "full_name": "Test User", "password": PASSWORD})
    assert r.status_code == 201, r.text
    t = _login(client, email)
    return Actor(r.json()["id"], email, t["access_token"], t["refresh_token"], "CUSTOMER")


def make_staff(client, role: str, email: str) -> Actor:
    with SessionLocal() as db:
        repo = UserRepository(db)
        u = repo.add(User(email=email, full_name=f"{role} user", hashed_password=hash_password(PASSWORD),
                          role_id=repo.role_id(RoleName[role])))
        db.commit()
        uid = str(u.id)
    t = _login(client, email)
    return Actor(uid, email, t["access_token"], t["refresh_token"], role)


def create_wallet(client, actor: Actor, currency="INR", name="Main") -> dict:
    r = client.post("/api/wallets", json={"currency": currency, "name": name}, headers=actor.h)
    assert r.status_code == 201, r.text
    return r.json()


def deposit(client, actor, wallet_id, amount, key=None, **extra):
    headers = {**actor.h, "Idempotency-Key": key or uuid.uuid4().hex}
    r = client.post(f"/api/wallets/{wallet_id}/deposits", json={"amount": amount, **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def transfer(client, actor, sender, receiver, amount, key=None, **extra):
    headers = {**actor.h, "Idempotency-Key": key or uuid.uuid4().hex}
    return client.post("/api/transfers", headers=headers,
                       json={"sender_wallet_id": sender, "receiver_wallet_id": receiver, "amount": amount, **extra})


def withdraw(client, actor, wallet_id, amount, key=None, **extra):
    headers = {**actor.h, "Idempotency-Key": key or uuid.uuid4().hex}
    return client.post(f"/api/wallets/{wallet_id}/withdrawals", headers=headers, json={"amount": amount, **extra})


def wallet(client, actor, wallet_id) -> dict:
    r = client.get(f"/api/wallets/{wallet_id}", headers=actor.h)
    assert r.status_code == 200, r.text
    return r.json()


def bal(client, actor, wallet_id) -> Decimal:
    return Decimal(wallet(client, actor, wallet_id)["available_balance"])


def set_limits(client, admin, **limits):
    r = client.patch("/api/admin/transaction-limits", json=limits, headers=admin.h)
    assert r.status_code == 200, r.text
    return r.json()


def err_code(response) -> str:
    body = response.json()
    assert body["success"] is False, body
    return body["error"]["code"]

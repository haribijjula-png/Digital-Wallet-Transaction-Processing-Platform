"""Generates postman_collection.json:
  1. "Quick Start" - an ordered, self-asserting workflow (runs green top-to-bottom, also with Newman)
  2. One reference folder per API tag, built from the live OpenAPI schema (examples included)

    python -m scripts.generate_postman
"""
import json
import re
from pathlib import Path

from app.main import app

BASE = "{{baseUrl}}"
ROOT = Path(__file__).resolve().parent.parent


def js(lines: list[str]) -> dict:
    return {"type": "text/javascript", "exec": lines}


def item(name, method, path, *, body=None, token=None, headers=None, tests=None, pre=None, query=None, desc=None):
    hdrs = [{"key": k, "value": v} for k, v in (headers or {}).items()]
    if token:
        hdrs.append({"key": "Authorization", "value": f"Bearer {{{{{token}}}}}"})
    if body is not None:
        hdrs.append({"key": "Content-Type", "value": "application/json"})
    url = {"raw": f"{BASE}{path}", "host": [BASE], "path": [p for p in path.strip("/").split("/")]}
    if query:
        url["query"] = [{"key": k, "value": v} for k, v in query.items()]
        url["raw"] += "?" + "&".join(f"{k}={v}" for k, v in query.items())
    req = {"method": method, "header": hdrs, "url": url}
    if desc:
        req["description"] = desc
    if body is not None:
        req["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2), "options": {"raw": {"language": "json"}}}
    events = []
    if pre:
        events.append({"listen": "prerequest", "script": js(pre)})
    if tests:
        events.append({"listen": "test", "script": js(tests)})
    return {"name": name, "event": events, "request": req}


def status(code):
    return [f"pm.test('status is {code}', () => pm.response.to.have.status({code}));"]


def check(code, *extra):
    return status(code) + list(extra)


def save(var, expr):
    return f"pm.collectionVariables.set('{var}', {expr});"


GUID = "pm.variables.replaceIn('{{$guid}}')"
KEY = {"Idempotency-Key": "{{idemKey}}"}
NEWKEY = [save("idemKey", GUID)]
J = "const j = pm.response.json();"


def quick_start():
    s = []
    s.append(item("Health check", "GET", "/health", tests=check(200)))
    s.append(item("Register Alice (customer)", "POST", "/api/auth/register",
                  body={"email": "alice+{{runId}}@example.com", "full_name": "Alice Sharma", "password": "StrongPass123"},
                  tests=check(201, J, "pm.test('role is CUSTOMER', () => pm.expect(j.role).to.eql('CUSTOMER'));",
                              save("aliceId", "j.id"))))
    s.append(item("Register Bob (customer)", "POST", "/api/auth/register",
                  body={"email": "bob+{{runId}}@example.com", "full_name": "Bob Verma", "password": "StrongPass123"},
                  tests=check(201, J, save("bobId", "j.id"))))
    s.append(item("Register - duplicate e-mail is rejected", "POST", "/api/auth/register",
                  body={"email": "alice+{{runId}}@example.com", "full_name": "Alice Again", "password": "StrongPass123"},
                  tests=check(409, J, "pm.test('error code', () => pm.expect(j.error.code).to.eql('EMAIL_ALREADY_REGISTERED'));")))
    s.append(item("Login - invalid credentials", "POST", "/api/auth/login",
                  body={"email": "alice+{{runId}}@example.com", "password": "WrongPass123"},
                  tests=check(401, J, "pm.test('error code', () => pm.expect(j.error.code).to.eql('INVALID_CREDENTIALS'));")))
    for who, email in (("alice", "alice+{{runId}}@example.com"), ("bob", "bob+{{runId}}@example.com")):
        s.append(item(f"Login {who.title()}", "POST", "/api/auth/login",
                      body={"email": email, "password": "StrongPass123"},
                      tests=check(200, J, save(f"{who}Token", "j.access_token"), save(f"{who}Refresh", "j.refresh_token"))))
    s.append(item("Login seeded Super Admin", "POST", "/api/auth/login",
                  body={"email": "{{superAdminEmail}}", "password": "{{superAdminPassword}}"},
                  tests=check(200, J, save("superToken", "j.access_token"))))
    s.append(item("Super Admin creates a Support Agent", "POST", "/api/admin/users", token="superToken",
                  body={"email": "support+{{runId}}@example.com", "full_name": "Sam Support",
                        "password": "StrongPass123", "role": "SUPPORT_AGENT"}, tests=check(201)))
    s.append(item("Login Support Agent", "POST", "/api/auth/login",
                  body={"email": "support+{{runId}}@example.com", "password": "StrongPass123"},
                  tests=check(200, J, save("supportToken", "j.access_token"))))
    s.append(item("Alice - current user", "GET", "/api/auth/me", token="aliceToken", tests=check(200)))
    s.append(item("Customer cannot access admin API (RBAC)", "GET", "/api/admin/transaction-limits", token="aliceToken",
                  tests=check(403)))
    s.append(item("Alice creates a wallet", "POST", "/api/wallets", token="aliceToken",
                  body={"currency": "INR", "name": "Alice Savings"},
                  tests=check(201, J, "pm.test('starts at zero', () => pm.expect(j.available_balance).to.eql('0.00'));",
                              save("aliceWallet", "j.id"))))
    s.append(item("Bob creates a wallet", "POST", "/api/wallets", token="bobToken",
                  body={"currency": "INR", "name": "Bob Main"}, tests=check(201, J, save("bobWallet", "j.id"))))
    s.append(item("Bob cannot read Alice's wallet (ownership)", "GET", "/api/wallets/{{aliceWallet}}", token="bobToken",
                  tests=check(403)))
    s.append(item("Alice deposits 1000.00 (Idempotency-Key)", "POST", "/api/wallets/{{aliceWallet}}/deposits",
                  token="aliceToken", headers=KEY, pre=NEWKEY, body={"amount": "1000.00", "currency": "INR"},
                  tests=check(201, J, "pm.test('completed', () => pm.expect(j.status).to.eql('COMPLETED'));",
                              save("depositId", "j.id"))))
    s.append(item("Retry the SAME deposit (same key) - replayed, not applied twice", "POST",
                  "/api/wallets/{{aliceWallet}}/deposits", token="aliceToken", headers=KEY,
                  body={"amount": "1000.00", "currency": "INR"},
                  tests=check(201, J, "pm.test('replay header', () => pm.response.to.have.header('Idempotent-Replayed', 'true'));",
                              "pm.test('same deposit', () => pm.expect(j.id).to.eql(pm.collectionVariables.get('depositId')));")))
    s.append(item("Alice's balance is still 1000.00", "GET", "/api/wallets/{{aliceWallet}}", token="aliceToken",
                  tests=check(200, J, "pm.test('1000.00', () => pm.expect(j.available_balance).to.eql('1000.00'));")))
    s.append(item("Transfer 300.00 Alice -> Bob", "POST", "/api/transfers", token="aliceToken", headers=KEY, pre=NEWKEY,
                  body={"sender_wallet_id": "{{aliceWallet}}", "receiver_wallet_id": "{{bobWallet}}",
                        "amount": "300.00", "currency": "INR", "reference": "Dinner"},
                  tests=check(201, J, "pm.test('completed', () => pm.expect(j.status).to.eql('COMPLETED'));",
                              save("transferId", "j.id"))))
    s.append(item("Retry the same transfer (same key) - no duplicate", "POST", "/api/transfers", token="aliceToken",
                  headers=KEY, body={"sender_wallet_id": "{{aliceWallet}}", "receiver_wallet_id": "{{bobWallet}}",
                                     "amount": "300.00", "currency": "INR", "reference": "Dinner"},
                  tests=check(201, J, "pm.test('replayed', () => pm.response.to.have.header('Idempotent-Replayed', 'true'));",
                              "pm.test('same transfer', () => pm.expect(j.id).to.eql(pm.collectionVariables.get('transferId')));")))
    s.append(item("Same key + different amount -> 409", "POST", "/api/transfers", token="aliceToken", headers=KEY,
                  body={"sender_wallet_id": "{{aliceWallet}}", "receiver_wallet_id": "{{bobWallet}}",
                        "amount": "999.00", "currency": "INR"},
                  tests=check(409, J, "pm.test('code', () => pm.expect(j.error.code).to.eql('DUPLICATE_IDEMPOTENCY_KEY'));")))
    s.append(item("Insufficient balance -> INSUFFICIENT_BALANCE", "POST", "/api/transfers", token="aliceToken",
                  headers={"Idempotency-Key": "{{$guid}}"},
                  body={"sender_wallet_id": "{{aliceWallet}}", "receiver_wallet_id": "{{bobWallet}}", "amount": "5000.00"},
                  tests=check(400, J, "pm.test('code', () => pm.expect(j.error.code).to.eql('INSUFFICIENT_BALANCE'));")))
    s.append(item("Missing Idempotency-Key -> 400", "POST", "/api/transfers", token="aliceToken",
                  body={"sender_wallet_id": "{{aliceWallet}}", "receiver_wallet_id": "{{bobWallet}}", "amount": "1.00"},
                  tests=check(400, J, "pm.test('code', () => pm.expect(j.error.code).to.eql('IDEMPOTENCY_KEY_REQUIRED'));")))
    s.append(item("Balances: Alice 700.00", "GET", "/api/wallets/{{aliceWallet}}", token="aliceToken",
                  tests=check(200, J, "pm.test('700.00', () => pm.expect(j.available_balance).to.eql('700.00'));")))
    s.append(item("Balances: Bob 300.00", "GET", "/api/wallets/{{bobWallet}}", token="bobToken",
                  tests=check(200, J, "pm.test('300.00', () => pm.expect(j.available_balance).to.eql('300.00'));")))
    s.append(item("Withdraw 100.00", "POST", "/api/wallets/{{aliceWallet}}/withdrawals", token="aliceToken",
                  headers={"Idempotency-Key": "{{$guid}}"}, body={"amount": "100.00", "destination": "HDFC ****1234"},
                  tests=check(201, J, "pm.test('completed', () => pm.expect(j.status).to.eql('COMPLETED'));")))
    s.append(item("Transaction history (filters + pagination)", "GET", "/api/transactions", token="aliceToken",
                  query={"type": "transfer", "status": "completed", "min_amount": "100", "page": "1", "page_size": "20",
                         "sort_by": "amount", "order": "desc"},
                  tests=check(200, J, "pm.test('one transfer', () => pm.expect(j.total).to.eql(1));",
                              save("txId", "j.items[0].id"))))
    s.append(item("Ledger entries of the transfer (immutable)", "GET", "/api/transactions/{{txId}}/ledger",
                  token="aliceToken",
                  tests=check(200, J, "pm.test('debit+credit', () => pm.expect(j.map(e => e.transaction_type).sort())"
                                      ".to.eql(['TRANSFER_CREDIT','TRANSFER_DEBIT']));")))
    s.append(item("Bob's notifications (transfer received)", "GET", "/api/notifications", token="bobToken",
                  tests=check(200, J, "pm.test('has TRANSFER_RECEIVED', () => pm.expect(j.items.map(n => n.type))"
                                      ".to.include('TRANSFER_RECEIVED'));")))
    s.append(item("Bob marks all notifications read", "PATCH", "/api/notifications/read-all", token="bobToken",
                  tests=check(200)))
    s.append(item("Alice requests a refund of the transfer", "POST", "/api/refunds", token="aliceToken",
                  headers={"Idempotency-Key": "{{$guid}}"},
                  body={"transaction_id": "{{txId}}", "amount": "300.00", "reason": "Wrong recipient"},
                  tests=check(201, J, "pm.test('requested', () => pm.expect(j.status).to.eql('REQUESTED'));",
                              save("refundId", "j.id"))))
    s.append(item("Alice cannot approve her own refund", "POST", "/api/refunds/{{refundId}}/approve", token="aliceToken",
                  tests=check(403)))
    s.append(item("Support approves the refund", "POST", "/api/refunds/{{refundId}}/approve", token="supportToken",
                  body={"notes": "Verified"},
                  tests=check(200, J, "pm.test('completed', () => pm.expect(j.status).to.eql('COMPLETED'));")))
    s.append(item("Alice is refunded: 700.00 + 300.00 - 100.00 = 900.00", "GET", "/api/wallets/{{aliceWallet}}",
                  token="aliceToken",
                  tests=check(200, J, "pm.test('900.00', () => pm.expect(j.available_balance).to.eql('900.00'));")))
    s.append(item("Support freezes Bob's wallet", "POST", "/api/wallets/{{bobWallet}}/freeze", token="supportToken",
                  tests=check(200, J, "pm.test('FROZEN', () => pm.expect(j.status).to.eql('FROZEN'));")))
    s.append(item("Transfer to a frozen wallet is rejected", "POST", "/api/transfers", token="aliceToken",
                  headers={"Idempotency-Key": "{{$guid}}"},
                  body={"sender_wallet_id": "{{aliceWallet}}", "receiver_wallet_id": "{{bobWallet}}", "amount": "10.00"},
                  tests=check(409, J, "pm.test('code', () => pm.expect(j.error.code).to.eql('WALLET_FROZEN'));")))
    s.append(item("Support unfreezes Bob's wallet", "POST", "/api/wallets/{{bobWallet}}/unfreeze", token="supportToken",
                  tests=check(200)))
    s.append(item("Admin: get transaction limits", "GET", "/api/admin/transaction-limits", token="superToken",
                  tests=check(200)))
    s.append(item("Admin: lower the suspicious-amount threshold (audited)", "PATCH", "/api/admin/transaction-limits",
                  token="superToken", body={"suspicious_amount_threshold": "500.00"}, tests=check(200)))
    s.append(item("Large transfer is HELD for review (202, funds reserved)", "POST", "/api/transfers",
                  token="aliceToken", headers={"Idempotency-Key": "{{$guid}}"},
                  body={"sender_wallet_id": "{{aliceWallet}}", "receiver_wallet_id": "{{bobWallet}}", "amount": "600.00"},
                  tests=check(202, J, "pm.test('PENDING', () => pm.expect(j.status).to.eql('PENDING'));",
                              save("heldTransferId", "j.id"))))
    s.append(item("Support: list flagged transactions", "GET", "/api/admin/suspicious-transactions", token="supportToken",
                  query={"review_status": "pending"},
                  tests=check(200, J, "pm.test('one pending', () => pm.expect(j.total).to.eql(1));",
                              save("suspiciousId", "j.items[0].id"))))
    s.append(item("Support approves the flagged transfer", "POST",
                  "/api/admin/suspicious-transactions/{{suspiciousId}}/review", token="supportToken",
                  body={"decision": "APPROVE", "notes": "Customer verified"},
                  tests=check(200, J, "pm.test('APPROVED', () => pm.expect(j.review_status).to.eql('APPROVED'));")))
    s.append(item("Held transfer is now COMPLETED", "GET", "/api/transfers/{{heldTransferId}}", token="aliceToken",
                  tests=check(200, J, "pm.test('COMPLETED', () => pm.expect(j.status).to.eql('COMPLETED'));")))
    s.append(item("Restore the suspicious-amount threshold", "PATCH", "/api/admin/transaction-limits", token="superToken",
                  body={"suspicious_amount_threshold": "25000.00"}, tests=check(200)))
    s.append(item("Dashboard - customer", "GET", "/api/dashboard/customer", token="aliceToken", tests=check(200)))
    s.append(item("Dashboard - support", "GET", "/api/dashboard/support", token="supportToken", tests=check(200)))
    s.append(item("Dashboard - admin", "GET", "/api/dashboard/admin", token="superToken", tests=check(200)))
    s.append(item("Audit logs (admin)", "GET", "/api/admin/audit-logs", token="superToken",
                  query={"action": "TRANSACTION_LIMIT_CHANGED"}, tests=check(200, J,
                  "pm.test('limit changes audited', () => pm.expect(j.total).to.be.above(0));")))
    s.append(item("Run a scheduled job on demand (suspicious scan)", "POST", "/api/admin/jobs/suspicious_scan/run",
                  token="superToken", tests=check(200)))
    s.append(item("Refresh token (rotation)", "POST", "/api/auth/refresh", body={"refresh_token": "{{bobRefresh}}"},
                  tests=check(200, J, save("bobRefreshOld", "pm.collectionVariables.get('bobRefresh')"),
                              save("bobRefresh", "j.refresh_token"))))
    s.append(item("Re-using the OLD refresh token revokes the session", "POST", "/api/auth/refresh",
                  body={"refresh_token": "{{bobRefreshOld}}"},
                  tests=check(401, J, "pm.test('code', () => pm.expect(j.error.code).to.eql('TOKEN_REVOKED'));")))
    s.append(item("Alice logs out", "POST", "/api/auth/logout", token="aliceToken",
                  body={"refresh_token": "{{aliceRefresh}}"}, tests=check(200)))
    s.append(item("Alice's token no longer works", "GET", "/api/auth/me", token="aliceToken", tests=check(401)))
    return s


def reference_folders():
    spec = app.openapi()
    folders: dict[str, list] = {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            tag = (op.get("tags") or ["Other"])[0]
            if tag == "Health":
                continue
            body = None
            rb = op.get("requestBody", {}).get("content", {}).get("application/json", {})
            if rb:
                schema = rb["schema"]
                ref = schema.get("$ref") or (schema.get("anyOf", [{}])[0]).get("$ref")
                if ref:
                    comp = spec["components"]["schemas"][ref.split("/")[-1]]
                    ex = (comp.get("examples") or [None])[0]
                    body = ex or {k: (v.get("default") if "default" in v else "string")
                                  for k, v in comp.get("properties", {}).items()}
            hdrs = {"Idempotency-Key": "{{$guid}}"} if any(
                p.get("name") == "Idempotency-Key" for p in op.get("parameters", [])) else None
            pth = re.sub(r"\{(\w+)\}", lambda m: "{{" + m.group(1) + "}}", path)
            q = {p["name"]: "" for p in op.get("parameters", []) if p["in"] == "query" and p.get("required")}
            tok = None if path in ("/api/auth/register", "/api/auth/login", "/api/auth/refresh") else "accessToken"
            folders.setdefault(tag, []).append(
                item(f"{method.upper()} {path}", method.upper(), pth, body=body, token=tok, headers=hdrs,
                     desc=op.get("summary"), query=q or None))
    return [{"name": f"Reference - {tag}", "item": items} for tag, items in sorted(folders.items())]


def build():
    return {
        "info": {
            "name": "Digital Wallet & Transaction Processing Platform",
            "description": ("Run folder 'Quick Start' top-to-bottom (Collection Runner / Newman) for a self-asserting "
                            "end-to-end demo. The 'Reference' folders contain every endpoint with example bodies; "
                            "set `accessToken` (from POST /api/auth/login) to call them."),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "variable": [
            {"key": "baseUrl", "value": "http://localhost:8000"},
            {"key": "superAdminEmail", "value": "superadmin@example.com"},
            {"key": "superAdminPassword", "value": "ChangeMe123!"},
            {"key": "accessToken", "value": ""},
            {"key": "wallet_id", "value": ""}, {"key": "deposit_id", "value": ""}, {"key": "withdrawal_id", "value": ""},
            {"key": "transfer_id", "value": ""}, {"key": "refund_id", "value": ""}, {"key": "transaction_id", "value": ""},
            {"key": "suspicious_id", "value": ""}, {"key": "user_id", "value": ""}, {"key": "notification_id", "value": ""},
            {"key": "job_name", "value": "pending_cleanup"},
        ],
        "event": [{"listen": "prerequest", "script": js([
            "if (!pm.collectionVariables.get('runId')) { pm.collectionVariables.set('runId', Date.now().toString()); }"])}],
        "item": [{"name": "Quick Start (run in order)", "item": quick_start()}] + reference_folders(),
    }


if __name__ == "__main__":
    out = ROOT / "postman_collection.json"
    out.write_text(json.dumps(build(), indent=2))
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")

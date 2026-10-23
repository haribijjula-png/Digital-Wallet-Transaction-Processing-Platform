"""Builds docs/API.md from the live route table + real captured request/response samples.

    python -m scripts.capture_api_samples      # once (needs the test DB)
    python -m scripts.generate_api_docs [samples.json]
"""
import json
import sys
from pathlib import Path

from fastapi.routing import APIRoute

from app.api.deps import get_current_user
from app.main import ROUTERS

ROOT = Path(__file__).resolve().parent.parent
ROLE_LABEL = {"SUPER_ADMIN": "Super Admin", "ADMIN": "Admin", "SUPPORT_AGENT": "Support", "CUSTOMER": "Customer"}


def access_of(route: APIRoute) -> str:
    def walk(dep):
        for d in dep.dependencies:
            yield d
            yield from walk(d)
    for d in walk(route.dependant):
        roles = getattr(d.call, "allowed_roles", None)
        if roles:
            names = sorted(ROLE_LABEL[r.value] for r in roles)
            if len(names) == 4:
                return "Any authenticated"
            return ", ".join(names)
    if any(d.call is get_current_user for d in walk(route.dependant)):
        return "Authenticated"
    return "Public"


def endpoint_table() -> str:
    rows = ["| Method | Path | Access | Description |", "|---|---|---|---|"]
    for r in (route for router in ROUTERS for route in router.routes):
        if isinstance(r, APIRoute) and r.path.startswith("/api"):
            def _walk(dep):
                for d in dep.dependencies:
                    yield d
                    yield from _walk(d)
            idem = " **Requires `Idempotency-Key`.**" if any(
                p.alias == "Idempotency-Key" for d in _walk(r.dependant) for p in d.header_params) else ""
            for m in sorted(r.methods):
                rows.append(f"| `{m}` | `{r.path}` | {access_of(r)} | {r.summary or ''}{idem} |")
    return "\n".join(rows)


def block(S, key, title, method_path, headers=None, note=None) -> str:
    s = S[key]
    out = [f"#### {title}", "", f"`{method_path}`", ""]
    if note:
        out += [note, ""]
    if headers:
        out += ["Headers: " + ", ".join(f"`{h}`" for h in headers), ""]
    if s.get("request") is not None:
        out += ["Request:", "```json", json.dumps(s["request"], indent=2), "```", ""]
    extra = ""
    if s.get("headers"):
        extra = "  (response header: " + ", ".join(f"`{k}: {v}`" for k, v in s["headers"].items()) + ")"
    out += [f"Response `{s['status']}`{extra}:", "```json", json.dumps(s["response"], indent=2), "```", ""]
    return "\n".join(out)


def main(samples_path: str) -> None:
    S = json.loads(Path(samples_path).read_text())
    A, I = ["Authorization: Bearer <access_token>"], ["Authorization: Bearer <access_token>", "Idempotency-Key: <unique key>"]
    parts = ["""# API Documentation

Interactive docs: **Swagger UI** `GET /docs` - **ReDoc** `GET /redoc` - **OpenAPI JSON** `GET /openapi.json`.
A ready-to-run **Postman collection** is in `postman_collection.json`.

## Conventions

* **Auth** - `Authorization: Bearer <access_token>` (JWT, 15 min). Refresh tokens rotate on every use.
* **Money** - decimal **strings** with exactly 2 decimals (`"800.00"`); never floats. Amounts must be `> 0`.
* **IDs** - UUIDs. **Timestamps** - ISO-8601 UTC.
* **Idempotency** - `POST` of deposits, withdrawals, transfers and refunds **requires** an `Idempotency-Key` header.
  Same key + same body => the original response is replayed (header `Idempotent-Replayed: true`) and nothing is executed twice.
  Same key + different body => `409 DUPLICATE_IDEMPOTENCY_KEY`. Keys are scoped per user.
* **Pagination** - `page` (>=1), `page_size` (1-100), `sort_by`, `order=asc|desc`; responses are
  `{items, total, page, page_size, pages}`. Enum filters are case-insensitive (`?type=transfer`).
* **Status codes** - `200/201` success, `202` accepted-but-held-for-review, `400` business-rule violation,
  `401` unauthenticated, `403` forbidden, `404` not found, `409` conflict / invalid state, `422` validation, `500` unexpected.

### Error envelope (every error, every status)
```json
{ "success": false, "error": { "code": "INSUFFICIENT_BALANCE", "message": "Insufficient wallet balance." } }
```

| Code | HTTP | Meaning |
|---|---|---|
| `INSUFFICIENT_BALANCE` | 400 | Spendable balance (available - reserved) is too low |
| `INVALID_AMOUNT` | 400 | Amount <= 0 (service-level guard; the API returns 422 first) |
| `INVALID_TRANSFER` | 400 | Self transfer, currency mismatch |
| `INVALID_REFUND` | 400 | Refund > original, original not a COMPLETED transfer |
| `TRANSACTION_LIMIT_EXCEEDED` | 400 | Max amount / daily limit / daily count exceeded |
| `SUSPICIOUS_TRANSACTION` | 403 | Blocked by velocity rule |
| `WALLET_FROZEN` / `WALLET_CLOSED` / `WALLET_SUSPENDED` | 409 | Wallet not ACTIVE |
| `INVALID_STATE_TRANSITION` | 409 | Illegal status change (wallet, transfer, refund ...) |
| `DUPLICATE_TRANSACTION` | 409 | A live refund already exists for the transaction |
| `DUPLICATE_IDEMPOTENCY_KEY` | 409 | Key reused with a different payload |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | Header missing |
| `WALLET_ACCESS_DENIED`, `ROLE_NOT_ALLOWED`, `FORBIDDEN` | 403 | Ownership / role checks |
| `TOKEN_EXPIRED`, `INVALID_TOKEN`, `TOKEN_REVOKED`, `NOT_AUTHENTICATED`, `INVALID_CREDENTIALS`, `ACCOUNT_INACTIVE` | 401 | Authentication |
| `VALIDATION_ERROR` | 422 | Body/query validation (`details[]` lists the fields) |
| `*_NOT_FOUND` | 404 | Unknown resource |
| `INTERNAL_SERVER_ERROR` | 500 | Unexpected (no internals leaked) |

## Endpoint index

*Authenticated* endpoints are additionally protected by **resource-level authorization** in the service layer:
customers can only access their own wallets, deposits, withdrawals, transfers (as sender or receiver), refunds,
transactions and notifications; staff (Support / Admin / Super Admin) can read across customers, and
destructive or configuration actions are restricted to the roles shown.

""", endpoint_table(), "\n\n## Examples (captured from a real run)\n\n### Authentication\n",
             block(S, "register", "Register", "POST /api/auth/register"),
             block(S, "login", "Login", "POST /api/auth/login"),
             block(S, "refresh", "Refresh (rotation: the old refresh token is now dead)", "POST /api/auth/refresh"),
             block(S, "invalid_login", "Invalid credentials", "POST /api/auth/login"),
             "### Wallets\n",
             block(S, "create_wallet", "Create wallet", "POST /api/wallets", A),
             block(S, "wallet_get", "Get wallet", "GET /api/wallets/{wallet_id}", A),
             block(S, "freeze", "Freeze wallet (Support)", "POST /api/wallets/{wallet_id}/freeze", A),
             "### Deposits, idempotency\n",
             block(S, "deposit", "Create deposit", "POST /api/wallets/{wallet_id}/deposits", ["Authorization: Bearer <access_token>", "Idempotency-Key: abc123"]),
             block(S, "deposit_replay", "Retry with the same key - replayed, balance NOT credited again", "POST /api/wallets/{wallet_id}/deposits", ["Idempotency-Key: abc123"]),
             block(S, "idem_conflict", "Same key, different body", "POST /api/wallets/{wallet_id}/deposits", ["Idempotency-Key: abc123"]),
             block(S, "missing_key", "Missing key", "POST /api/wallets/{wallet_id}/deposits"),
             "### Withdrawals\n",
             block(S, "withdrawal", "Create withdrawal", "POST /api/wallets/{wallet_id}/withdrawals", I),
             "### Transfers\n",
             block(S, "transfer", "Create transfer", "POST /api/transfers", I),
             block(S, "insufficient", "Insufficient balance (a FAILED transfer record is kept; no money moves)", "POST /api/transfers", I),
             block(S, "self_transfer", "Transfer to yourself", "POST /api/transfers", I),
             block(S, "transfer_frozen", "Receiver wallet frozen", "POST /api/transfers", I),
             block(S, "transfer_held", "Suspicious amount: held for review, funds reserved (202)", "POST /api/transfers", I,
                   "The sender's `available_balance` is unchanged but `pending_balance` rises by the held amount, so the money cannot be spent twice."),
             block(S, "wallet_pending", "Sender wallet while the transfer is held", "GET /api/wallets/{wallet_id}", A),
             "### Suspicious transactions (staff)\n",
             block(S, "suspicious_list", "List flagged transactions", "GET /api/admin/suspicious-transactions?review_status=pending"),
             block(S, "suspicious_review", "Approve - releases the held funds and completes the transfer", "POST /api/admin/suspicious-transactions/{id}/review"),
             "### Refunds\n",
             block(S, "refund_request", "Request refund (sender of a COMPLETED transfer)", "POST /api/refunds", I),
             block(S, "refund_approve", "Approve (staff) - posts two REFUND ledger entries", "POST /api/refunds/{refund_id}/approve"),
             "### Transactions & ledger\n",
             block(S, "transactions", "Search / filter / paginate", "GET /api/transactions?type=transfer&status=completed&min_amount=100&page=1&page_size=20", A),
             block(S, "tx_ledger", "Immutable ledger entries of one transaction", "GET /api/transactions/{id}/ledger", A),
             "### Notifications\n",
             block(S, "notifications", "List notifications", "GET /api/notifications"),
             "### Limits & administration\n",
             block(S, "limits_get", "Get limits", "GET /api/admin/transaction-limits"),
             block(S, "limits_patch", "Update limits (audited)", "PATCH /api/admin/transaction-limits"),
             block(S, "audit", "Audit logs", "GET /api/admin/audit-logs"),
             block(S, "job", "Run a scheduled job on demand", "POST /api/admin/jobs/{job_name}/run"),
             "### Dashboards\n",
             block(S, "dash_customer", "Customer", "GET /api/dashboard/customer"),
             block(S, "dash_support", "Support", "GET /api/dashboard/support"),
             block(S, "dash_admin", "Admin", "GET /api/dashboard/admin"),
             "### Errors\n",
             block(S, "validation", "Validation error (422)", "POST /api/wallets"),
             block(S, "forbidden", "Forbidden (403)", "GET /api/admin/transaction-limits"),
             block(S, "unauth", "Unauthenticated (401)", "GET /api/wallets")]
    out = ROOT / "docs" / "API.md"
    out.write_text("\n".join(parts))
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "docs" / "api_samples.json"))

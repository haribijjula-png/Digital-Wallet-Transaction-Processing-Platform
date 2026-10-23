# API Documentation

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


| Method | Path | Access | Description |
|---|---|---|---|
| `POST` | `/api/auth/register` | Public | Register a new customer account |
| `POST` | `/api/auth/login` | Public | Login (returns access + refresh token) |
| `POST` | `/api/auth/refresh` | Public | Rotate the refresh token and get a new access token |
| `POST` | `/api/auth/logout` | Authenticated | Revoke the current session (and the supplied refresh token family) |
| `GET` | `/api/auth/me` | Authenticated | Current user profile |
| `POST` | `/api/auth/change-password` | Authenticated | Change password (revokes all sessions) |
| `POST` | `/api/wallets` | Customer | Create a wallet (customers) |
| `GET` | `/api/wallets` | Authenticated | List wallets (customers: own wallets; staff: all, optional user_id filter) |
| `GET` | `/api/wallets/{wallet_id}` | Authenticated | Get a wallet |
| `PATCH` | `/api/wallets/{wallet_id}` | Authenticated | Update wallet name / status (invalid status transitions are rejected) |
| `POST` | `/api/wallets/{wallet_id}/freeze` | Authenticated | Freeze a wallet (owner or staff) |
| `POST` | `/api/wallets/{wallet_id}/unfreeze` | Authenticated | Unfreeze a wallet (staff only) |
| `GET` | `/api/wallets/{wallet_id}/ledger` | Authenticated | Immutable ledger entries of a wallet |
| `POST` | `/api/wallets/{wallet_id}/deposits` | Customer | Create a (simulated) deposit - idempotent via Idempotency-Key **Requires `Idempotency-Key`.** |
| `POST` | `/api/wallets/{wallet_id}/withdrawals` | Customer | Create a withdrawal - idempotent via Idempotency-Key (202 when held for review) **Requires `Idempotency-Key`.** |
| `GET` | `/api/deposits` | Authenticated | List deposits |
| `GET` | `/api/deposits/{deposit_id}` | Authenticated | Get a deposit |
| `GET` | `/api/withdrawals` | Authenticated | List withdrawals |
| `GET` | `/api/withdrawals/{withdrawal_id}` | Authenticated | Get a withdrawal |
| `POST` | `/api/withdrawals/{withdrawal_id}/cancel` | Authenticated | Cancel a PENDING (held for review) withdrawal |
| `POST` | `/api/transfers` | Customer | Create a transfer - idempotent via Idempotency-Key (202 when held for review) **Requires `Idempotency-Key`.** |
| `GET` | `/api/transfers` | Authenticated | List transfers |
| `GET` | `/api/transfers/{transfer_id}` | Authenticated | Get a transfer |
| `POST` | `/api/transfers/{transfer_id}/cancel` | Authenticated | Cancel a PENDING (held for review) transfer |
| `POST` | `/api/refunds` | Customer | Request a refund of a completed transfer - idempotent via Idempotency-Key **Requires `Idempotency-Key`.** |
| `GET` | `/api/refunds` | Authenticated | List refunds |
| `GET` | `/api/refunds/{refund_id}` | Authenticated | Get a refund |
| `POST` | `/api/refunds/{refund_id}/start-review` | Admin, Super Admin, Support | Move a refund to UNDER_REVIEW (staff) |
| `POST` | `/api/refunds/{refund_id}/approve` | Admin, Super Admin, Support | Approve a refund and post the REFUND ledger entries (staff) |
| `POST` | `/api/refunds/{refund_id}/reject` | Admin, Super Admin, Support | Reject a refund (staff) |
| `GET` | `/api/transactions` | Authenticated | Transaction history: pagination, sorting, search and filters |
| `GET` | `/api/transactions/{transaction_id}` | Authenticated | Get a transaction |
| `GET` | `/api/transactions/{transaction_id}/ledger` | Authenticated | Ledger entries produced by a transaction |
| `GET` | `/api/notifications` | Authenticated | List my notifications |
| `PATCH` | `/api/notifications/read-all` | Authenticated | Mark all notifications as read |
| `PATCH` | `/api/notifications/{notification_id}/read` | Authenticated | Mark one notification as read |
| `GET` | `/api/dashboard/admin` | Admin, Super Admin | Admin / Super Admin dashboard |
| `GET` | `/api/dashboard/support` | Admin, Super Admin, Support | Support agent dashboard (staff) |
| `GET` | `/api/dashboard/customer` | Customer | Customer dashboard |
| `GET` | `/api/admin/transaction-limits` | Admin, Super Admin | Get transaction limits & monitoring thresholds |
| `PATCH` | `/api/admin/transaction-limits` | Admin, Super Admin | Update transaction limits & monitoring thresholds (audited) |
| `GET` | `/api/admin/users` | Admin, Super Admin | List users |
| `POST` | `/api/admin/users` | Super Admin | Create a user with any role (Super Admin) |
| `GET` | `/api/admin/users/{user_id}` | Admin, Super Admin | Get a user |
| `PATCH` | `/api/admin/users/{user_id}` | Admin, Super Admin | Activate/deactivate a user, change role (Super Admin), rename |
| `GET` | `/api/admin/suspicious-transactions` | Admin, Super Admin, Support | List flagged transactions (staff) |
| `GET` | `/api/admin/suspicious-transactions/{suspicious_id}` | Admin, Super Admin, Support | Get a flagged transaction (staff) |
| `POST` | `/api/admin/suspicious-transactions/{suspicious_id}/review` | Admin, Super Admin, Support | Approve (release funds) or reject (release hold) a flagged transaction (staff) |
| `GET` | `/api/admin/audit-logs` | Admin, Super Admin | Search audit logs |
| `POST` | `/api/admin/wallets/{wallet_id}/adjustments` | Super Admin | Post a manual balance adjustment (Super Admin, ledger-backed) |
| `POST` | `/api/admin/jobs/{job_name}/run` | Admin, Super Admin | Run a scheduled job on demand: daily_limit_reset, pending_cleanup, suspicious_scan, daily_wallet_summary, failed_transaction_retry, daily_transaction_report |


## Examples (captured from a real run)

### Authentication

#### Register

`POST /api/auth/register`

Request:
```json
{
  "email": "alice@example.com",
  "full_name": "Alice Sharma",
  "password": "StrongPass123"
}
```

Response `201`:
```json
{
  "id": "57a6dcc2-2d2b-4fa1-968c-6a8729f81cc3",
  "email": "alice@example.com",
  "full_name": "Alice Sharma",
  "role": "CUSTOMER",
  "is_active": true,
  "created_at": "2026-10-12T03:46:10.138225Z",
  "last_login_at": null
}
```

#### Login

`POST /api/auth/login`

Request:
```json
{
  "email": "alice@example.com",
  "password": "StrongPass123"
}
```

Response `200`:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5\u2026",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5\u2026",
  "token_type": "bearer",
  "expires_in": 900
}
```

#### Refresh (rotation: the old refresh token is now dead)

`POST /api/auth/refresh`

Request:
```json
{
  "refresh_token": "<refresh_token>"
}
```

Response `200`:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5\u2026",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5\u2026",
  "token_type": "bearer",
  "expires_in": 900
}
```

#### Invalid credentials

`POST /api/auth/login`

Response `401`:
```json
{
  "success": false,
  "error": {
    "code": "INVALID_CREDENTIALS",
    "message": "Invalid e-mail or password."
  }
}
```

### Wallets

#### Create wallet

`POST /api/wallets`

Headers: `Authorization: Bearer <access_token>`

Request:
```json
{
  "currency": "INR",
  "name": "Savings"
}
```

Response `201`:
```json
{
  "id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "user_id": "57a6dcc2-2d2b-4fa1-968c-6a8729f81cc3",
  "name": "Savings",
  "currency": "INR",
  "available_balance": "0.00",
  "pending_balance": "0.00",
  "status": "ACTIVE",
  "created_at": "2026-10-12T03:46:10.227515Z",
  "updated_at": "2026-10-12T03:46:10.227515Z"
}
```

#### Get wallet

`GET /api/wallets/{wallet_id}`

Headers: `Authorization: Bearer <access_token>`

Response `200`:
```json
{
  "id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "user_id": "57a6dcc2-2d2b-4fa1-968c-6a8729f81cc3",
  "name": "Savings",
  "currency": "INR",
  "available_balance": "700.00",
  "pending_balance": "0.00",
  "status": "ACTIVE",
  "created_at": "2026-10-12T03:46:10.227515Z",
  "updated_at": "2026-10-12T03:46:10.227515Z"
}
```

#### Freeze wallet (Support)

`POST /api/wallets/{wallet_id}/freeze`

Headers: `Authorization: Bearer <access_token>`

Response `200`:
```json
{
  "id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
  "user_id": "72ec1e51-0ceb-4f6f-bebf-a24ef53b5259",
  "name": "Main",
  "currency": "INR",
  "available_balance": "0.00",
  "pending_balance": "0.00",
  "status": "FROZEN",
  "created_at": "2026-10-12T03:46:10.242675Z",
  "updated_at": "2026-10-12T03:46:10.596590Z"
}
```

### Deposits, idempotency

#### Create deposit

`POST /api/wallets/{wallet_id}/deposits`

Headers: `Authorization: Bearer <access_token>`, `Idempotency-Key: abc123`

Request:
```json
{
  "amount": "1000.00"
}
```

Response `201`:
```json
{
  "id": "a9b634dd-6ee9-4088-b880-a600d465a977",
  "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "amount": "1000.00",
  "currency": "INR",
  "status": "COMPLETED",
  "reference_number": "DEP-656560794FFD4A",
  "failure_reason": null,
  "created_at": "2026-10-12T03:46:10.251913Z",
  "completed_at": "2026-10-12T03:46:10.283627Z"
}
```

#### Retry with the same key - replayed, balance NOT credited again

`POST /api/wallets/{wallet_id}/deposits`

Headers: `Idempotency-Key: abc123`

Request:
```json
{
  "amount": "1000.00"
}
```

Response `201`  (response header: `idempotent-replayed: true`):
```json
{
  "id": "a9b634dd-6ee9-4088-b880-a600d465a977",
  "amount": "1000.00",
  "status": "COMPLETED",
  "currency": "INR",
  "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "created_at": "2026-10-12T03:46:10.251913Z",
  "completed_at": "2026-10-12T03:46:10.283627Z",
  "failure_reason": null,
  "reference_number": "DEP-656560794FFD4A"
}
```

#### Same key, different body

`POST /api/wallets/{wallet_id}/deposits`

Headers: `Idempotency-Key: abc123`

Request:
```json
{
  "amount": "5.00"
}
```

Response `409`:
```json
{
  "success": false,
  "error": {
    "code": "DUPLICATE_IDEMPOTENCY_KEY",
    "message": "Idempotency-Key was already used with a different request payload."
  }
}
```

#### Missing key

`POST /api/wallets/{wallet_id}/deposits`

Response `400`:
```json
{
  "success": false,
  "error": {
    "code": "IDEMPOTENCY_KEY_REQUIRED",
    "message": "The Idempotency-Key header is required."
  }
}
```

### Withdrawals

#### Create withdrawal

`POST /api/wallets/{wallet_id}/withdrawals`

Headers: `Authorization: Bearer <access_token>`, `Idempotency-Key: <unique key>`

Request:
```json
{
  "amount": "100.00",
  "destination": "HDFC ****1234"
}
```

Response `201`:
```json
{
  "id": "72e1306d-add2-492d-bd87-5c410df5e31a",
  "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "amount": "100.00",
  "currency": "INR",
  "status": "COMPLETED",
  "reference_number": "WDR-127A39E1044E46",
  "destination": "HDFC ****1234",
  "failure_reason": null,
  "created_at": "2026-10-12T03:46:10.443448Z",
  "completed_at": "2026-10-12T03:46:10.462662Z"
}
```

### Transfers

#### Create transfer

`POST /api/transfers`

Headers: `Authorization: Bearer <access_token>`, `Idempotency-Key: <unique key>`

Request:
```json
{
  "sender_wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "receiver_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
  "amount": "300.00",
  "currency": "INR",
  "reference": "Dinner"
}
```

Response `201`:
```json
{
  "id": "56ebcc2f-a2b6-4051-b075-01530851a4c9",
  "sender_wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "receiver_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
  "amount": "300.00",
  "currency": "INR",
  "status": "COMPLETED",
  "reference_number": "TRF-91A173BC97D348",
  "reference": "Dinner",
  "failure_reason": null,
  "created_at": "2026-10-12T03:46:10.364465Z",
  "completed_at": "2026-10-12T03:46:10.389392Z"
}
```

#### Insufficient balance (a FAILED transfer record is kept; no money moves)

`POST /api/transfers`

Headers: `Authorization: Bearer <access_token>`, `Idempotency-Key: <unique key>`

Request:
```json
{
  "sender_wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "receiver_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
  "amount": "9000.00",
  "currency": "INR",
  "reference": "Dinner"
}
```

Response `400`:
```json
{
  "success": false,
  "error": {
    "code": "INSUFFICIENT_BALANCE",
    "message": "Insufficient wallet balance."
  }
}
```

#### Transfer to yourself

`POST /api/transfers`

Headers: `Authorization: Bearer <access_token>`, `Idempotency-Key: <unique key>`

Response `400`:
```json
{
  "success": false,
  "error": {
    "code": "INVALID_TRANSFER",
    "message": "You cannot transfer money to yourself."
  }
}
```

#### Receiver wallet frozen

`POST /api/transfers`

Headers: `Authorization: Bearer <access_token>`, `Idempotency-Key: <unique key>`

Response `409`:
```json
{
  "success": false,
  "error": {
    "code": "WALLET_FROZEN",
    "message": "Receiver wallet is frozen."
  }
}
```

#### Suspicious amount: held for review, funds reserved (202)

`POST /api/transfers`

The sender's `available_balance` is unchanged but `pending_balance` rises by the held amount, so the money cannot be spent twice.

Headers: `Authorization: Bearer <access_token>`, `Idempotency-Key: <unique key>`

Request:
```json
{
  "sender_wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "receiver_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
  "amount": "600.00",
  "currency": "INR",
  "reference": "Dinner"
}
```

Response `202`:
```json
{
  "id": "509ce7e7-613f-45fc-b986-d9bc7649f3b4",
  "sender_wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "receiver_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
  "amount": "600.00",
  "currency": "INR",
  "status": "PENDING",
  "reference_number": "TRF-351C860CAD8443",
  "reference": "Dinner",
  "failure_reason": null,
  "created_at": "2026-10-12T03:46:10.684107Z",
  "completed_at": null
}
```

#### Sender wallet while the transfer is held

`GET /api/wallets/{wallet_id}`

Headers: `Authorization: Bearer <access_token>`

Response `200`:
```json
{
  "id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "user_id": "57a6dcc2-2d2b-4fa1-968c-6a8729f81cc3",
  "name": "Savings",
  "currency": "INR",
  "available_balance": "900.00",
  "pending_balance": "600.00",
  "status": "ACTIVE",
  "created_at": "2026-10-12T03:46:10.227515Z",
  "updated_at": "2026-10-12T03:46:10.227515Z"
}
```

### Suspicious transactions (staff)

#### List flagged transactions

`GET /api/admin/suspicious-transactions?review_status=pending`

Response `200`:
```json
{
  "items": [
    {
      "id": "72490545-a720-4786-b427-2d24f92b2edd",
      "transaction_id": "a65d8358-d60d-4961-803b-7aab55836c7a",
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "risk_status": "REVIEW_REQUIRED",
      "reasons": [
        "AMOUNT_ABOVE_THRESHOLD"
      ],
      "review_status": "PENDING",
      "reviewed_by": null,
      "reviewed_at": null,
      "review_notes": null,
      "created_at": "2026-10-12T03:46:10.684107Z"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20,
  "pages": 1
}
```

#### Approve - releases the held funds and completes the transfer

`POST /api/admin/suspicious-transactions/{id}/review`

Request:
```json
{
  "decision": "APPROVE",
  "notes": "Customer verified"
}
```

Response `200`:
```json
{
  "id": "72490545-a720-4786-b427-2d24f92b2edd",
  "transaction_id": "a65d8358-d60d-4961-803b-7aab55836c7a",
  "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "risk_status": "REVIEW_REQUIRED",
  "reasons": [
    "AMOUNT_ABOVE_THRESHOLD"
  ],
  "review_status": "APPROVED",
  "reviewed_by": "1d61c92a-095c-4b1f-8e93-8d824106f8f1",
  "reviewed_at": "2026-10-12T03:46:10.751500Z",
  "review_notes": "Customer verified",
  "created_at": "2026-10-12T03:46:10.684107Z"
}
```

### Refunds

#### Request refund (sender of a COMPLETED transfer)

`POST /api/refunds`

Headers: `Authorization: Bearer <access_token>`, `Idempotency-Key: <unique key>`

Request:
```json
{
  "transaction_id": "4f0d6192-f9f1-4727-aab8-d2dc02f76e60",
  "amount": "300.00",
  "reason": "Wrong recipient"
}
```

Response `201`:
```json
{
  "id": "3c268742-a338-4124-b582-67c54d585ba9",
  "transaction_id": "4f0d6192-f9f1-4727-aab8-d2dc02f76e60",
  "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "requested_by": "57a6dcc2-2d2b-4fa1-968c-6a8729f81cc3",
  "amount": "300.00",
  "currency": "INR",
  "reason": "Wrong recipient",
  "status": "REQUESTED",
  "reference_number": "RFD-CFCF838BA3EE41",
  "reviewed_by": null,
  "reviewed_at": null,
  "review_notes": null,
  "failure_reason": null,
  "created_at": "2026-10-12T03:46:10.537475Z",
  "completed_at": null
}
```

#### Approve (staff) - posts two REFUND ledger entries

`POST /api/refunds/{refund_id}/approve`

Request:
```json
{
  "notes": "Verified"
}
```

Response `200`:
```json
{
  "id": "3c268742-a338-4124-b582-67c54d585ba9",
  "transaction_id": "4f0d6192-f9f1-4727-aab8-d2dc02f76e60",
  "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
  "requested_by": "57a6dcc2-2d2b-4fa1-968c-6a8729f81cc3",
  "amount": "300.00",
  "currency": "INR",
  "reason": "Wrong recipient",
  "status": "COMPLETED",
  "reference_number": "RFD-CFCF838BA3EE41",
  "reviewed_by": "1d61c92a-095c-4b1f-8e93-8d824106f8f1",
  "reviewed_at": "2026-10-12T03:46:10.580879Z",
  "review_notes": "Verified",
  "failure_reason": null,
  "created_at": "2026-10-12T03:46:10.537475Z",
  "completed_at": "2026-10-12T03:46:10.588284Z"
}
```

### Transactions & ledger

#### Search / filter / paginate

`GET /api/transactions?type=transfer&status=completed&min_amount=100&page=1&page_size=20`

Headers: `Authorization: Bearer <access_token>`

Response `200`:
```json
{
  "items": [
    {
      "id": "4f0d6192-f9f1-4727-aab8-d2dc02f76e60",
      "type": "TRANSFER",
      "status": "COMPLETED",
      "risk_status": "NORMAL",
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "counterparty_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
      "amount": "300.00",
      "currency": "INR",
      "reference_number": "TRF-91A173BC97D348",
      "reference_entity": "transfer",
      "reference_id": "56ebcc2f-a2b6-4051-b075-01530851a4c9",
      "description": "Dinner",
      "created_at": "2026-10-12T03:46:10.364465Z",
      "completed_at": "2026-10-12T03:46:10.389392Z"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20,
  "pages": 1
}
```

#### Immutable ledger entries of one transaction

`GET /api/transactions/{id}/ledger`

Headers: `Authorization: Bearer <access_token>`

Response `200`:
```json
[
  {
    "id": "0055451b-3069-4875-8b1c-ec904da0046b",
    "wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
    "transaction_id": "4f0d6192-f9f1-4727-aab8-d2dc02f76e60",
    "transaction_type": "TRANSFER_CREDIT",
    "direction": "CREDIT",
    "amount": "300.00",
    "balance_before": "0.00",
    "balance_after": "300.00",
    "reference_entity": "transfer",
    "reference_id": "56ebcc2f-a2b6-4051-b075-01530851a4c9",
    "description": "Transfer TRF-91A173BC97D348 from wallet 069ce7f8-f341-4922-a2e9-9d1538ba87c4",
    "created_by": "57a6dcc2-2d2b-4fa1-968c-6a8729f81cc3",
    "created_at": "2026-10-12T03:46:10.364465Z",
    "metadata": {
      "counterparty_wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4"
    }
  },
  {
    "id": "e5c405c4-4db0-4329-b82b-da423078df31",
    "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
    "transaction_id": "4f0d6192-f9f1-4727-aab8-d2dc02f76e60",
    "transaction_type": "TRANSFER_DEBIT",
    "direction": "DEBIT",
    "amount": "300.00",
    "balance_before": "1000.00",
    "balance_after": "700.00",
    "reference_entity": "transfer",
    "reference_id": "56ebcc2f-a2b6-4051-b075-01530851a4c9",
    "description": "Transfer TRF-91A173BC97D348 to wallet 9751dc23-ceda-431d-97db-283aa4d24fd0",
    "created_by": "57a6dcc2-2d2b-4fa1-968c-6a8729f81cc3",
    "created_at": "2026-10-12T03:46:10.364465Z",
    "metadata": {
      "counterparty_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0"
    }
  }
]
```

### Notifications

#### List notifications

`GET /api/notifications`

Response `200`:
```json
{
  "items": [
    {
      "id": "447acd26-5e92-4dc9-a1e7-973ecf9f87bc",
      "type": "TRANSFER_RECEIVED",
      "title": "Transfer received",
      "message": "You received INR 300.00.",
      "is_read": false,
      "read_at": null,
      "created_at": "2026-10-12T03:46:10.364465Z",
      "metadata": {
        "transfer_id": "56ebcc2f-a2b6-4051-b075-01530851a4c9"
      }
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 2,
  "pages": 1
}
```

### Limits & administration

#### Get limits

`GET /api/admin/transaction-limits`

Response `200`:
```json
{
  "max_transaction_amount": "100000.00",
  "daily_transfer_limit": "200000.00",
  "daily_withdrawal_limit": "100000.00",
  "max_transactions_per_day": 50,
  "suspicious_amount_threshold": "25000.00",
  "velocity_window_minutes": 10,
  "velocity_review_count": 5,
  "velocity_block_count": 10,
  "frequency_review_threshold": 20,
  "recently_frozen_hours": 24,
  "updated_by": null,
  "updated_at": "2026-10-12T03:46:09.563632Z"
}
```

#### Update limits (audited)

`PATCH /api/admin/transaction-limits`

Request:
```json
{
  "daily_transfer_limit": "150000.00",
  "suspicious_amount_threshold": "500.00"
}
```

Response `200`:
```json
{
  "max_transaction_amount": "100000.00",
  "daily_transfer_limit": "150000.00",
  "daily_withdrawal_limit": "100000.00",
  "max_transactions_per_day": 50,
  "suspicious_amount_threshold": "500.00",
  "velocity_window_minutes": 10,
  "velocity_review_count": 5,
  "velocity_block_count": 10,
  "frequency_review_threshold": 20,
  "recently_frozen_hours": 24,
  "updated_by": "16a01f81-0429-4149-98f4-146d71c6dda0",
  "updated_at": "2026-10-12T03:46:10.674443Z"
}
```

#### Audit logs

`GET /api/admin/audit-logs`

Response `200`:
```json
{
  "items": [
    {
      "id": "7bde5265-041b-48c1-8ed2-e304aaaa538c",
      "user_id": "16a01f81-0429-4149-98f4-146d71c6dda0",
      "action": "TRANSACTION_LIMIT_CHANGED",
      "entity": "transaction_limits",
      "entity_id": "1",
      "ip_address": "testclient",
      "metadata": {
        "after": {
          "daily_transfer_limit": "150000.00",
          "suspicious_amount_threshold": "500.00"
        },
        "before": {
          "daily_transfer_limit": "200000.00",
          "suspicious_amount_threshold": "25000.00"
        }
      },
      "created_at": "2026-10-12T03:46:10.674443Z"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 1,
  "pages": 1
}
```

#### Run a scheduled job on demand

`POST /api/admin/jobs/{job_name}/run`

Response `200`:
```json
{
  "success": true,
  "job": "suspicious_scan",
  "result": {
    "flagged": 0
  }
}
```

### Dashboards

#### Customer

`GET /api/dashboard/customer`

Response `200`:
```json
{
  "wallet_balances": [
    {
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "name": "Savings",
      "currency": "INR",
      "status": "ACTIVE",
      "available_balance": "300.00",
      "pending_balance": "0.00"
    }
  ],
  "total_balance": {
    "INR": "300.00"
  },
  "recent_transactions": [
    {
      "id": "a65d8358-d60d-4961-803b-7aab55836c7a",
      "type": "TRANSFER",
      "status": "COMPLETED",
      "risk_status": "NORMAL",
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "counterparty_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
      "amount": "600.00",
      "currency": "INR",
      "reference_number": "TRF-351C860CAD8443",
      "reference_entity": "transfer",
      "reference_id": "509ce7e7-613f-45fc-b986-d9bc7649f3b4",
      "description": "Dinner",
      "created_at": "2026-10-12T03:46:10.684107Z",
      "completed_at": "2026-10-12T03:46:10.749972Z"
    },
    {
      "id": "4591ea69-eecc-49cd-b77a-ba5e760c3868",
      "type": "REFUND",
      "status": "COMPLETED",
      "risk_status": "NORMAL",
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "counterparty_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
      "amount": "300.00",
      "currency": "INR",
      "reference_number": "RFD-CFCF838BA3EE41",
      "reference_entity": "refund",
      "reference_id": "3c268742-a338-4124-b582-67c54d585ba9",
      "description": "Refund of TRF-91A173BC97D348",
      "created_at": "2026-10-12T03:46:10.575360Z",
      "completed_at": "2026-10-12T03:46:10.588284Z"
    },
    {
      "id": "a1a15c3e-49a6-4d65-b4a0-3d7961152ee6",
      "type": "WITHDRAWAL",
      "status": "COMPLETED",
      "risk_status": "NORMAL",
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "counterparty_wallet_id": null,
      "amount": "100.00",
      "currency": "INR",
      "reference_number": "WDR-127A39E1044E46",
      "reference_entity": "withdrawal",
      "reference_id": "72e1306d-add2-492d-bd87-5c410df5e31a",
      "description": "Withdrawal to HDFC ****1234",
      "created_at": "2026-10-12T03:46:10.443448Z",
      "completed_at": "2026-10-12T03:46:10.462662Z"
    },
    {
      "id": "6333d822-2ede-4c59-837a-07271df252a6",
      "type": "TRANSFER",
      "status": "FAILED",
      "risk_status": "NORMAL",
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "counterparty_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
      "amount": "9000.00",
      "currency": "INR",
      "reference_number": "TRF-D363FB957EF441",
      "reference_entity": "transfer",
      "reference_id": "d9171361-7526-4731-80fd-fa69bc73aca1",
      "description": "Dinner",
      "created_at": "2026-10-12T03:46:10.400707Z",
      "completed_at": null
    },
    {
      "id": "4f0d6192-f9f1-4727-aab8-d2dc02f76e60",
      "type": "TRANSFER",
      "status": "COMPLETED",
      "risk_status": "NORMAL",
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "counterparty_wallet_id": "9751dc23-ceda-431d-97db-283aa4d24fd0",
      "amount": "300.00",
      "currency": "INR",
      "reference_number": "TRF-91A173BC97D348",
      "reference_entity": "transfer",
      "reference_id": "56ebcc2f-a2b6-4051-b075-01530851a4c9",
      "description": "Dinner",
      "created_at": "2026-10-12T03:46:10.364465Z",
      "completed_at": "2026-10-12T03:46:10.389392Z"
    },
    {
      "id": "7912bc91-32c3-4d26-9a98-ff03761cd1db",
      "type": "DEPOSIT",
      "status": "COMPLETED",
      "risk_status": "NORMAL",
      "wallet_id": "069ce7f8-f341-4922-a2e9-9d1538ba87c4",
      "counterparty_wallet_id": null,
      "amount": "1000.00",
      "currency": "INR",
      "reference_number": "DEP-656560794FFD4A",
      "reference_entity": "deposit",
      "reference_id": "a9b634dd-6ee9-4088-b880-a600d465a977",
      "description": "Wallet deposit",
      "created_at": "2026-10-12T03:46:10.251913Z",
      "completed_at": "2026-10-12T03:46:10.283627Z"
    }
  ],
  "total_deposits": {
    "INR": "1000.00"
  },
  "total_transfers": {
    "INR": "900.00"
  },
  "total_withdrawals": {
    "INR": "100.00"
  },
  "pending_transactions": 0
}
```

#### Support

`GET /api/dashboard/support`

Response `200`:
```json
{
  "pending_reviews": 0,
  "suspicious_transactions": 1,
  "failed_transactions": 1,
  "refund_requests": 0,
  "frozen_wallets": 0
}
```

#### Admin

`GET /api/dashboard/admin`

Response `200`:
```json
{
  "total_users": 4,
  "active_wallets": 2,
  "total_wallet_balance": {
    "INR": "900.00"
  },
  "todays_transactions": 6,
  "todays_transaction_volume": {
    "INR": "2300.00"
  },
  "failed_transactions": 1,
  "suspicious_transactions": 1
}
```

### Errors

#### Validation error (422)

`POST /api/wallets`

Request:
```json
{
  "currency": "INR",
  "name": ""
}
```

Response `422`:
```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed.",
    "details": [
      {
        "field": "name",
        "message": "String should have at least 1 character",
        "type": "string_too_short"
      }
    ]
  }
}
```

#### Forbidden (403)

`GET /api/admin/transaction-limits`

Response `403`:
```json
{
  "success": false,
  "error": {
    "code": "ROLE_NOT_ALLOWED",
    "message": "Your role is not allowed to access this resource."
  }
}
```

#### Unauthenticated (401)

`GET /api/wallets`

Response `401`:
```json
{
  "success": false,
  "error": {
    "code": "NOT_AUTHENTICATED",
    "message": "Authentication required."
  }
}
```

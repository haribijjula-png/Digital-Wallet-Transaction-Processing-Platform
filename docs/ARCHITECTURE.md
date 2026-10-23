# Architecture

## Layered design

Business rules live in the **service / domain layer**. Route handlers only translate HTTP <-> use-cases;
repositories only contain queries and row locking.

```mermaid
flowchart TD
    C[Client / Postman / Swagger] -->|HTTPS + JWT| R

    subgraph API["API layer  (app/api)"]
        R[Routes<br/>request parsing, response models, status codes]
        D[Dependencies<br/>get_db, get_current_user, require_roles,<br/>idempotency_key, page_params, enum_param]
    end

    subgraph SVC["Service / domain layer  (app/services, app/domain)"]
        S1[AuthService]
        S2[WalletService]
        S3[Deposit / Withdrawal /<br/>Transfer / Refund services]
        S4[PolicyService<br/>limits + suspicious rules]
        S5[LedgerService<br/>ONLY place balances change]
        S6[IdempotencyService]
        S7[Audit / Notification services]
        SM[State machines<br/>wallet, tx, refund]
    end

    subgraph REPO["Repository layer  (app/repositories)"]
        RP[Users, Wallets, Transactions,<br/>Ledger, Refunds, Limits, Audit ...<br/>SELECT ... FOR UPDATE, pagination]
    end

    subgraph DATA["Persistence  (app/models, alembic)"]
        M[SQLAlchemy models]
        PG[(PostgreSQL<br/>CHECK / UNIQUE / FK / triggers)]
    end

    subgraph BG["Background processing"]
        BT[FastAPI BackgroundTasks<br/>notification delivery]
        JB[Scheduled jobs  app/jobs/tasks.py<br/>Celery Beat optional]
    end

    R --> D --> S1 & S2 & S3
    S3 --> S4 & S5 & S6 & S7 & SM
    S2 --> SM
    S1 & S2 & S3 & S4 & S5 & S6 & S7 --> RP --> M --> PG
    R -.after response.-> BT
    JB --> S3
    JB --> RP
    EX[Centralised exception handlers<br/>consistent error envelope] -.-> R
```

## Anatomy of a transfer (one DB transaction)

```mermaid
sequenceDiagram
    autonumber
    participant Cl as Client
    participant Rt as Route
    participant Tr as TransferService
    participant Id as IdempotencyService
    participant Db as PostgreSQL
    Cl->>Rt: POST /api/transfers (Idempotency-Key)
    Rt->>Tr: create(user, payload, key)
    Tr->>Id: begin(user, scope, key, payload)
    Id->>Db: INSERT idempotency_keys ON CONFLICT DO NOTHING
    alt key already completed
        Id-->>Cl: replay stored response (Idempotent-Replayed: true)
    else new key
        Tr->>Db: SELECT wallets ... ORDER BY id FOR UPDATE (both wallets)
        Tr->>Tr: validate status, currency, ownership, self-transfer
        Tr->>Tr: PolicyService: limits + suspicious rules (under the lock)
        alt blocked / insufficient balance
            Tr->>Db: INSERT FAILED transfer + audit + notification + key response
            Tr->>Db: COMMIT
            Tr-->>Cl: 4xx domain error (sender balance untouched)
        else needs review
            Tr->>Db: reserve funds (pending_balance += amount), flag, notify staff
            Tr->>Db: COMMIT
            Tr-->>Cl: 202 PENDING
        else normal
            Tr->>Db: debit sender + credit receiver + 2 ledger rows
            Tr->>Db: transfer/transaction COMPLETED, notifications, audit, key response
            Tr->>Db: COMMIT  (all or nothing)
            Tr-->>Cl: 201 COMPLETED
        end
    end
    Rt-)Rt: BackgroundTask: deliver notifications
```

## State machines

```mermaid
stateDiagram-v2
    direction LR
    state "Transfer / Withdrawal / Deposit" as T {
        [*] --> PENDING
        PENDING --> PROCESSING
        PENDING --> CANCELLED: cancel / stale cleanup
        PENDING --> FAILED
        PROCESSING --> COMPLETED
        PROCESSING --> FAILED
        FAILED --> PROCESSING: bounded retry (GATEWAY_TIMEOUT only)
    }
```

```mermaid
stateDiagram-v2
    direction LR
    [*] --> ACTIVE
    ACTIVE --> FROZEN
    ACTIVE --> SUSPENDED
    ACTIVE --> CLOSED
    FROZEN --> ACTIVE: unfreeze (staff)
    FROZEN --> SUSPENDED
    FROZEN --> CLOSED
    SUSPENDED --> ACTIVE
    SUSPENDED --> FROZEN
    SUSPENDED --> CLOSED
    CLOSED --> [*]
```

```mermaid
stateDiagram-v2
    direction LR
    [*] --> REQUESTED
    REQUESTED --> UNDER_REVIEW
    REQUESTED --> APPROVED
    REQUESTED --> REJECTED
    UNDER_REVIEW --> APPROVED
    UNDER_REVIEW --> REJECTED
    APPROVED --> COMPLETED: ledger entries posted
    APPROVED --> FAILED: closed wallet / insufficient funds
```

## Request-level security pipeline

1. `HTTPBearer` extracts the token -> `AuthService.authenticate` verifies signature, expiry, **type = access**,
   that the user exists and **is active**, and that the **session (token family) has not been revoked**.
2. `require_roles(...)` enforces **role-based** access (403 `ROLE_NOT_ALLOWED`).
3. Services enforce **resource-based** rules: wallet ownership, transfer participants, refund requester, who may
   cancel what, who may change which wallet status (403 `*_ACCESS_DENIED`).
4. Business invariants (state machines, limits, balances) are enforced in services and re-checked by DB constraints.

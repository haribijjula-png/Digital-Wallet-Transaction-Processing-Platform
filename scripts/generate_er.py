"""Generates docs/ER_DIAGRAM.md (Mermaid ER diagram + constraint/index catalogue) from the
SQLAlchemy metadata, so the diagram can never drift from the real schema.

    python -m scripts.generate_er
"""
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, UniqueConstraint

import app.models  # noqa: F401
from app.core.database import Base

ROOT = Path(__file__).resolve().parent.parent


def type_name(t) -> str:
    if isinstance(t, sa.Uuid):
        return "uuid"
    if isinstance(t, sa.Enum):
        return "enum"
    if isinstance(t, sa.Numeric):
        return f"numeric_{t.precision}_{t.scale}"
    if isinstance(t, sa.DateTime):
        return "timestamptz"
    if isinstance(t, sa.Boolean):
        return "bool"
    if isinstance(t, sa.Integer):
        return "int"
    if isinstance(t, sa.Text):
        return "text"
    if isinstance(t, sa.String):
        return f"varchar_{t.length}" if t.length else "varchar"
    return type(t).__name__.lower()


def diagram() -> str:
    lines = ["erDiagram"]
    tables = sorted(Base.metadata.tables.values(), key=lambda t: t.name)
    for t in tables:
        lines.append(f"    {t.name} {{")
        unique_cols = {c.name for c in t.columns if c.unique}
        for uc in t.constraints:
            if isinstance(uc, UniqueConstraint) and len(uc.columns) == 1:
                unique_cols.add(list(uc.columns)[0].name)
        for c in t.columns:
            keys = []
            if c.primary_key:
                keys.append("PK")
            if c.foreign_keys:
                keys.append("FK")
            if c.name in unique_cols and not c.primary_key:
                keys.append("UK")
            lines.append(f"        {type_name(c.type)} {c.name}" + (f" {', '.join(keys)}" if keys else ""))
        lines.append("    }")
    for t in tables:
        for c in t.columns:
            for fk in c.foreign_keys:
                parent = fk.column.table.name
                one_to_one = c.primary_key and len(t.primary_key.columns) == 1 or c.unique
                left = "||" if not c.nullable else "|o"
                right = "o|" if one_to_one else "o{"
                lines.append(f'    {parent} {left}--{right} {t.name} : "{c.name}"')
    return "\n".join(lines)


def catalogue() -> str:
    out = []
    for t in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        items = []
        for con in sorted(t.constraints, key=lambda x: str(x.name)):
            if isinstance(con, CheckConstraint) and con.name:
                sql = str(con.sqltext).replace("\n", " ")
                if "POSTCOMPILE" in sql:
                    sql = "status/type is one of the enum values"
                    items.append(f"CHECK `{con.name}`: {sql}")
                else:
                    items.append(f"CHECK `{con.name}`: `{sql}`")
            elif isinstance(con, UniqueConstraint):
                items.append("UNIQUE (" + ", ".join(c.name for c in con.columns) + ")")
        for c in t.columns:
            if c.unique and f"UNIQUE ({c.name})" not in items:
                items.append(f"UNIQUE ({c.name})")
        for fk in t.foreign_key_constraints:
            od = fk.ondelete or "NO ACTION"
            items.append(f"FK ({', '.join(c.name for c in fk.columns)}) -> {fk.elements[0].column.table.name} ON DELETE {od}")
        for ix in sorted(t.indexes, key=lambda i: i.name):
            kind = "UNIQUE INDEX" if ix.unique else "INDEX"
            where = ix.dialect_options["postgresql"].get("where")
            cols = ", ".join(str(getattr(e, "name", e)) for e in ix.expressions)
            items.append(f"{kind} `{ix.name}` ({cols})" + (f" WHERE {where}" if where is not None else ""))
        out.append(f"**`{t.name}`**\n" + "\n".join(f"- {i}" for i in items) + "\n")
    return "\n".join(out)


HEADER = """# ER Diagram & Database Design

> The diagram and the constraint catalogue below are **generated from the SQLAlchemy models**
> (`python -m scripts.generate_er`), so they always match the schema created by Alembic.
> GitHub renders the Mermaid block natively.

## Entity-relationship diagram

```mermaid
"""

DESIGN = """
## Design explanation

**Identity & access** - `users` (role FK to `roles`, case-insensitive unique e-mail via a `lower(email)` index,
`is_active` flag) and `refresh_tokens`. Each login creates a *token family* (`family_id` = session id). Refresh tokens
are single-use: rotating marks the old row `revoked_at` and links `replaced_by_jti`. Presenting an already-revoked
token revokes the whole family (theft detection). Access tokens carry the family id, so logout / password change /
deactivation invalidate them immediately, not only when they expire.

**Wallets & balances** - `wallets` hold identity and status (`ACTIVE | FROZEN | SUSPENDED | CLOSED`, enforced by a
CHECK constraint **and** a transition table in `app/domain/state_machines.py`). Money lives in a separate
`wallet_balances` row (1:1) with `available_balance` and `pending_balance`. `pending_balance` is the part of
`available_balance` *reserved* for outgoing transactions that are held for review, so
`spendable = available - pending`. Three CHECK constraints make illegal states unrepresentable:
`available >= 0`, `pending >= 0`, `pending <= available`.

**Money movement** - `deposits`, `withdrawals`, `transfers` carry domain-specific fields and their own status. Every one
of them is mirrored by exactly one row in the unified **`transactions`** table (type, status, risk status, wallet,
counterparty, amount, reference). This is what history, filtering, limits and monitoring query, and what refunds
point at (`reference_entity` + `reference_id` tie it back to the domain row). Refunds and manual adjustments also
create a `transactions` row.

**Immutable ledger** - `transaction_ledger` is append-only: one row per available-balance change, with
`balance_before`, `balance_after`, direction and a JSONB `metadata`. A CHECK constraint enforces the arithmetic
(`CREDIT: after = before + amount`, `DEBIT: after = before - amount`). A `BEFORE UPDATE OR DELETE` trigger rejects
mutation at the database level (`audit_logs` has the same trigger) and an ORM event listener rejects it in the
application. Balance == sum(credits) - sum(debits) for every wallet, which the concurrency tests assert.

**Refunds** - a **partial unique index** (`uq_refunds_one_active_per_transaction`) allows at most one *live*
(`REQUESTED/UNDER_REVIEW/APPROVED/COMPLETED`) refund per original transaction. Duplicate refunds are therefore
impossible even under a race; `REJECTED`/`FAILED` refunds free the slot for a new request.

**Limits & monitoring** - `transaction_limits` is a singleton row (`CHECK id = 1`) holding limits *and* the
suspicious-activity thresholds, editable by admins (changes are audited with before/after values).
`suspicious_transactions` records each flagged transaction with its reasons and review state.

**Idempotency** - `idempotency_keys` has `UNIQUE (user_id, scope, key)`, the SHA-256 of the request payload and the
stored response. It is written in the *same DB transaction* as the money movement, so a key exists if and only if the
movement committed.

**Cross-cutting** - `notifications` (per-user, `delivered_at` set by the background dispatcher) and `audit_logs`
(user, action, entity, entity id, IP, JSONB metadata). All primary keys are UUIDs; all money columns are
`NUMERIC(18,2)`; timestamps are `timestamptz` with server defaults; `updated_at` uses `onupdate`.

**Cascading rules** - financial history is protected: wallets, transactions, ledger, refunds use `ON DELETE RESTRICT`
(you cannot delete a user/wallet that has financial records). `wallet_balances` cascades from its wallet,
`refresh_tokens`/`notifications`/`idempotency_keys` cascade from the user, and audit/creator references use
`SET NULL` so deleting an account never destroys the audit trail.

## Constraint & index catalogue

"""


def main() -> None:
    out = ROOT / "docs" / "ER_DIAGRAM.md"
    out.write_text(HEADER + diagram() + "\n```\n" + DESIGN + catalogue())
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

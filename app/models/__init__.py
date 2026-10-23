from sqlalchemy import event

from app.core.exceptions import ImmutableRecordError
from app.models.entities import *  # noqa: F401,F403
from app.models.entities import AuditLog, LedgerEntry
from app.models.enums import *  # noqa: F401,F403


def _block_mutation(*_args, **_kwargs):
    raise ImmutableRecordError("Ledger and audit records are immutable and cannot be updated or deleted.")


# Application-level guard (the database also enforces this with triggers, see migration 0001).
for _model in (LedgerEntry, AuditLog):
    event.listen(_model, "before_update", _block_mutation)
    event.listen(_model, "before_delete", _block_mutation)

import uuid

from sqlalchemy.orm import Session

from app.models import AuditLog
from app.repositories.misc import AuditRepository
from app.services.common import json_safe


class AuditService:
    """Adds an audit record in the *caller's* DB transaction, so the audit trail is
    committed atomically with the operation it describes."""

    def __init__(self, db: Session):
        self.repo = AuditRepository(db)

    def log(self, action: str, *, user_id: uuid.UUID | None = None, entity: str | None = None,
            entity_id=None, ip: str | None = None, meta: dict | None = None) -> None:
        self.repo.add(AuditLog(user_id=user_id, action=action, entity=entity,
                               entity_id=str(entity_id) if entity_id is not None else None,
                               ip_address=ip, meta=json_safe(meta or {})))

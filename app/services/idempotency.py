import hashlib
import json
import uuid

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, DuplicateIdempotencyKeyError, error_body
from app.models import IdempotencyKey
from app.models.enums import IdempotencyState
from app.repositories.misc import IdempotencyRepository
from app.services.common import ApiResult


class IdempotencyService:
    """Guarantees a retried request (same user + endpoint scope + Idempotency-Key) never
    creates a second financial transaction.

    * The key row is inserted in the SAME DB transaction as the money movement, so the
      key exists iff the movement committed.
    * Unique(user_id, scope, key) + INSERT .. ON CONFLICT DO NOTHING serialises concurrent
      duplicates: the loser waits for the winner, then replays its stored response.
    * Same key + different payload => 409 DUPLICATE_IDEMPOTENCY_KEY."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = IdempotencyRepository(db)

    @staticmethod
    def hash_payload(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def begin(self, user_id: uuid.UUID, scope: str, key: str, payload: dict) -> tuple[IdempotencyKey, ApiResult | None]:
        request_hash = self.hash_payload(payload)
        row_id = self.repo.try_insert(user_id, scope, key, request_hash)
        if row_id:
            return self.repo.get(row_id), None
        existing = self.repo.find(user_id, scope, key)
        if existing is None:  # extremely unlikely (row removed by cleanup between statements)
            raise AppError("Please retry the request.", code="IDEMPOTENCY_RETRY", status_code=409)
        if existing.request_hash != request_hash:
            raise DuplicateIdempotencyKeyError()
        if existing.state != IdempotencyState.COMPLETED:
            raise DuplicateIdempotencyKeyError("A request with this Idempotency-Key is still being processed.")
        return existing, ApiResult(existing.response_status, existing.response_body, replayed=True,
                                   headers={"Idempotent-Replayed": "true"})

    def complete(self, row: IdempotencyKey, status_code: int, body: dict, resource_id: uuid.UUID | None = None):
        row.state = IdempotencyState.COMPLETED
        row.response_status = status_code
        row.response_body = body
        row.resource_id = resource_id

    def complete_with_error(self, row: IdempotencyKey, err: AppError) -> None:
        self.complete(row, err.status_code, error_body(err))

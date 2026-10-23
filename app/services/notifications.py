import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models import Notification, NotificationType, User
from app.repositories.base import PageParams
from app.repositories.misc import NotificationRepository
from app.services.common import json_safe

logger = logging.getLogger("wallet.notifications")


class NotificationService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = NotificationRepository(db)

    def notify(self, user_id: uuid.UUID, type_: NotificationType, title: str, message: str,
               meta: dict | None = None) -> None:
        """Stored in the caller's transaction (never lost / never sent for rolled-back work).
        Delivery (e-mail/push simulation) happens afterwards in a BackgroundTask."""
        self.repo.add(Notification(user_id=user_id, type=type_, title=title, message=message,
                                   meta=json_safe(meta or {})))

    def notify_many(self, user_ids, type_: NotificationType, title: str, message: str, meta: dict | None = None):
        for user_id in set(user_ids):
            self.notify(user_id, type_, title, message, meta)

    # ---- API use-cases
    def list(self, user: User, params: PageParams, unread_only: bool = False):
        return self.repo.list(params, user.id, unread_only)

    def mark_read(self, user: User, notification_id: uuid.UUID) -> Notification:
        n = self.repo.get(notification_id)
        if not n:
            raise NotFoundError("Notification not found.", code="NOTIFICATION_NOT_FOUND")
        if n.user_id != user.id:
            raise ForbiddenError("This notification belongs to another user.")
        if not n.is_read:
            n.is_read = True
            n.read_at = datetime.now(timezone.utc)
            self.db.commit()
        return n

    def mark_all_read(self, user: User) -> int:
        count = self.repo.mark_all_read(user.id, datetime.now(timezone.utc))
        self.db.commit()
        return count


def deliver_pending_notifications(limit: int = 200) -> int:
    """BackgroundTask: simulates e-mail/push delivery for undelivered notifications.
    Uses its own session because it runs after the request has finished."""
    with SessionLocal() as db:
        pending = db.scalars(select(Notification).where(Notification.delivered_at.is_(None))
                             .order_by(Notification.created_at).limit(limit).with_for_update(skip_locked=True)).all()
        for n in pending:
            logger.info("notify user=%s type=%s title=%s", n.user_id, n.type.value, n.title)
            n.delivered_at = datetime.now(timezone.utc)
        db.commit()
        return len(pending)

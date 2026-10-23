import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, page_params
from app.core.database import get_db
from app.models import User
from app.repositories.base import PageParams, page_dict
from app.schemas.common import ErrorResponse, MessageResponse, Page
from app.schemas.wallets import NotificationOut
from app.services.notifications import NotificationService

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])
ERR = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}


@router.get("", response_model=Page[NotificationOut], responses=ERR, summary="List my notifications")
def list_notifications(unread_only: bool = False, params: PageParams = Depends(page_params),
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items, total = NotificationService(db).list(user, params, unread_only)
    return page_dict(items, total, params)


# NOTE: declared before "/{id}/read" so the literal path wins.
@router.patch("/read-all", response_model=MessageResponse, responses=ERR, summary="Mark all notifications as read")
def read_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    count = NotificationService(db).mark_all_read(user)
    return MessageResponse(message=f"{count} notification(s) marked as read.")


@router.patch("/{notification_id}/read", response_model=NotificationOut, responses=ERR,
              summary="Mark one notification as read")
def read_one(notification_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return NotificationService(db).mark_read(user, notification_id)

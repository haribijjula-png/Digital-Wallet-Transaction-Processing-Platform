import uuid
from collections.abc import Callable

from fastapi import BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import AppError, ForbiddenError, IdempotencyKeyRequiredError, UnauthorizedError
from app.models import RoleName, User
from app.models.enums import ADMIN_ROLES, STAFF_ROLES
from app.repositories.base import PageParams
from app.services.auth import AuthService
from app.services.common import ApiResult
from app.services.notifications import deliver_pending_notifications

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")


def get_current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                     db: Session = Depends(get_db)) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise UnauthorizedError("Authentication required.", code="NOT_AUTHENTICATED")
    return AuthService(db).authenticate(creds.credentials)


def require_roles(*roles: RoleName | set) -> Callable:
    allowed: set[RoleName] = set()
    for r in roles:
        allowed |= set(r) if isinstance(r, (set, frozenset, list, tuple)) else {r}

    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role_name not in allowed:
            raise ForbiddenError("Your role is not allowed to access this resource.", code="ROLE_NOT_ALLOWED")
        return user
    checker.allowed_roles = frozenset(allowed)   # introspected by the docs generator
    return checker


require_customer = require_roles(RoleName.CUSTOMER)
require_staff = require_roles(STAFF_ROLES)
require_admin = require_roles(ADMIN_ROLES)
require_super_admin = require_roles(RoleName.SUPER_ADMIN)


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def idempotency_key(key: str | None = Header(default=None, alias="Idempotency-Key",
                                             description="Unique key (e.g. a UUID) making retries safe.")) -> str:
    if key is None or not key.strip():
        raise IdempotencyKeyRequiredError()
    key = key.strip()
    if len(key) > 255:
        raise IdempotencyKeyRequiredError("Idempotency-Key must be at most 255 characters.")
    return key


def page_params(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                sort_by: str | None = Query(None, description="Column to sort by"),
                order: str = Query("desc", pattern="^(asc|desc)$")) -> PageParams:
    return PageParams(page=page, page_size=page_size, sort_by=sort_by, order=order)


def to_response(result: ApiResult, background: BackgroundTasks | None = None) -> JSONResponse:
    if background is not None:
        background.add_task(deliver_pending_notifications)
    return JSONResponse(status_code=result.status_code, content=result.body, headers=result.headers or None)


def enum_param(enum_cls, name: str):
    """Case-insensitive enum query parameter (``?type=transfer`` == ``?type=TRANSFER``)."""
    allowed = ", ".join(e.value for e in enum_cls)

    def dep(value: str | None = Query(None, alias=name, description=f"One of: {allowed} (case-insensitive)")):
        if value is None or value == "":
            return None
        try:
            return enum_cls(value.strip().upper())
        except ValueError:
            raise AppError(f"Invalid value '{value}' for '{name}'. Allowed: {allowed}.",
                           code="VALIDATION_ERROR", status_code=422)
    return dep

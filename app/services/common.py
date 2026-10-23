import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.models import RoleName, User
from app.models.enums import ADMIN_ROLES, STAFF_ROLES


@dataclass
class ApiResult:
    """Return type of idempotent use-cases: replays return the exact stored response."""
    status_code: int
    body: dict
    replayed: bool = False
    headers: dict = field(default_factory=dict)


def json_safe(data: Any) -> Any:
    return json.loads(json.dumps(data, default=str))


def is_staff(user: User) -> bool:
    return user.role_name in STAFF_ROLES


def is_admin(user: User) -> bool:
    return user.role_name in ADMIN_ROLES


def is_super_admin(user: User) -> bool:
    return user.role_name == RoleName.SUPER_ADMIN


def money(value) -> str:
    return f"{value:.2f}"


def uid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

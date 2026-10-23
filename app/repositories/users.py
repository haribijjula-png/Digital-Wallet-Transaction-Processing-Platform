from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import RefreshToken, Role, RoleName, User
from app.repositories.base import PageParams, paginate


class UserRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.db.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(func.lower(User.email) == email.lower()))

    def role_id(self, name: RoleName) -> int:
        return self.db.scalar(select(Role.id).where(Role.name == name))

    def add(self, user: User) -> User:
        self.db.add(user)
        self.db.flush()
        return user

    def list(self, params: PageParams, role: RoleName | None = None, is_active: bool | None = None,
             search: str | None = None):
        stmt = select(User)
        if role:
            stmt = stmt.join(Role, User.role_id == Role.id).where(Role.name == role)
        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)
        if search:
            like = f"%{search}%"
            stmt = stmt.where(User.email.ilike(like) | User.full_name.ilike(like))
        return paginate(self.db, stmt, params, {"created_at": User.created_at, "email": User.email,
                                                "full_name": User.full_name}, "created_at", User.id)

    def staff_ids(self) -> list[uuid.UUID]:
        return list(self.db.scalars(
            select(User.id).join(Role, User.role_id == Role.id)
            .where(Role.name != RoleName.CUSTOMER, User.is_active.is_(True))))

    def count(self) -> int:
        return self.db.scalar(select(func.count()).select_from(User)) or 0


class RefreshTokenRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, token: RefreshToken) -> RefreshToken:
        self.db.add(token)
        self.db.flush()
        return token

    def get_by_jti_for_update(self, jti: str) -> RefreshToken | None:
        return self.db.scalar(select(RefreshToken).where(RefreshToken.jti == jti).with_for_update()
                              .execution_options(populate_existing=True))

    def family_active(self, family_id: uuid.UUID, now: datetime) -> bool:
        return self.db.scalar(select(func.count()).select_from(RefreshToken).where(
            RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now)) > 0

    def revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        self.db.execute(update(RefreshToken).where(RefreshToken.family_id == family_id,
                                                   RefreshToken.revoked_at.is_(None)).values(revoked_at=now))

    def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime) -> None:
        self.db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id,
                                                   RefreshToken.revoked_at.is_(None)).values(revoked_at=now))

import uuid
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (create_access_token, create_refresh_token, decode_token, hash_password, utcnow,
                               verify_password)
from app.models import RefreshToken, RoleName, User
from app.repositories.users import RefreshTokenRepository, UserRepository
from app.schemas.auth import ChangePasswordRequest, LoginRequest, RegisterRequest
from app.services.audit import AuditService

# Used to equalise timing when the e-mail does not exist (avoid user enumeration by latency).
_DUMMY_HASH = hash_password("dummy-password-for-timing")


class AuthService:
    def __init__(self, db: Session):
        self.db = db
        self.users = UserRepository(db)
        self.tokens = RefreshTokenRepository(db)
        self.audit = AuditService(db)

    # ------------------------------------------------------------------ register
    def register(self, payload: RegisterRequest, ip: str | None) -> User:
        if self.users.get_by_email(payload.email):
            raise ConflictError("An account with this e-mail already exists.", code="EMAIL_ALREADY_REGISTERED")
        user = User(email=payload.email.lower(), full_name=payload.full_name.strip(),
                    hashed_password=hash_password(payload.password),
                    role_id=self.users.role_id(RoleName.CUSTOMER))
        try:
            self.users.add(user)
            self.audit.log("USER_REGISTERED", user_id=user.id, entity="user", entity_id=user.id, ip=ip)
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ConflictError("An account with this e-mail already exists.", code="EMAIL_ALREADY_REGISTERED")
        self.db.refresh(user)
        return user

    # ------------------------------------------------------------------ tokens
    def _issue_tokens(self, user: User, family_id: uuid.UUID, ip: str | None) -> dict:
        jti = uuid.uuid4().hex
        refresh = create_refresh_token(user.id, family_id, jti)
        self.tokens.add(RefreshToken(jti=jti, user_id=user.id, family_id=family_id, ip_address=ip,
                                     expires_at=utcnow() + timedelta(days=settings.refresh_token_expire_days)))
        access = create_access_token(user.id, user.role_name.value, family_id)
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
                "expires_in": settings.access_token_expire_minutes * 60}

    def login(self, payload: LoginRequest, ip: str | None) -> dict:
        user = self.users.get_by_email(payload.email)
        valid = verify_password(payload.password, user.hashed_password if user else _DUMMY_HASH)
        if not user or not valid:
            self.audit.log("LOGIN_FAILED", user_id=user.id if user else None, entity="user",
                           entity_id=user.id if user else None, ip=ip, meta={"email": payload.email})
            self.db.commit()
            raise UnauthorizedError("Invalid e-mail or password.", code="INVALID_CREDENTIALS")
        if not user.is_active:
            self.audit.log("LOGIN_FAILED", user_id=user.id, entity="user", entity_id=user.id, ip=ip,
                           meta={"reason": "inactive"})
            self.db.commit()
            raise UnauthorizedError("This account is inactive.", code="ACCOUNT_INACTIVE")
        tokens = self._issue_tokens(user, uuid.uuid4(), ip)
        user.last_login_at = utcnow()
        self.audit.log("LOGIN", user_id=user.id, entity="user", entity_id=user.id, ip=ip)
        self.db.commit()
        return tokens

    def refresh(self, refresh_token: str, ip: str | None) -> dict:
        """Refresh-token ROTATION: each refresh token is single-use. Presenting an already
        used token is treated as theft -> the whole token family (session) is revoked."""
        claims = decode_token(refresh_token, "refresh")
        row = self.tokens.get_by_jti_for_update(claims["jti"])
        if row is None:
            raise UnauthorizedError("Invalid token.", code="INVALID_TOKEN")
        now = utcnow()
        if row.revoked_at is not None:
            self.tokens.revoke_family(row.family_id, now)
            self.audit.log("REFRESH_TOKEN_REUSE_DETECTED", user_id=row.user_id, entity="user",
                           entity_id=row.user_id, ip=ip)
            self.db.commit()
            raise UnauthorizedError("Refresh token is no longer valid.", code="TOKEN_REVOKED")
        if row.expires_at <= now:
            raise UnauthorizedError("Token has expired.", code="TOKEN_EXPIRED")
        user = self.users.get(row.user_id)
        if not user or not user.is_active:
            raise UnauthorizedError("This account is inactive.", code="ACCOUNT_INACTIVE")
        tokens = self._issue_tokens(user, row.family_id, ip)
        new_jti = decode_token(tokens["refresh_token"], "refresh")["jti"]
        row.revoked_at, row.replaced_by_jti = now, new_jti
        self.db.commit()
        return tokens

    def logout(self, user: User, session_id: uuid.UUID, refresh_token: str | None, ip: str | None) -> None:
        family = session_id
        if refresh_token:
            claims = decode_token(refresh_token, "refresh")
            if claims["sub"] != str(user.id):
                raise UnauthorizedError("Invalid token.", code="INVALID_TOKEN")
            family = uuid.UUID(claims["sid"])
        self.tokens.revoke_family(family, utcnow())      # also invalidates the paired access token
        self.audit.log("LOGOUT", user_id=user.id, entity="user", entity_id=user.id, ip=ip)
        self.db.commit()

    def change_password(self, user: User, payload: ChangePasswordRequest, ip: str | None) -> None:
        if not verify_password(payload.current_password, user.hashed_password):
            raise UnauthorizedError("Current password is incorrect.", code="INVALID_CREDENTIALS")
        user.hashed_password = hash_password(payload.new_password)
        self.tokens.revoke_all_for_user(user.id, utcnow())   # sign out everywhere
        self.audit.log("PASSWORD_CHANGED", user_id=user.id, entity="user", entity_id=user.id, ip=ip)
        self.db.commit()

    # ------------------------------------------------------------------ request authentication
    def authenticate(self, access_token: str) -> User:
        claims = decode_token(access_token, "access")
        user = self.users.get(uuid.UUID(claims["sub"]))
        if not user or not user.is_active:
            raise UnauthorizedError("Account is inactive or does not exist.", code="ACCOUNT_INACTIVE")
        sid = uuid.UUID(claims["sid"])
        if not self.tokens.family_active(sid, utcnow()):
            raise UnauthorizedError("Session has been revoked.", code="TOKEN_REVOKED")
        user.session_id = sid  # type: ignore[attr-defined]  (transient, not persisted)
        return user

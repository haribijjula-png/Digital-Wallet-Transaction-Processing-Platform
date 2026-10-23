import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import ExpiredSignatureError, JWTError, jwt

from app.core.config import settings
from app.core.exceptions import UnauthorizedError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def _encode(claims: dict, expires_delta: timedelta) -> str:
    now = utcnow()
    payload = {**claims, "iat": now, "exp": now + expires_delta}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_access_token(user_id: uuid.UUID, role: str, session_id: uuid.UUID,
                        expires_delta: timedelta | None = None) -> str:
    return _encode(
        {"sub": str(user_id), "role": role, "sid": str(session_id), "jti": uuid.uuid4().hex, "type": "access"},
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(user_id: uuid.UUID, session_id: uuid.UUID, jti: str,
                         expires_delta: timedelta | None = None) -> str:
    return _encode(
        {"sub": str(user_id), "sid": str(session_id), "jti": jti, "type": "refresh"},
        expires_delta or timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except ExpiredSignatureError:
        raise UnauthorizedError("Token has expired.", code="TOKEN_EXPIRED")
    except JWTError:
        raise UnauthorizedError("Invalid token.", code="INVALID_TOKEN")
    if payload.get("type") != expected_type:
        raise UnauthorizedError("Invalid token type.", code="INVALID_TOKEN")
    return payload

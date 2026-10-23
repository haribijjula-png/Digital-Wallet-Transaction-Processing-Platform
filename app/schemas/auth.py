import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import RoleName
from app.schemas.common import ORMModel


def _validate_password(v: str) -> str:
    if not (re.search(r"[a-z]", v) and re.search(r"[A-Z]", v) and re.search(r"\d", v)):
        raise ValueError("Password must contain upper-case, lower-case letters and a digit.")
    return v


class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"email": "alice@example.com", "full_name": "Alice Sharma", "password": "StrongPass123"}]})

    _pw = field_validator("password")(_validate_password)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"email": "alice@example.com", "password": "StrongPass123"}]})


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)
    _pw = field_validator("new_password")(_validate_password)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: RoleName = Field(validation_alias="role_name")
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class StaffCreateRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    role: RoleName
    _pw = field_validator("password")(_validate_password)


class UserUpdateRequest(BaseModel):
    is_active: bool | None = None
    role: RoleName | None = None
    full_name: str | None = Field(default=None, min_length=2, max_length=120)

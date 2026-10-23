from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import client_ip, get_current_user
from app.core.database import get_db
from app.models import User
from app.schemas.auth import (ChangePasswordRequest, LoginRequest, LogoutRequest, RefreshRequest, RegisterRequest,
                              TokenResponse, UserOut)
from app.schemas.common import ErrorResponse, MessageResponse
from app.services.auth import AuthService

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
ERR = {401: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


@router.post("/register", response_model=UserOut, status_code=201, responses=ERR,
             summary="Register a new customer account")
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    return AuthService(db).register(payload, client_ip(request))


@router.post("/login", response_model=TokenResponse, responses=ERR, summary="Login (returns access + refresh token)")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    return AuthService(db).login(payload, client_ip(request))


@router.post("/refresh", response_model=TokenResponse, responses=ERR,
             summary="Rotate the refresh token and get a new access token")
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    return AuthService(db).refresh(payload.refresh_token, client_ip(request))


@router.post("/logout", response_model=MessageResponse, responses=ERR,
             summary="Revoke the current session (and the supplied refresh token family)")
def logout(request: Request, payload: LogoutRequest | None = None, user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    AuthService(db).logout(user, user.session_id, payload.refresh_token if payload else None, client_ip(request))
    return MessageResponse(message="Logged out.")


@router.get("/me", response_model=UserOut, responses=ERR, summary="Current user profile")
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password", response_model=MessageResponse, responses=ERR,
             summary="Change password (revokes all sessions)")
def change_password(payload: ChangePasswordRequest, request: Request, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    AuthService(db).change_password(user, payload, client_ip(request))
    return MessageResponse(message="Password changed. Please sign in again.")

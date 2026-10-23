"""Domain-specific exceptions + centralised handlers (consistent error envelope)."""
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("wallet.errors")


class AppError(Exception):
    status_code = 400
    code = "BAD_REQUEST"
    message = "Bad request."

    def __init__(self, message: str | None = None, *, code: str | None = None,
                 status_code: int | None = None, details: Any = None):
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code, code, message = 400, "BAD_REQUEST", "Bad request."


class UnauthorizedError(AppError):
    status_code, code, message = 401, "UNAUTHORIZED", "Authentication required."


class ForbiddenError(AppError):
    status_code, code, message = 403, "FORBIDDEN", "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code, code, message = 404, "NOT_FOUND", "Resource not found."


class ConflictError(AppError):
    status_code, code, message = 409, "CONFLICT", "Conflict."


# ---- financial / domain errors -------------------------------------------------
class InsufficientBalanceError(AppError):
    status_code, code, message = 400, "INSUFFICIENT_BALANCE", "Insufficient wallet balance."


class InvalidAmountError(AppError):
    status_code, code, message = 400, "INVALID_AMOUNT", "Amount must be greater than zero."


class WalletFrozenError(AppError):
    status_code, code, message = 409, "WALLET_FROZEN", "Wallet is frozen."


class WalletClosedError(AppError):
    status_code, code, message = 409, "WALLET_CLOSED", "Wallet is closed."


class WalletSuspendedError(AppError):
    status_code, code, message = 409, "WALLET_SUSPENDED", "Wallet is suspended."


class InvalidStateTransitionError(AppError):
    status_code, code, message = 409, "INVALID_STATE_TRANSITION", "Invalid status transition."


class InvalidTransferError(AppError):
    status_code, code, message = 400, "INVALID_TRANSFER", "Invalid transfer."


class DuplicateTransactionError(AppError):
    status_code, code, message = 409, "DUPLICATE_TRANSACTION", "Duplicate transaction."


class TransactionLimitExceededError(AppError):
    status_code, code, message = 400, "TRANSACTION_LIMIT_EXCEEDED", "Transaction limit exceeded."


class DuplicateIdempotencyKeyError(AppError):
    status_code, code, message = 409, "DUPLICATE_IDEMPOTENCY_KEY", \
        "Idempotency-Key was already used with a different request payload."


class IdempotencyKeyRequiredError(AppError):
    status_code, code, message = 400, "IDEMPOTENCY_KEY_REQUIRED", "The Idempotency-Key header is required."


class InvalidRefundError(AppError):
    status_code, code, message = 400, "INVALID_REFUND", "Invalid refund."


class SuspiciousTransactionError(AppError):
    status_code, code, message = 403, "SUSPICIOUS_TRANSACTION", "Transaction blocked as suspicious."


class ImmutableRecordError(AppError):
    status_code, code, message = 403, "IMMUTABLE_RECORD", "This record is immutable."


def error_body(exc: AppError) -> dict:
    err: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        err["details"] = exc.details
    return {"success": False, "error": err}


def _json(status: int, code: str, message: str, details: Any = None, headers: dict | None = None):
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return JSONResponse(status_code=status, content={"success": False, "error": err}, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(status_code=exc.status_code, content=error_body(exc), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return _json(422, "VALIDATION_ERROR", "Request validation failed.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        codes = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                 405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 422: "VALIDATION_ERROR"}
        return _json(exc.status_code, codes.get(exc.status_code, "HTTP_ERROR"),
                     str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("Unhandled error", exc_info=exc)
        return _json(500, "INTERNAL_SERVER_ERROR", "An unexpected error occurred.")

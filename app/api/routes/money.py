"""Deposits, withdrawals, transfers, refunds, transactions."""
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import (enum_param, client_ip, get_current_user, idempotency_key, page_params, require_customer,
                          require_staff, to_response)
from app.core.database import get_db
from app.models import RefundStatus, RiskStatus, TransactionType, TxStatus, User
from app.repositories.base import PageParams, page_dict
from app.schemas.common import ErrorResponse, Page
from app.schemas.wallets import (DepositOut, LedgerOut, RefundCreate, RefundDecision, RefundOut, TransactionOut,
                                 TransferCreate, TransferOut, WithdrawalOut)
from app.services.deposits import DepositService
from app.services.notifications import deliver_pending_notifications
from app.services.refunds import RefundService
from app.services.transactions import TransactionQueryService
from app.services.transfers import TransferService
from app.services.withdrawals import WithdrawalService

ERR = {400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
       404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}

deposits = APIRouter(prefix="/api/deposits", tags=["Deposits"])
withdrawals = APIRouter(prefix="/api/withdrawals", tags=["Withdrawals"])
transfers = APIRouter(prefix="/api/transfers", tags=["Transfers"])
refunds = APIRouter(prefix="/api/refunds", tags=["Refunds"])
transactions = APIRouter(prefix="/api/transactions", tags=["Transactions"])


# ---------------------------------------------------------------- deposits
@deposits.get("", response_model=Page[DepositOut], responses=ERR, summary="List deposits")
def list_deposits(status: TxStatus | None = Depends(enum_param(TxStatus, "status")), wallet_id: uuid.UUID | None = None,
                  params: PageParams = Depends(page_params), user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    items, total = DepositService(db).list(user, params, status, wallet_id)
    return page_dict(items, total, params)


@deposits.get("/{deposit_id}", response_model=DepositOut, responses=ERR, summary="Get a deposit")
def get_deposit(deposit_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return DepositService(db).get_authorized(user, deposit_id)


# ---------------------------------------------------------------- withdrawals
@withdrawals.get("", response_model=Page[WithdrawalOut], responses=ERR, summary="List withdrawals")
def list_withdrawals(status: TxStatus | None = Depends(enum_param(TxStatus, "status")), wallet_id: uuid.UUID | None = None,
                     params: PageParams = Depends(page_params), user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    items, total = WithdrawalService(db).list(user, params, status, wallet_id)
    return page_dict(items, total, params)


@withdrawals.get("/{withdrawal_id}", response_model=WithdrawalOut, responses=ERR, summary="Get a withdrawal")
def get_withdrawal(withdrawal_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return WithdrawalService(db).get_authorized(user, withdrawal_id)


@withdrawals.post("/{withdrawal_id}/cancel", response_model=WithdrawalOut, responses=ERR,
                  summary="Cancel a PENDING (held for review) withdrawal")
def cancel_withdrawal(withdrawal_id: uuid.UUID, request: Request, background: BackgroundTasks,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    w = WithdrawalService(db).cancel(user, withdrawal_id, client_ip(request))
    background.add_task(deliver_pending_notifications)
    return w


# ---------------------------------------------------------------- transfers
@transfers.post("", response_model=TransferOut, status_code=201, responses=ERR,
                summary="Create a transfer - idempotent via Idempotency-Key (202 when held for review)")
def create_transfer(payload: TransferCreate, request: Request, background: BackgroundTasks,
                    key: str = Depends(idempotency_key), user: User = Depends(require_customer),
                    db: Session = Depends(get_db)):
    return to_response(TransferService(db).create(user, payload, key, client_ip(request)), background)


@transfers.get("", response_model=Page[TransferOut], responses=ERR, summary="List transfers")
def list_transfers(status: TxStatus | None = Depends(enum_param(TxStatus, "status")), wallet_id: uuid.UUID | None = None,
                   params: PageParams = Depends(page_params), user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    items, total = TransferService(db).list(user, params, status, wallet_id)
    return page_dict(items, total, params)


@transfers.get("/{transfer_id}", response_model=TransferOut, responses=ERR, summary="Get a transfer")
def get_transfer(transfer_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return TransferService(db).get_authorized(user, transfer_id)


@transfers.post("/{transfer_id}/cancel", response_model=TransferOut, responses=ERR,
                summary="Cancel a PENDING (held for review) transfer")
def cancel_transfer(transfer_id: uuid.UUID, request: Request, background: BackgroundTasks,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = TransferService(db).cancel(user, transfer_id, client_ip(request))
    background.add_task(deliver_pending_notifications)
    return t


# ---------------------------------------------------------------- refunds
@refunds.post("", response_model=RefundOut, status_code=201, responses=ERR,
              summary="Request a refund of a completed transfer - idempotent via Idempotency-Key")
def create_refund(payload: RefundCreate, request: Request, key: str = Depends(idempotency_key),
                  user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return to_response(RefundService(db).create(user, payload, key, client_ip(request)))


@refunds.get("", response_model=Page[RefundOut], responses=ERR, summary="List refunds")
def list_refunds(status: RefundStatus | None = Depends(enum_param(RefundStatus, "status")), transaction_id: uuid.UUID | None = None,
                 params: PageParams = Depends(page_params), user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    items, total = RefundService(db).list(user, params, status, transaction_id)
    return page_dict(items, total, params)


@refunds.get("/{refund_id}", response_model=RefundOut, responses=ERR, summary="Get a refund")
def get_refund(refund_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return RefundService(db).get_authorized(user, refund_id)


@refunds.post("/{refund_id}/start-review", response_model=RefundOut, responses=ERR,
              summary="Move a refund to UNDER_REVIEW (staff)")
def start_refund_review(refund_id: uuid.UUID, request: Request, user: User = Depends(require_staff),
                        db: Session = Depends(get_db)):
    return RefundService(db).start_review(user, refund_id, client_ip(request))


@refunds.post("/{refund_id}/approve", response_model=RefundOut, responses=ERR,
              summary="Approve a refund and post the REFUND ledger entries (staff)")
def approve_refund(refund_id: uuid.UUID, request: Request, background: BackgroundTasks,
                   payload: RefundDecision | None = None, user: User = Depends(require_staff),
                   db: Session = Depends(get_db)):
    r = RefundService(db).approve(user, refund_id, payload or RefundDecision(), client_ip(request))
    background.add_task(deliver_pending_notifications)
    return r


@refunds.post("/{refund_id}/reject", response_model=RefundOut, responses=ERR, summary="Reject a refund (staff)")
def reject_refund(refund_id: uuid.UUID, request: Request, background: BackgroundTasks,
                  payload: RefundDecision | None = None, user: User = Depends(require_staff),
                  db: Session = Depends(get_db)):
    r = RefundService(db).reject(user, refund_id, payload or RefundDecision(), client_ip(request))
    background.add_task(deliver_pending_notifications)
    return r


# ---------------------------------------------------------------- transaction history
@transactions.get("", response_model=Page[TransactionOut], responses=ERR,
                  summary="Transaction history: pagination, sorting, search and filters")
def list_transactions(
        type: TransactionType | None = Depends(enum_param(TransactionType, "type")),
        status: TxStatus | None = Depends(enum_param(TxStatus, "status")), risk_status: RiskStatus | None = Depends(enum_param(RiskStatus, "risk_status")), wallet_id: uuid.UUID | None = None,
        min_amount: Decimal | None = Query(None, ge=0), max_amount: Decimal | None = Query(None, ge=0),
        from_date: date | None = None, to_date: date | None = None,
        search: str | None = Query(None, description="Matches reference number / description"),
        params: PageParams = Depends(page_params), user: User = Depends(get_current_user),
        db: Session = Depends(get_db)):
    items, total = TransactionQueryService(db).search(
        user, params, wallet_id=wallet_id, tx_type=type, status=status, risk_status=risk_status,
        min_amount=min_amount, max_amount=max_amount, from_date=from_date, to_date=to_date, search=search)
    return page_dict(items, total, params)


@transactions.get("/{transaction_id}", response_model=TransactionOut, responses=ERR, summary="Get a transaction")
def get_transaction(transaction_id: uuid.UUID, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    return TransactionQueryService(db).get(user, transaction_id)


@transactions.get("/{transaction_id}/ledger", response_model=list[LedgerOut], responses=ERR,
                  summary="Ledger entries produced by a transaction")
def transaction_ledger(transaction_id: uuid.UUID, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    return TransactionQueryService(db).ledger_entries(user, transaction_id)

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import (enum_param, client_ip, get_current_user, idempotency_key, page_params, require_customer, to_response)
from app.core.database import get_db
from app.models import LedgerType, User, WalletStatus
from app.repositories.base import PageParams, page_dict
from app.schemas.common import ErrorResponse, Page
from app.schemas.wallets import (DepositCreate, DepositOut, LedgerOut, WalletCreate, WalletOut, WalletUpdate,
                                 WithdrawalCreate, WithdrawalOut)
from app.services.deposits import DepositService
from app.services.notifications import deliver_pending_notifications
from app.services.wallets import WalletService
from app.services.withdrawals import WithdrawalService

router = APIRouter(prefix="/api/wallets", tags=["Wallets"])
ERR = {400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
       404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


@router.post("", response_model=WalletOut, status_code=201, responses=ERR, summary="Create a wallet (customers)")
def create_wallet(payload: WalletCreate, request: Request, user: User = Depends(require_customer),
                  db: Session = Depends(get_db)):
    return WalletService(db).create(user, payload, client_ip(request))


@router.get("", response_model=Page[WalletOut], responses=ERR,
            summary="List wallets (customers: own wallets; staff: all, optional user_id filter)")
def list_wallets(status: WalletStatus | None = Depends(enum_param(WalletStatus, "status")), currency: str | None = None, user_id: uuid.UUID | None = None,
                 params: PageParams = Depends(page_params), user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    items, total = WalletService(db).list(user, params, status, currency, user_id)
    return page_dict(items, total, params)


@router.get("/{wallet_id}", response_model=WalletOut, responses=ERR, summary="Get a wallet")
def get_wallet(wallet_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return WalletService(db).get_authorized(user, wallet_id)


@router.patch("/{wallet_id}", response_model=WalletOut, responses=ERR,
              summary="Update wallet name / status (invalid status transitions are rejected)")
def update_wallet(wallet_id: uuid.UUID, payload: WalletUpdate, request: Request, background: BackgroundTasks,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    wallet = WalletService(db).update(user, wallet_id, payload, client_ip(request))
    background.add_task(deliver_pending_notifications)
    return wallet


@router.post("/{wallet_id}/freeze", response_model=WalletOut, responses=ERR,
             summary="Freeze a wallet (owner or staff)")
def freeze_wallet(wallet_id: uuid.UUID, request: Request, background: BackgroundTasks,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    wallet = WalletService(db).freeze(user, wallet_id, client_ip(request))
    background.add_task(deliver_pending_notifications)
    return wallet


@router.post("/{wallet_id}/unfreeze", response_model=WalletOut, responses=ERR,
             summary="Unfreeze a wallet (staff only)")
def unfreeze_wallet(wallet_id: uuid.UUID, request: Request, background: BackgroundTasks,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    wallet = WalletService(db).unfreeze(user, wallet_id, client_ip(request))
    background.add_task(deliver_pending_notifications)
    return wallet


@router.get("/{wallet_id}/ledger", response_model=Page[LedgerOut], responses=ERR,
            summary="Immutable ledger entries of a wallet")
def wallet_ledger(wallet_id: uuid.UUID, entry_type: LedgerType | None = Depends(enum_param(LedgerType, "entry_type")),
                  params: PageParams = Depends(page_params), user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    items, total = WalletService(db).ledger_for(user, wallet_id, params, entry_type)
    return page_dict(items, total, params)


@router.post("/{wallet_id}/deposits", response_model=DepositOut, status_code=201, responses=ERR,
             summary="Create a (simulated) deposit - idempotent via Idempotency-Key")
def create_deposit(wallet_id: uuid.UUID, payload: DepositCreate, request: Request, background: BackgroundTasks,
                   key: str = Depends(idempotency_key), user: User = Depends(require_customer),
                   db: Session = Depends(get_db)):
    return to_response(DepositService(db).create(user, wallet_id, payload, key, client_ip(request)), background)


@router.post("/{wallet_id}/withdrawals", response_model=WithdrawalOut, status_code=201, responses=ERR,
             summary="Create a withdrawal - idempotent via Idempotency-Key (202 when held for review)")
def create_withdrawal(wallet_id: uuid.UUID, payload: WithdrawalCreate, request: Request,
                      background: BackgroundTasks, key: str = Depends(idempotency_key),
                      user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return to_response(WithdrawalService(db).create(user, wallet_id, payload, key, client_ip(request)), background)

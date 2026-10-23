import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (BadRequestError, ConflictError, ForbiddenError, NotFoundError)
from app.core.security import utcnow
from app.domain.state_machines import WALLET_TRANSITIONS, ensure_transition
from app.models import NotificationType, RoleName, User, Wallet, WalletStatus
from app.repositories.base import PageParams
from app.repositories.transactions import LedgerRepository
from app.repositories.wallets import WalletRepository
from app.schemas.wallets import WalletCreate, WalletUpdate
from app.services.access import assert_wallet_access
from app.services.audit import AuditService
from app.services.common import is_admin, is_staff
from app.services.notifications import NotificationService


class WalletService:
    def __init__(self, db: Session):
        self.db = db
        self.wallets = WalletRepository(db)
        self.ledger = LedgerRepository(db)
        self.audit = AuditService(db)
        self.notify = NotificationService(db)

    # ------------------------------------------------------------------ queries
    def get_authorized(self, user: User, wallet_id: uuid.UUID) -> Wallet:
        wallet = self.wallets.get(wallet_id)
        if not wallet:
            raise NotFoundError("Wallet not found.", code="WALLET_NOT_FOUND")
        assert_wallet_access(user, wallet)
        return wallet

    def list(self, user: User, params: PageParams, status=None, currency=None, user_id=None):
        scope = user_id if is_staff(user) else user.id
        return self.wallets.list(params, user_id=scope, status=status, currency=currency)

    def ledger_for(self, user: User, wallet_id: uuid.UUID, params: PageParams, entry_type=None):
        self.get_authorized(user, wallet_id)
        return self.ledger.for_wallet(wallet_id, params, entry_type)

    # ------------------------------------------------------------------ commands
    def create(self, user: User, payload: WalletCreate, ip: str | None) -> Wallet:
        currency = payload.currency.upper()
        if currency not in settings.supported_currencies:
            raise BadRequestError(f"Unsupported currency. Supported: {', '.join(settings.supported_currencies)}.",
                                  code="UNSUPPORTED_CURRENCY")
        if self.wallets.count_for_user(user.id) >= settings.max_wallets_per_user:
            raise ConflictError("Maximum number of wallets reached.", code="WALLET_LIMIT_REACHED")
        wallet = self.wallets.add(Wallet(user_id=user.id, currency=currency, name=payload.name.strip()))
        self.audit.log("WALLET_CREATED", user_id=user.id, entity="wallet", entity_id=wallet.id, ip=ip,
                       meta={"currency": currency})
        self.db.commit()
        return wallet

    def update(self, user: User, wallet_id: uuid.UUID, payload: WalletUpdate, ip: str | None) -> Wallet:
        wallet = self.get_authorized(user, wallet_id)
        if payload.name is not None:
            if not (wallet.user_id == user.id or is_admin(user)):
                raise ForbiddenError("Only the owner or an administrator can rename a wallet.")
            wallet.name = payload.name.strip()
            self.audit.log("WALLET_UPDATED", user_id=user.id, entity="wallet", entity_id=wallet.id, ip=ip,
                           meta={"name": wallet.name})
            self.db.commit()
        if payload.status is not None and payload.status != wallet.status:
            return self.change_status(user, wallet_id, payload.status, ip)
        if payload.status is not None and payload.status == wallet.status and payload.name is None:
            ensure_transition(WALLET_TRANSITIONS, wallet.status, payload.status, "wallet")  # same-state = invalid
        return wallet

    def freeze(self, user: User, wallet_id: uuid.UUID, ip: str | None) -> Wallet:
        return self.change_status(user, wallet_id, WalletStatus.FROZEN, ip)

    def unfreeze(self, user: User, wallet_id: uuid.UUID, ip: str | None) -> Wallet:
        wallet = self.wallets.get(wallet_id)
        if wallet and wallet.status != WalletStatus.FROZEN:
            from app.core.exceptions import InvalidStateTransitionError
            raise InvalidStateTransitionError(f"Only a FROZEN wallet can be unfrozen (current: {wallet.status.value}).")
        return self.change_status(user, wallet_id, WalletStatus.ACTIVE, ip)

    def change_status(self, user: User, wallet_id: uuid.UUID, new: WalletStatus, ip: str | None) -> Wallet:
        locked = self.wallets.lock_many([wallet_id])
        wallet = locked.get(wallet_id)
        if not wallet:
            raise NotFoundError("Wallet not found.", code="WALLET_NOT_FOUND")
        assert_wallet_access(user, wallet)
        old = wallet.status
        ensure_transition(WALLET_TRANSITIONS, old, new, "wallet")       # 409 for invalid transitions
        self._authorize_status_change(user, wallet, old, new)           # 403 for insufficient privilege

        if new == WalletStatus.CLOSED and (wallet.balance.available_balance > 0 or wallet.balance.pending_balance > 0):
            raise ConflictError("Wallet balance must be zero before closing.", code="WALLET_NOT_EMPTY")

        now = utcnow()
        wallet.status = new
        if new == WalletStatus.FROZEN:
            wallet.last_frozen_at = now
        if new == WalletStatus.CLOSED:
            wallet.closed_at = now

        action = {WalletStatus.FROZEN: "WALLET_FROZEN", WalletStatus.ACTIVE: "WALLET_UNFROZEN"}.get(
            new, f"WALLET_{new.value}")
        if new == WalletStatus.ACTIVE and old != WalletStatus.FROZEN:
            action = "WALLET_REACTIVATED"
        self.audit.log(action, user_id=user.id, entity="wallet", entity_id=wallet.id, ip=ip,
                       meta={"from": old.value, "to": new.value})
        if new == WalletStatus.FROZEN:
            self.notify.notify(wallet.user_id, NotificationType.WALLET_FROZEN, "Wallet frozen",
                               f"Your wallet '{wallet.name}' has been frozen.", {"wallet_id": wallet.id})
        elif old == WalletStatus.FROZEN and new == WalletStatus.ACTIVE:
            self.notify.notify(wallet.user_id, NotificationType.WALLET_UNFROZEN, "Wallet unfrozen",
                               f"Your wallet '{wallet.name}' is active again.", {"wallet_id": wallet.id})
        self.db.commit()
        return wallet

    @staticmethod
    def _authorize_status_change(user: User, wallet: Wallet, old: WalletStatus, new: WalletStatus) -> None:
        role = user.role_name
        if role in (RoleName.SUPER_ADMIN, RoleName.ADMIN):
            return
        if role == RoleName.SUPPORT_AGENT:
            if new == WalletStatus.FROZEN or (new == WalletStatus.ACTIVE and old == WalletStatus.FROZEN):
                return
            raise ForbiddenError("Support agents may only freeze or unfreeze wallets.")
        # CUSTOMER (owner): may freeze or close their own wallet; unfreezing needs staff
        if wallet.user_id == user.id and new in (WalletStatus.FROZEN, WalletStatus.CLOSED):
            return
        raise ForbiddenError("You are not allowed to make this wallet status change.")

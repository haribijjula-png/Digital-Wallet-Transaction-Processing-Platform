import uuid
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (BadRequestError, ConflictError, ForbiddenError, InvalidAmountError, NotFoundError)
from app.core.security import hash_password, utcnow
from app.models import (LedgerType, RoleName, Transaction, TransactionType, TxStatus, User, WalletStatus)
from app.models.enums import Direction
from app.repositories.base import PageParams, new_ref
from app.repositories.misc import AuditRepository, LimitRepository
from app.repositories.users import RefreshTokenRepository, UserRepository
from app.repositories.wallets import WalletRepository
from app.schemas.auth import StaffCreateRequest, UserUpdateRequest
from app.schemas.wallets import AdjustmentRequest, LimitsUpdate
from app.services.audit import AuditService
from app.services.common import is_super_admin
from app.services.ledger import LedgerService
from app.services.movement import MovementSupport


class AdminService:
    def __init__(self, db: Session):
        self.db = db
        self.users = UserRepository(db)
        self.tokens = RefreshTokenRepository(db)
        self.limits = LimitRepository(db)
        self.wallets = WalletRepository(db)
        self.audit_repo = AuditRepository(db)
        self.audit = AuditService(db)
        self.ledger = LedgerService(db)

    # ------------------------------------------------------------------ transaction limits / configuration
    def get_limits(self):
        return self.limits.get()

    def update_limits(self, admin: User, payload: LimitsUpdate, ip):
        row = self.limits.get_for_update()
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise BadRequestError("No limit values supplied.", code="NO_CHANGES")
        before = {k: getattr(row, k) for k in changes}
        for k, v in changes.items():
            setattr(row, k, v)
        if row.velocity_review_count > row.velocity_block_count:
            self.db.rollback()
            raise BadRequestError("velocity_review_count must not exceed velocity_block_count.",
                                  code="INVALID_LIMITS")
        row.updated_by = admin.id
        self.audit.log("TRANSACTION_LIMIT_CHANGED", user_id=admin.id, entity="transaction_limits", entity_id=1,
                       ip=ip, meta={"before": before, "after": changes})
        self.db.commit()
        return row

    # ------------------------------------------------------------------ users
    def list_users(self, params: PageParams, role=None, is_active=None, search=None):
        return self.users.list(params, role, is_active, search)

    def get_user(self, user_id: uuid.UUID) -> User:
        u = self.users.get(user_id)
        if not u:
            raise NotFoundError("User not found.", code="USER_NOT_FOUND")
        return u

    def create_staff(self, actor: User, payload: StaffCreateRequest, ip) -> User:
        if not is_super_admin(actor):
            raise ForbiddenError("Only a Super Admin can create users with a specific role.")
        if self.users.get_by_email(payload.email):
            raise ConflictError("An account with this e-mail already exists.", code="EMAIL_ALREADY_REGISTERED")
        user = User(email=payload.email.lower(), full_name=payload.full_name.strip(),
                    hashed_password=hash_password(payload.password), role_id=self.users.role_id(payload.role))
        try:
            self.users.add(user)
            self.audit.log("USER_CREATED_BY_ADMIN", user_id=actor.id, entity="user", entity_id=user.id, ip=ip,
                           meta={"role": payload.role.value})
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ConflictError("An account with this e-mail already exists.", code="EMAIL_ALREADY_REGISTERED")
        self.db.refresh(user)
        return user

    def update_user(self, actor: User, user_id: uuid.UUID, payload: UserUpdateRequest, ip) -> User:
        target = self.get_user(user_id)
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise BadRequestError("No changes supplied.", code="NO_CHANGES")
        # Admins manage customers and support agents; staff above that is Super Admin territory.
        if not is_super_admin(actor) and target.role_name in (RoleName.ADMIN, RoleName.SUPER_ADMIN):
            raise ForbiddenError("Only a Super Admin can modify administrator accounts.")
        if "role" in changes:
            if not is_super_admin(actor):
                raise ForbiddenError("Only a Super Admin can change roles.")
            if target.id == actor.id:
                raise ForbiddenError("You cannot change your own role.")
            if changes["role"] != RoleName.CUSTOMER and self.wallets.count_for_user(target.id) > 0:
                raise ConflictError("A user that owns wallets cannot be given a staff role.", code="USER_HAS_WALLETS")
            target.role_id = self.users.role_id(changes["role"])
        if "is_active" in changes:
            if target.id == actor.id and not changes["is_active"]:
                raise ForbiddenError("You cannot deactivate your own account.")
            target.is_active = changes["is_active"]
            if not target.is_active:
                self.tokens.revoke_all_for_user(target.id, utcnow())     # kill every session immediately
        if "full_name" in changes:
            target.full_name = changes["full_name"].strip()
        self.audit.log("USER_UPDATED", user_id=actor.id, entity="user", entity_id=target.id, ip=ip,
                       meta={k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()})
        self.db.commit()
        self.db.refresh(target)
        return target

    # ------------------------------------------------------------------ audit logs
    def audit_logs(self, params: PageParams, **filters):
        return self.audit_repo.list(params, **filters)

    # ------------------------------------------------------------------ manual adjustment (Super Admin)
    def adjust_wallet(self, actor: User, wallet_id: uuid.UUID, payload: AdjustmentRequest, ip):
        if not is_super_admin(actor):
            raise ForbiddenError("Only a Super Admin can post balance adjustments.")
        if payload.amount <= 0:
            raise InvalidAmountError()
        locked = self.wallets.lock_many([wallet_id])
        wallet = locked.get(wallet_id)
        if not wallet:
            raise NotFoundError("Wallet not found.", code="WALLET_NOT_FOUND")
        if wallet.status == WalletStatus.CLOSED:
            from app.core.exceptions import WalletClosedError
            raise WalletClosedError()
        s = MovementSupport(self.db)
        tx = s.new_tx(type_=TransactionType.ADJUSTMENT, wallet_id=wallet.id, counterparty=None,
                      amount=payload.amount, currency=wallet.currency, reference_number=new_ref("ADJ"),
                      entity="adjustment", ref_id=uuid.uuid4(), user_id=actor.id, description=payload.reason)
        meta = {"reason": payload.reason}
        if payload.direction == Direction.CREDIT:
            entry = self.ledger.credit(wallet, payload.amount, LedgerType.ADJUSTMENT, transaction=tx,
                                       description=payload.reason, created_by=actor.id, meta=meta)
        else:
            entry = self.ledger.debit(wallet, payload.amount, LedgerType.ADJUSTMENT, transaction=tx,
                                      description=payload.reason, created_by=actor.id, meta=meta)
        tx.status = TxStatus.COMPLETED
        tx.completed_at = utcnow()
        self.audit.log("WALLET_ADJUSTED", user_id=actor.id, entity="wallet", entity_id=wallet.id, ip=ip,
                       meta={"direction": payload.direction.value, "amount": payload.amount, "reason": payload.reason})
        self.db.commit()
        return entry

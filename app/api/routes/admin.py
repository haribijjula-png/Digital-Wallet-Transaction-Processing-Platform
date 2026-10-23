import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import (client_ip, enum_param, page_params, require_admin, require_staff, require_super_admin)
from app.core.database import get_db
from app.core.exceptions import NotFoundError
from app.models import ReviewStatus, RiskStatus, RoleName, User
from app.repositories.base import PageParams, page_dict
from app.schemas.auth import StaffCreateRequest, UserOut, UserUpdateRequest
from app.schemas.common import ErrorResponse, Page
from app.schemas.wallets import (AdjustmentRequest, AuditLogOut, LedgerOut, LimitsOut, LimitsUpdate, SuspiciousOut,
                                 SuspiciousReview)
from app.jobs.tasks import JOBS, run_job
from app.services.admin import AdminService
from app.services.notifications import deliver_pending_notifications
from app.services.suspicious import SuspiciousService

router = APIRouter(prefix="/api/admin", tags=["Administration"])
ERR = {400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
       404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


# ---------------------------------------------------------------- transaction limits (system configuration)
@router.get("/transaction-limits", response_model=LimitsOut, responses=ERR,
            summary="Get transaction limits & monitoring thresholds")
def get_limits(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return AdminService(db).get_limits()


@router.patch("/transaction-limits", response_model=LimitsOut, responses=ERR,
              summary="Update transaction limits & monitoring thresholds (audited)")
def update_limits(payload: LimitsUpdate, request: Request, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    return AdminService(db).update_limits(user, payload, client_ip(request))


# ---------------------------------------------------------------- users
@router.get("/users", response_model=Page[UserOut], responses=ERR, summary="List users")
def list_users(role: RoleName | None = Depends(enum_param(RoleName, "role")), is_active: bool | None = None,
               search: str | None = None, params: PageParams = Depends(page_params),
               _: User = Depends(require_admin), db: Session = Depends(get_db)):
    items, total = AdminService(db).list_users(params, role, is_active, search)
    return page_dict(items, total, params)


@router.post("/users", response_model=UserOut, status_code=201, responses=ERR,
             summary="Create a user with any role (Super Admin)")
def create_user(payload: StaffCreateRequest, request: Request, user: User = Depends(require_super_admin),
                db: Session = Depends(get_db)):
    return AdminService(db).create_staff(user, payload, client_ip(request))


@router.get("/users/{user_id}", response_model=UserOut, responses=ERR, summary="Get a user")
def get_user(user_id: uuid.UUID, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    return AdminService(db).get_user(user_id)


@router.patch("/users/{user_id}", response_model=UserOut, responses=ERR,
              summary="Activate/deactivate a user, change role (Super Admin), rename")
def update_user(user_id: uuid.UUID, payload: UserUpdateRequest, request: Request,
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return AdminService(db).update_user(user, user_id, payload, client_ip(request))


# ---------------------------------------------------------------- suspicious transactions
@router.get("/suspicious-transactions", response_model=Page[SuspiciousOut], responses=ERR,
            summary="List flagged transactions (staff)")
def list_suspicious(review_status: ReviewStatus | None = Depends(enum_param(ReviewStatus, "review_status")),
                    risk_status: RiskStatus | None = Depends(enum_param(RiskStatus, "risk_status")),
                    wallet_id: uuid.UUID | None = None, params: PageParams = Depends(page_params),
                    _: User = Depends(require_staff), db: Session = Depends(get_db)):
    items, total = SuspiciousService(db).list(params, review_status, risk_status, wallet_id)
    return page_dict(items, total, params)


@router.get("/suspicious-transactions/{suspicious_id}", response_model=SuspiciousOut, responses=ERR,
            summary="Get a flagged transaction (staff)")
def get_suspicious(suspicious_id: uuid.UUID, _: User = Depends(require_staff), db: Session = Depends(get_db)):
    return SuspiciousService(db).get(suspicious_id)


@router.post("/suspicious-transactions/{suspicious_id}/review", response_model=SuspiciousOut, responses=ERR,
             summary="Approve (release funds) or reject (release hold) a flagged transaction (staff)")
def review_suspicious(suspicious_id: uuid.UUID, payload: SuspiciousReview, request: Request,
                      background: BackgroundTasks, user: User = Depends(require_staff),
                      db: Session = Depends(get_db)):
    rec = SuspiciousService(db).review(user, suspicious_id, payload, client_ip(request))
    background.add_task(deliver_pending_notifications)
    return rec


# ---------------------------------------------------------------- audit logs / adjustments / jobs
@router.get("/audit-logs", response_model=Page[AuditLogOut], responses=ERR, summary="Search audit logs")
def audit_logs(user_id: uuid.UUID | None = None, action: str | None = None, entity: str | None = None,
               entity_id: str | None = None, params: PageParams = Depends(page_params),
               _: User = Depends(require_admin), db: Session = Depends(get_db)):
    items, total = AdminService(db).audit_logs(params, user_id=user_id, action=action, entity=entity,
                                               entity_id=entity_id)
    return page_dict(items, total, params)


@router.post("/wallets/{wallet_id}/adjustments", response_model=LedgerOut, status_code=201, responses=ERR,
             summary="Post a manual balance adjustment (Super Admin, ledger-backed)")
def adjust_wallet(wallet_id: uuid.UUID, payload: AdjustmentRequest, request: Request,
                  user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    return AdminService(db).adjust_wallet(user, wallet_id, payload, client_ip(request))


@router.post("/jobs/{job_name}/run", responses=ERR,
             summary="Run a scheduled job on demand: " + ", ".join(JOBS))
def run_scheduled_job(job_name: str, _: User = Depends(require_admin)):
    if job_name not in JOBS:
        raise NotFoundError(f"Unknown job. Available: {', '.join(JOBS)}.", code="JOB_NOT_FOUND")
    return {"success": True, "job": job_name, "result": run_job(job_name)}

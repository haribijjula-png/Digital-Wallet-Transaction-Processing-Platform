from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import require_admin, require_customer, require_roles
from app.core.database import get_db
from app.models import RoleName, User
from app.models.enums import STAFF_ROLES
from app.schemas.common import ErrorResponse
from app.schemas.dashboard import AdminDashboard, CustomerDashboard, SupportDashboard
from app.services.dashboard import DashboardService

router = APIRouter(prefix="/api/dashboard", tags=["Dashboards"])
ERR = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}}


@router.get("/admin", response_model=AdminDashboard, responses=ERR, summary="Admin / Super Admin dashboard")
def admin_dashboard(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return DashboardService(db).admin()


@router.get("/support", response_model=SupportDashboard, responses=ERR, summary="Support agent dashboard (staff)")
def support_dashboard(_: User = Depends(require_roles(STAFF_ROLES)), db: Session = Depends(get_db)):
    return DashboardService(db).support()


@router.get("/customer", response_model=CustomerDashboard, responses=ERR, summary="Customer dashboard")
def customer_dashboard(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return DashboardService(db).customer(user)

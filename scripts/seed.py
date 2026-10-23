"""Create the initial Super Admin (idempotent).   python -m scripts.seed"""
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import RoleName, User
from app.repositories.users import UserRepository


def main() -> None:
    with SessionLocal() as db:
        repo = UserRepository(db)
        if repo.get_by_email(settings.seed_admin_email):
            print(f"Super admin {settings.seed_admin_email} already exists.")
            return
        repo.add(User(email=settings.seed_admin_email.lower(), full_name="Super Admin",
                      hashed_password=hash_password(settings.seed_admin_password),
                      role_id=repo.role_id(RoleName.SUPER_ADMIN)))
        db.commit()
        print(f"Created super admin {settings.seed_admin_email}")


if __name__ == "__main__":
    main()

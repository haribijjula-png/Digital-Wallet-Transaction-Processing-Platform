"""Resource-level authorization helpers (ownership checks)."""
import uuid

from app.core.exceptions import ForbiddenError
from app.models import User, Wallet
from app.services.common import is_staff


def assert_wallet_access(user: User, wallet: Wallet) -> None:
    if is_staff(user) or wallet.user_id == user.id:
        return
    raise ForbiddenError("You do not have access to this wallet.", code="WALLET_ACCESS_DENIED")


def assert_wallet_owner(user: User, wallet: Wallet) -> None:
    if wallet.user_id != user.id:
        raise ForbiddenError("You do not own this wallet.", code="WALLET_ACCESS_DENIED")


def visible_wallet_ids(user: User, wallet_repo) -> list[uuid.UUID] | None:
    """None = unrestricted (staff). Otherwise the list of the customer's own wallet ids."""
    return None if is_staff(user) else wallet_repo.ids_for_user(user.id)

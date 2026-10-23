"""Central, declarative state machines. Services never assign `.status` without going
through `transition()`, so invalid transitions are rejected in exactly one place."""
from app.core.exceptions import InvalidStateTransitionError
from app.models.enums import RefundStatus, TxStatus, WalletStatus

WALLET_TRANSITIONS: dict[WalletStatus, set[WalletStatus]] = {
    WalletStatus.ACTIVE: {WalletStatus.FROZEN, WalletStatus.SUSPENDED, WalletStatus.CLOSED},
    WalletStatus.FROZEN: {WalletStatus.ACTIVE, WalletStatus.SUSPENDED, WalletStatus.CLOSED},
    WalletStatus.SUSPENDED: {WalletStatus.ACTIVE, WalletStatus.FROZEN, WalletStatus.CLOSED},
    WalletStatus.CLOSED: set(),
}

# FAILED -> PROCESSING exists only for the (bounded) retry of retryable failures.
TX_TRANSITIONS: dict[TxStatus, set[TxStatus]] = {
    TxStatus.PENDING: {TxStatus.PROCESSING, TxStatus.CANCELLED, TxStatus.FAILED},
    TxStatus.PROCESSING: {TxStatus.COMPLETED, TxStatus.FAILED},
    TxStatus.COMPLETED: set(),
    TxStatus.FAILED: {TxStatus.PROCESSING},
    TxStatus.CANCELLED: set(),
}

REFUND_TRANSITIONS: dict[RefundStatus, set[RefundStatus]] = {
    RefundStatus.REQUESTED: {RefundStatus.UNDER_REVIEW, RefundStatus.APPROVED, RefundStatus.REJECTED},
    RefundStatus.UNDER_REVIEW: {RefundStatus.APPROVED, RefundStatus.REJECTED},
    RefundStatus.APPROVED: {RefundStatus.COMPLETED, RefundStatus.FAILED},
    RefundStatus.REJECTED: set(),
    RefundStatus.COMPLETED: set(),
    RefundStatus.FAILED: set(),
}


def ensure_transition(table: dict, current, new, entity: str) -> None:
    if new not in table.get(current, set()):
        raise InvalidStateTransitionError(
            f"Invalid {entity} status transition: {current.value} -> {new.value}."
        )


def transition(obj, new_status, table: dict, entity: str) -> None:
    ensure_transition(table, obj.status, new_status, entity)
    obj.status = new_status

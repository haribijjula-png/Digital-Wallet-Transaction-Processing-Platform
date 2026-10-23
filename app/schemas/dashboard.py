from pydantic import BaseModel

from app.schemas.wallets import TransactionOut


class AdminDashboard(BaseModel):
    total_users: int
    active_wallets: int
    total_wallet_balance: dict[str, str]
    todays_transactions: int
    todays_transaction_volume: dict[str, str]
    failed_transactions: int
    suspicious_transactions: int


class SupportDashboard(BaseModel):
    pending_reviews: int
    suspicious_transactions: int
    failed_transactions: int
    refund_requests: int
    frozen_wallets: int


class WalletBalanceItem(BaseModel):
    wallet_id: str
    name: str
    currency: str
    status: str
    available_balance: str
    pending_balance: str


class CustomerDashboard(BaseModel):
    wallet_balances: list[WalletBalanceItem]
    total_balance: dict[str, str]
    recent_transactions: list[TransactionOut]
    total_deposits: dict[str, str]
    total_transfers: dict[str, str]
    total_withdrawals: dict[str, str]
    pending_transactions: int

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Digital Wallet & Transaction Processing Platform"
    debug: bool = False

    database_url: str = "postgresql+psycopg2://wallet:wallet@localhost:5432/wallet_db"

    secret_key: str = "dev-only-secret-change-me"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    bcrypt_rounds: int = 12

    # Simulated payment gateway
    auto_process_payments: bool = True
    allow_simulation: bool = True

    # Scheduled processing
    pending_deposit_timeout_minutes: int = 30
    pending_review_timeout_hours: int = 72
    max_retry_attempts: int = 3
    idempotency_ttl_hours: int = 48

    max_wallets_per_user: int = 10
    supported_currencies: list[str] = ["INR", "USD", "EUR"]

    seed_admin_email: str = "superadmin@example.com"
    seed_admin_password: str = "ChangeMe123!"

    redis_url: str = "redis://localhost:6379/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

import os

# Must be set BEFORE the app (and its engine/settings) is imported.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg2://wallet:wallet@localhost:5432/wallet_test")
os.environ.setdefault("BCRYPT_ROUNDS", "4")           # fast hashing in tests
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.main import app
from tests import helpers

DEFAULT_LIMITS = """
INSERT INTO transaction_limits (id, max_transaction_amount, daily_transfer_limit, daily_withdrawal_limit,
  max_transactions_per_day, suspicious_amount_threshold, velocity_window_minutes, velocity_review_count,
  velocity_block_count, frequency_review_threshold, recently_frozen_hours)
VALUES (1, 100000, 200000, 100000, 50, 25000, 10, 5, 10, 20, 24)
"""


@pytest.fixture(scope="session", autouse=True)
def _database():
    """Build the schema exactly as production does: through the Alembic migrations."""
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_database):
    with engine.begin() as conn:
        tables = [r[0] for r in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename NOT IN ('roles','alembic_version','transaction_limits')"))]
        # CASCADE also clears transaction_limits (FK updated_by -> users), so re-seed the singleton row.
        conn.execute(text("TRUNCATE " + ", ".join(tables) + ", transaction_limits RESTART IDENTITY CASCADE"))
        conn.execute(text(DEFAULT_LIMITS))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def customer(client):
    return helpers.make_customer(client, "alice@example.com")


@pytest.fixture
def customer2(client):
    return helpers.make_customer(client, "bob@example.com")


@pytest.fixture
def admin(client):
    return helpers.make_staff(client, "ADMIN", "admin@example.com")


@pytest.fixture
def super_admin(client):
    return helpers.make_staff(client, "SUPER_ADMIN", "root@example.com")


@pytest.fixture
def support(client):
    return helpers.make_staff(client, "SUPPORT_AGENT", "support@example.com")


@pytest.fixture
def funded(client, customer, customer2):
    """Alice: INR wallet with 1000.00 (spendable); Bob: empty INR wallet."""
    a = helpers.create_wallet(client, customer)
    b = helpers.create_wallet(client, customer2)
    helpers.deposit(client, customer, a["id"], "1000.00")
    return a, b
